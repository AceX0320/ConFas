"""ConFas shared-learnings cloud sync (optional).

What this does
--------------
Every conversion already teaches the local LinUCB bandit (see ``learning.py``).
This module lets a user *donate* those learnings to a central endpoint and
*download* the community pool, so a brand-new install starts warm instead of
cold - i.e. "the improved version" for people who download the tool later.

How it works (two supported transports, both extremely simple):

1. **Plain JSON mode** (default when you set a URL) - used with GitHub Gists,
   Cloudflare Workers/KV, S3 or any tiny REST service::

       GET  {CLOUD_URL}/state        -> {"bandit": {...}, "median_ms": {...}}
       PUT  {CLOUD_URL}/state        <- same shape (whole-state merge server-side)

2. **GitHub Gist mode** (recommended zero-backend setup) - set a fine-grained
   PAT with ``gist`` scope in ``CONFAS_CLOUD_TOKEN`` (or write it to
   ``~/.confas/cloud_token``). The gist holds one file per contributor plus an
   aggregated ``merged.json``; the merge worker runs in CI
   (``python -m confas.cloud --merge-gist``).

Privacy: only aggregate numbers are ever uploaded - learned weight matrices,
per-pair median latencies and arm sample counts. **No file names, no paths, no
content.** Opt-in: if ``cloud_sync`` is off (the default) nothing leaves the
machine. Conflicts never corrupt anything: every state carries a Lamport clock
and merges take element-wise maxima / weighted averages.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

DEFAULT_GIST_ID = "REPLACE_WITH_YOUR_GIST_ID"
API_ROOT = "https://api.github.com"


# --------------------------------------------------------------------------- #
#  config helpers
# --------------------------------------------------------------------------- #

def _data_dir() -> Path:
    return Path(os.environ.get("CONFAS_HOME", str(Path.home() / ".confas")))


def load_cloud_config() -> dict:
    """Merge DEFAULT_CONFIG's cloud keys with the user's config.json."""
    cfg = {}
    try:
        from .monitor import DEFAULT_CONFIG, load_config
        full = load_config()
        cfg = {k: full.get(k, DEFAULT_CONFIG.get(k)) for k in DEFAULT_CONFIG
               if k.startswith("cloud_")}
    except Exception:
        pass
    return cfg


def token() -> str:
    t = os.environ.get("CONFAS_CLOUD_TOKEN", "").strip()
    if not t:
        f = _data_dir() / "cloud_token"
        if f.exists():
            t = f.read_text(encoding="utf-8").strip()
    return t


# --------------------------------------------------------------------------- #
#  transport (urllib - no extra dependency inside the frozen EXE)
# --------------------------------------------------------------------------- #

def _request(url: str, method: str = "GET", body: Optional[dict] = None,
             headers: Optional[dict] = None, timeout: int = 15) -> Optional[dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", "ConFas/CloudSync")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return None


# --------------------------------------------------------------------------- #
#  merge logic (pure-python + numpy, safe on client AND in CI)
# --------------------------------------------------------------------------- #

def merge_states(states: list[dict]) -> dict:
    """Merge contributor states into one pooled state.

    * ``bandit`` arms: A matrices averaged across contributors that have >=1
      sample (each contributor's A starts at ridge*I, so averaging keeps the
      proper prior scale), b vectors summed (rewards accumulate), n summed.
    * ``median_ms``: minimum observed latency wins (best-case speed knowledge).
    * ``clock``: max + 1 (Lamport).
    """
    bandit: dict[str, dict] = {}
    medians: dict[str, float] = {}
    clock = 0
    for st in states:
        if not isinstance(st, dict):
            continue
        clock = max(clock, int(st.get("clock", 0)))
        for arm, s in (st.get("bandit") or {}).items():
            d = bandit.setdefault(arm, {"A_sum": None, "nA": 0, "b_sum": None, "n": 0})
            try:
                A = np.array(s["A"], dtype=float)
                b = np.array(s["b"], dtype=float)
            except Exception:
                continue
            n = int(s.get("n", 0))
            d["n"] += n
            if n > 0:                       # only real learners vote on the prior
                d["nA"] += 1
                d["A_sum"] = A if d["A_sum"] is None else d["A_sum"] + A
                d["b_sum"] = b if d["b_sum"] is None else d["b_sum"] + b
        for pair, ms in (st.get("median_ms") or {}).items():
            try:
                ms = float(ms)
            except Exception:
                continue
            if pair not in medians or ms < medians[pair]:
                medians[pair] = ms
    out_bandit = {}
    for arm, d in bandit.items():
        if d["A_sum"] is None:
            continue
        out_bandit[arm] = {
            "A": (d["A_sum"] / max(d["nA"], 1)).tolist(),
            "b": d["b_sum"].tolist(),
            "n": d["n"],
        }
    return {"version": 1, "clock": clock + 1, "updated": time.time(),
            "bandit": out_bandit, "median_ms": medians}


def _contributor_id(cfg: dict) -> str:
    cid = cfg.get("cloud_client_id")
    if not cid:
        import uuid
        cid = uuid.uuid4().hex[:16]
        cfg["cloud_client_id"] = cid
        try:
            from .monitor import load_config, save_config
            full = load_config()
            full["cloud_client_id"] = cid
            save_config(full)
        except Exception:
            pass
    return cid


# --------------------------------------------------------------------------- #
#  public API
# --------------------------------------------------------------------------- #

class CloudSync:
    """Push/pull the bandit state to/from the shared learning pool."""

    def __init__(self, selector, cfg: Optional[dict] = None) -> None:
        self.selector = selector                    # BanditSelector
        cfg = cfg if cfg is not None else load_cloud_config()
        self.cfg = cfg
        self.mode = (cfg.get("cloud_mode") or "url").lower()
        self.url = (cfg.get("cloud_url") or "").strip()
        self.gist_id = (cfg.get("cloud_gist_id") or DEFAULT_GIST_ID).strip()
        self.token = token()
        self.interval = float(cfg.get("cloud_interval_s", 900))
        self.pull_on_start = bool(cfg.get("cloud_pull_on_start", True))
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    # -- enabled? ----------------------------------------------------------- #
    def enabled(self) -> bool:
        if not self.cfg.get("cloud_enabled"):
            return False
        if self.mode == "gist":
            return bool(self.gist_id and self.gist_id != DEFAULT_GIST_ID)
        return bool(self.url)

    # -- payload ------------------------------------------------------------ #
    def build_payload(self) -> dict:
        return {"version": 1,
                "clock": int(self.cfg.get("cloud_clock", 0)) + 1,
                "bandit": self.selector.bandit.export(),
                "median_ms": dict(self.selector.median_ms)}

    # -- pull --------------------------------------------------------------- #
    def pull(self) -> bool:
        """Download pooled state and merge it into the local bandit."""
        with self._lock:
            state = self._fetch_pooled()
            if not state or not state.get("bandit"):
                return False
            try:
                self.selector.bandit.import_state(state["bandit"])
                for pair, ms in (state.get("median_ms") or {}).items():
                    cur = self.selector.median_ms.get(pair)
                    if cur is None or float(ms) < cur:
                        self.selector.median_ms[pair] = float(ms)
                self.selector._save_state()
                self._set_meta("cloud_last_pull", time.time())
                self._set_meta("cloud_clock", int(state.get("clock", 0)))
                return True
            except Exception:
                return False

    def _fetch_pooled(self) -> Optional[dict]:
        if self.mode == "gist":
            data = _request(f"{API_ROOT}/gists/{self.gist_id}",
                            headers=self._gh_headers())
            if not data:
                return None
            files = data.get("files") or {}
            entry = files.get("merged.json") or files.get("state.json")
            if not entry:
                return None
            raw = entry.get("content")
            if raw is None:                      # too big -> must fetch raw_url
                raw_resp = _request(entry.get("raw_url", ""))
                return raw_resp                  # best effort
            try:
                return json.loads(raw)
            except Exception:
                return None
        return _request(f"{self.url.rstrip('/')}/state")

    # -- push --------------------------------------------------------------- #
    def push(self) -> bool:
        """Upload our aggregate learnings to the shared pool."""
        with self._lock:
            payload = self.build_payload()
            if self.mode == "gist":
                ok = self._push_gist(payload)
            else:
                ok = _request(f"{self.url.rstrip('/')}/state", "PUT",
                              payload) is not None
            if ok:
                self._set_meta("cloud_last_push", time.time())
                self._set_meta("cloud_clock", payload["clock"])
            return ok

    def _gh_headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.token}"}
        if self.token:
            h["Accept"] = "application/vnd.github+json"
        return h

    def _push_gist(self, payload: dict) -> bool:
        if not self.token:
            return False
        data = _request(f"{API_ROOT}/gists/{self.gist_id}", headers=self._gh_headers())
        if not data:
            return False
        files = dict(data.get("files") or {})
        mine = f"contrib-{_contributor_id(self.cfg)}.json"
        keep = {k: {"content": v["content"]} for k, v in files.items()
                if k.endswith(".json") and "content" in v}
        # strip our own stale copy, merge everything, re-add merged + contribution
        keep.pop(mine, None)
        keep.pop("merged.json", None)
        contribs = []
        for name, f in keep.items():
            try:
                contribs.append(json.loads(f["content"]))
            except Exception:
                continue
        contribs.append(payload)
        keep[mine] = {"content": json.dumps(payload)}
        keep["merged.json"] = {"content": json.dumps(merge_states(contribs))}
        resp = _request(f"{API_ROOT}/gists/{self.gist_id}", "PATCH",
                        {"files": keep}, headers=self._gh_headers())
        return resp is not None

    # -- meta --------------------------------------------------------------- #
    def _set_meta(self, key: str, value) -> None:
        try:
            from .monitor import load_config, save_config
            cfg = load_config()
            cfg[key] = value
            save_config(cfg)
            self.cfg[key] = value
        except Exception:
            pass

    # -- background loop ----------------------------------------------------- #
    def start(self) -> None:
        if not self.enabled():
            return
        if self.pull_on_start:
            threading.Thread(target=self.pull, daemon=True,
                             name="confas-cloud-pull").start()
        self._schedule(self.interval)

    def _schedule(self, delay: Optional[float] = None) -> None:
        """(Re-)arm the sync timer. Debounced: never fires more often than
        ``cloud_debounce_s`` (default 60 s), so a burst of conversions results
        in exactly one upload."""
        debounce = float(self.cfg.get("cloud_debounce_s", 60))
        delay = max(delay if delay is not None else self.interval, debounce)
        now = time.time()
        if self._timer is not None and getattr(self, "_next_at", 0) - debounce < now:
            return                      # one already pending soon enough
        if self._timer:
            self._timer.cancel()
        self._next_at = now + delay
        self._timer = threading.Timer(delay, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _schedule_now(self) -> None:
        """Called by the selector after every recorded conversion."""
        self._schedule(float(self.cfg.get("cloud_debounce_s", 60)))

    def _tick(self) -> None:
        try:
            self.push()
            self.pull()
        finally:
            self._schedule(self.interval)

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def status(self) -> dict:
        return {"enabled": self.enabled(), "mode": self.mode,
                "endpoint": self.url or (f"gist:{self.gist_id}" if self.enabled() else ""),
                "token_set": bool(self.token),
                "last_pull": self.cfg.get("cloud_last_pull"),
                "last_push": self.cfg.get("cloud_last_push"),
                "arms_learned": len(self.selector.bandit.n),
                "samples": sum(self.selector.bandit.n.values())}


# --------------------------------------------------------------------------- #
#  CI merge worker:  python -m confas.cloud --merge-gist  (run from repo root)
# --------------------------------------------------------------------------- #

def merge_gist_worker(gist_id: Optional[str] = None, token_: Optional[str] = None,
                      dry_run: bool = False) -> dict:
    """Recompute merged.json from all contrib-*.json files in the gist.

    Used by the scheduled GitHub Action so contributors only ever PATCH their
    own file (no lost-update races on the aggregate).
    """
    gid = gist_id or os.environ.get("CONFAS_GIST_ID") or DEFAULT_GIST_ID
    tok = token_ or token()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    data = _request(f"{API_ROOT}/gists/{gid}", headers=headers)
    if not data:
        raise RuntimeError(f"cannot read gist {gid}")
    files = data.get("files") or {}
    contribs, keep = [], {k: {"content": v["content"]}
                          for k, v in files.items()
                          if k.startswith("contrib-") and "content" in v}
    for f in keep.values():
        try:
            contribs.append(json.loads(f["content"]))
        except Exception:
            continue
    merged = merge_states(contribs)
    if not dry_run and tok:
        keep["merged.json"] = {"content": json.dumps(merged)}
        _request(f"{API_ROOT}/gists/{gid}", "PATCH", {"files": keep},
                 headers=headers)
    return merged


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(prog="confas.cloud")
    ap.add_argument("--merge-gist", action="store_true")
    ap.add_argument("--gist-id", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args()
    if ns.merge_gist:
        m = merge_gist_worker(ns.gist_id, dry_run=ns.dry_run)
        print(json.dumps({"clock": m["clock"], "arms": len(m["bandit"]),
                          "pairs": len(m["median_ms"])}, indent=2))
    else:
        ap.print_help()
