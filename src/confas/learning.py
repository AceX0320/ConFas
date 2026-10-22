"""ConFas adaptive learning (lightweight ML) for backend selection.

Why this exists
---------------
For a given conversion pair (e.g. ``pdf -> docx``) several backends may be
available (``pdf2docx``, ``libreoffice``, ...).  Which one is *fastest* and
*most reliable* depends on the machine, the installed software versions and
even on characteristics of the file (size, page count proxy...).

Instead of hard-coding an order we learn it online with a **contextual
multi-armed bandit** (LinUCB-style linear reward model, pure Python/numpy,
zero heavy dependencies so it fits inside a small frozen EXE):

    context features x  = [1, log(bytes), log(path depth), src_ext one-hot,
                           dst_ext one-hot, hour-of-day sin/cos]
    per-backend linear model theta_b updated with ridge regression:
        theta_b <- (A_b + lambda I)^-1 b_b          (Woodbury / closed form)
    optimistic score = x^T theta_b + alpha * sqrt(x^T A_b^-1 x)

Reward combines speed and success:

    r = success(0/1) * 1.0  -  penalty for latency  =  success - 0.5*log(ms/median_ms)

so the selector converges to the backend that is both reliable and fast.
Every completed conversion is appended to ``~/.confas/history.jsonl`` which
doubles as an audit log (visible via ``confas stats``).

Cold start: until enough samples exist, static engine weights are used; the
bandit only *re-orders* candidates, it never removes fallbacks, therefore
correctness can never degrade - worst case the first attempt fails and the
engine transparently retries the next backend.

Set environment variable ``CONFAS_DISABLE_ML=1`` to switch this off entirely
(the "scratch that part" option - everything still works).
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import numpy as np
    _HAVE_NUMPY = True
except Exception:  # pragma: no cover - numpy is bundled by PyInstaller anyway
    _HAVE_NUMPY = False


# --------------------------------------------------------------------------- #
#  Feature extraction
# --------------------------------------------------------------------------- #

_EXT_VOCAB = ["pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "odt", "ods",
              "odp", "rtf", "txt", "md", "csv", "html", "htm", "epub", "png",
              "jpg", "jpeg", "webp", "avif", "jxl", "tif", "tiff", "gif", "bmp"]
_EXT_INDEX = {e: i for i, e in enumerate(_EXT_VOCAB)}

BASE_DIM = 6                      # bias, log_bytes, depth, sin_h, cos_h, pages_proxy
N_EXT = len(_EXT_VOCAB)
DIM = BASE_DIM + 2 * N_EXT        # + src one-hot + dst one-hot


def _features(src: Path, dst_ext: str) -> list[float]:
    try:
        size = src.stat().st_size
    except OSError:
        size = 1
    lb = math.log1p(size)
    depth = min(len(src.parts), 12) / 12.0
    h = time.localtime().tm_hour + time.localtime().tm_min / 60.0
    sin_h, cos_h = math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24)
    pages_proxy = min(lb / 18.0, 1.0)     # bigger files ~ more pages ~ slower
    f = [1.0, lb, depth, sin_h, cos_h, pages_proxy]
    src_one = [0.0] * N_EXT
    dst_one = [0.0] * N_EXT
    s = src.suffix.lstrip(".").lower()
    d = dst_ext.lstrip(".").lower()
    if s in _EXT_INDEX:
        src_one[_EXT_INDEX[s]] = 1.0
    if d in _EXT_INDEX:
        dst_one[_EXT_INDEX[d]] = 1.0
    return f + src_one + dst_one


# --------------------------------------------------------------------------- #
#  LinUCB selector
# --------------------------------------------------------------------------- #

class _LinearBandit:
    """Per-arm ridge-regression UCB bandit."""

    def __init__(self, dim: int = DIM, alpha: float = 1.2, ridge: float = 8.0) -> None:
        self.dim = dim
        self.alpha = alpha
        self.ridge = ridge
        self.A: dict[str, "np.ndarray"] = {}   # arm -> covariance accumulator
        self.b: dict[str, "np.ndarray"] = {}   # arm -> reward vector
        self.n: dict[str, int] = {}
        self._lock = threading.Lock()

    def _ensure(self, arm: str) -> None:
        if arm not in self.A:
            self.A[arm] = np.eye(self.dim) * self.ridge
            self.b[arm] = np.zeros(self.dim)
            self.n[arm] = 0

    def score(self, arm: str, x: "np.ndarray") -> float:
        self._ensure(arm)
        A_inv = np.linalg.inv(self.A[arm])
        theta = A_inv @ self.b[arm]
        mean = float(theta @ x)
        ucb = self.alpha * float(math.sqrt(max(x @ A_inv @ x, 0.0)))
        return mean + ucb

    def update(self, arm: str, x: "np.ndarray", reward: float) -> None:
        with self._lock:
            self._ensure(arm)
            self.A[arm] += np.outer(x, x)
            self.b[arm] += reward * x
            self.n[arm] += 1

    def export(self) -> dict:
        return {a: {"A": self.A[a].tolist(), "b": self.b[a].tolist(), "n": self.n[a]}
                for a in self.A}

    def import_state(self, state: dict) -> None:
        for a, s in (state or {}).items():
            self.A[a] = np.array(s["A"], dtype=float)
            self.b[a] = np.array(s["b"], dtype=float)
            self.n[a] = int(s.get("n", 0))


# --------------------------------------------------------------------------- #
#  Public selector used by the engine
# --------------------------------------------------------------------------- #

class BanditSelector:
    """Re-orders candidate routes using learned latencies/success rates."""

    MIN_SAMPLES_TO_TRUST = 6      # per arm, before scores beat static weights

    def __init__(self, history_file: Optional[Path] = None) -> None:
        if not _HAVE_NUMPY:
            raise RuntimeError("numpy required for ML selector")
        self.data_dir = (history_file or Path.home() / ".confas" / "history.jsonl").parent
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.data_dir / "history.jsonl"
        self.state_file = self.data_dir / "bandit.json"
        self.median_ms: dict[str, float] = {}       # "src>dst|arm" -> running median-ish
        self.bandit = _LinearBandit()
        self._load_state()

    # -- persistence -------------------------------------------------------- #
    def _load_state(self) -> None:
        try:
            st = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.bandit.import_state(st.get("bandit", {}))
            self.median_ms = st.get("median_ms", {})
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _save_state(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"bandit": self.bandit.export(),
                                       "median_ms": self.median_ms}),
                           encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception:
            pass

    # -- API used by engine -------------------------------------------------- #
    def rank(self, src: Path, dst_ext: str, routes: list) -> list:
        """Return routes sorted best-first (stable, never drops fallbacks)."""
        if len(routes) <= 1:
            return routes
        x = np.array(_features(src, dst_ext), dtype=float)
        pair_key = f"{src.suffix.lstrip('.').lower()}>{dst_ext.lstrip('.').lower()}"

        scored = []
        for pos, r in enumerate(routes):
            arm = f"{pair_key}|{r.name}"
            n = self.bandit.n.get(arm, 0)
            if n >= self.MIN_SAMPLES_TO_TRUST:
                s = self.bandit.score(arm, x)
            else:
                # cold start: blend static weight into a neutral prior so that
                # exploration still happens but sensible defaults are respected
                s = (r.weight / 100.0) * 0.35 + self.bandit.score(arm, x) * 0.0 \
                    + (self.bandit.score(arm, x) if n else 0.0)
                s += 0.05 * (self.MIN_SAMPLES_TO_TRUST - n) / self.MIN_SAMPLES_TO_TRUST  # explore bonus
            scored.append((-s, -r.weight, pos, r))
        scored.sort(key=lambda t: (t[0], t[1], t[2]))
        return [t[3] for t in scored]

    def record(self, src: Path, dst_ext: str, backend: str, elapsed_ms: float, ok: bool) -> None:
        pair_key = f"{src.suffix.lstrip('.').lower()}>{dst_ext.lstrip('.').lower()}"
        arm = f"{pair_key}|{backend}"
        x = np.array(_features(src, dst_ext), dtype=float)

        key = pair_key
        med = self.median_ms.get(key)
        ref = med if med and med > 0 else max(elapsed_ms, 1.0)
        speed_reward = 1.0 - 0.5 * abs(math.log(max(elapsed_ms, 1.0) / ref))
        reward = (1.0 if ok else -1.0) + max(min(speed_reward, 1.0), -1.0) * 0.5

        self.bandit.update(arm, x, float(reward))
        # exponential moving average of successful latency per pair
        if ok:
            prev = self.median_ms.get(key)
            self.median_ms[key] = elapsed_ms if prev is None else prev * 0.7 + elapsed_ms * 0.3

        entry = {
            "pair": pair_key, "arm": arm, "ms": round(elapsed_ms, 1),
            "ok": ok, "reward": round(reward, 3), "file": src.name, "t": time.time(),
        }
        try:
            with open(self.history_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception:
            pass
        self._save_state()

    # -- introspection ------------------------------------------------------- #
    def stats(self) -> dict:
        out = {"arms": {}, "pairs_ms": self.median_ms}
        for arm, n in self.bandit.n.items():
            A_inv = np.linalg.inv(self.bandit.A[arm])
            theta = A_inv @ self.bandit.b[arm]
            out["arms"][arm] = {"samples": n, "mean_reward_at_bias_ctx": round(float(theta[0]), 3)}
        return out
