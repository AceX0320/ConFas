"""ConFas full debugging layer.

After every conversion the engine writes a human-readable ``*.confas-debug.txt``
report next to the output file showing exactly what went right and what went
wrong: environment, backend availability, the ordered candidate chain, ML
ranking scores, every attempt (success or failure with full traceback), timing
and byte counts.

The layer is fail-safe by design: a broken/ unwritable report can NEVER break
or slow down an actual conversion (all writer errors are swallowed).

Modes (config key ``debug_reports``, CLI ``ConFas.exe config debug=...``):
    always  - write a report after every conversion            (default)
    onerror - only write reports when a conversion failed
    off     - never write reports
Env override: CONFAS_DEBUG_REPORTS=always|onerror|off
Retention:    the last ``debug_keep`` (default 200) reports are kept in the
              data dir; older ones are pruned automatically.
"""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

MODE_ALWAYS = "always"
MODE_ON_ERROR = "onerror"
MODE_OFF = "off"

_write_lock = threading.Lock()


# --------------------------------------------------------------------------- #
#  configuration
# --------------------------------------------------------------------------- #

def _cfg_value(key: str, default):
    """Read a config value without hard-depending on monitor (test-safe)."""
    try:
        from . import monitor
        return monitor.load_config().get(key, default)
    except Exception:
        return default


def get_mode() -> str:
    m = os.environ.get("CONFAS_DEBUG_REPORTS") or _cfg_value("debug_reports", MODE_ALWAYS)
    m = str(m).strip().lower()
    if m in ("1", "true", "yes", "on", MODE_ALWAYS):
        return MODE_ALWAYS
    if m in (MODE_ON_ERROR, "errors", "error"):
        return MODE_ON_ERROR
    if m in ("0", "false", "no", MODE_OFF):
        return MODE_OFF
    return MODE_ALWAYS


def should_write(ok: bool) -> bool:
    mode = get_mode()
    if mode == MODE_OFF:
        return False
    if mode == MODE_ON_ERROR:
        return not ok
    return True


def keep_count() -> int:
    try:
        return max(10, int(_cfg_value("debug_keep", 200)))
    except Exception:
        return 200


# --------------------------------------------------------------------------- #
#  report writing
# --------------------------------------------------------------------------- #

def _fmt_exc(exc: Optional[BaseException]) -> str:
    if exc is None:
        return ""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def render_report(session: dict) -> str:
    """Render a session dict into the plain-text report."""
    L: list[str] = []
    bar = "=" * 78
    sub = "-" * 78
    L.append(bar)
    L.append("ConFas CONVERSION DEBUG REPORT")
    L.append(bar)
    L.append(f"Run id      : {session.get('run_id', '')}")
    L.append(f"Started     : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session.get('t_start', time.time())))}"
             f"   (pid {os.getpid()}, thread {threading.current_thread().name})")
    finished = session.get("t_end")
    if finished:
        L.append(f"Finished    : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(finished))}")
    L.append("")

    L.append("[ENVIRONMENT]")
    L.append(f"  ConFas version : {session.get('version', '?')}")
    L.append(f"  OS             : {platform.platform()}")
    L.append(f"  Python         : {sys.version.split()[0]} ({'frozen EXE' if getattr(sys, 'frozen', False) else 'source'})")
    L.append(f"  Data dir       : {session.get('data_dir', '')}")
    L.append("")

    src = Path(session.get("src", ""))
    L.append("[INPUT / OUTPUT]")
    L.append(f"  Source         : {src}")
    L.append(f"  Source size    : {session.get('bytes_in', 0):,} bytes")
    L.append(f"  Target ext     : {session.get('dst_ext', '')}")
    L.append(f"  Planned output : {session.get('planned_out', '')}")
    L.append(f"  Actual output  : {session.get('out_path', '')}")
    if session.get("bytes_out"):
        L.append(f"  Output size    : {session['bytes_out']:,} bytes")
    L.append("")

    L.append("[BACKEND AVAILABILITY]")
    for k, v in (session.get("backends") or {}).items():
        L.append(f"  {k:<14}: {v}")
    L.append("")

    ml = session.get("ml") or {}
    L.append("[ML BACKEND SELECTION]")
    L.append(f"  enabled        : {ml.get('enabled', False)}")
    if ml.get("history_samples") is not None:
        L.append(f"  history samples: {ml['history_samples']}")
    order = ml.get("order_after") or [r["name"] for r in session.get("routes", [])]
    L.append(f"  final order    : {' > '.join(order) if order else '(none)'}")
    if ml.get("scores"):
        L.append("  bandit scores (higher = tried first):")
        for arm, sc in sorted(ml["scores"].items(), key=lambda kv: -kv[1]):
            L.append(f"    {arm:<20}{sc:>10.3f}")
    L.append("")

    routes = session.get("routes", [])
    attempts = session.get("attempts", [])
    att_by_name: dict[str, list] = {}
    for a in attempts:
        att_by_name.setdefault(a["backend"], []).append(a)

    L.append("[CANDIDATE CHAIN & ATTEMPTS]")
    for i, r in enumerate(routes):
        mark = ">" if any(a["backend"] == r["name"] for a in attempts) else " "
        L.append(f"  {mark} [{i}] {r['name']} (static weight {r.get('weight', 0)})")
        for a in att_by_name.get(r["name"], []):
            if a["ok"]:
                L.append(f"      + SUCCESS in {a['ms']:.1f} ms")
            else:
                L.append(f"      x FAILED after {a['ms']:.1f} ms")
                err = a.get("error", "")
                L.append(f"        error: {err[:500]}")
                tb = a.get("traceback", "")
                if tb:
                    for line in tb.rstrip().splitlines():
                        L.append(f"        | {line}")
    L.append("")

    total_ms = session.get("total_ms", 0.0)
    ok = session.get("ok", False)
    L.append("[RESULT]")
    L.append(f"  Outcome        : {'SUCCESS' if ok else 'FAILURE'}")
    L.append(f"  Backend used   : {session.get('winning_backend') or '-'}")
    L.append(f"  Total elapsed  : {total_ms:.1f} ms")
    nfail = sum(1 for a in attempts if not a["ok"])
    L.append(f"  Attempts       : {len(attempts)} ({nfail} failed)")
    if session.get("final_error"):
        L.append(f"  Final error    : {session['final_error']}")
    if ok:
        L.append("")
        L.append("  What went RIGHT:")
        L.append(f"    - conversion completed via '{session.get('winning_backend')}' producing a valid "
                 f"'{session.get('dst_ext')}' file at the planned location.")
        if nfail:
            L.append(f"    - fallback worked: {nfail} earlier backend(s) failed and the chain recovered.")
    elif attempts:
        L.append("")
        L.append("  What went WRONG:")
        for a in attempts:
            if not a["ok"]:
                L.append(f"    - backend '{a['backend']}': {a.get('error', '')[:300]}")
        L.append("    - no remaining backend could produce the output; see tracebacks above.")
    L.append(bar)
    L.append("End of report. (disable with `ConFas.exe config debug=off`)")
    return "\n".join(L) + "\n"


def _prune(data_dir: Path, keep: int) -> None:
    try:
        files = sorted(data_dir.glob("*.confas-debug.txt"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[keep:]
        for old in files:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def new_session(src, dst_ext: str) -> dict:
    """Create a fresh debug session record (called at convert() entry)."""
    data_dir = ""
    version = "?"
    try:
        from . import monitor
        data_dir = str(monitor.DATA_DIR)
    except Exception:
        data_dir = str(Path.home() / ".confas")
    try:
        from . import __version__ as version  # type: ignore
    except Exception:
        pass
    return {
        "run_id": uuid_hex(),
        "t_start": time.time(),
        "src": str(src),
        "dst_ext": str(dst_ext or ""),
        "planned_out": "",
        "out_path": "",
        "bytes_in": 0,
        "bytes_out": 0,
        "version": version,
        "data_dir": data_dir,
        "backends": _backend_snapshot(),
        "ml": {"enabled": False},
        "routes_static": [],
        "routes": [],
        "attempts": [],
        "ok": False,
        "winning_backend": "",
        "final_error": "",
        "total_ms": 0.0,
    }


def uuid_hex() -> str:
    try:
        import uuid
        return uuid.uuid4().hex[:12]
    except Exception:
        return f"{time.time():.6f}"[-12:]


def _backend_snapshot() -> dict:
    try:
        from .engine import BACKENDS
        return BACKENDS.summary()
    except Exception:
        return {}


def finalize(sess: dict, result) -> None:
    """Close out a session and write its txt report. Never raises."""
    try:
        sess.setdefault("t_end", time.time())
        if not sess.get("ml"):
            sess["ml"] = {"enabled": False}
        write_report(sess)
    except Exception:
        pass


def write_report(session: dict) -> Optional[Path]:
    """Write the debug txt report. Never raises - returns path or None."""
    try:
        if not should_write(bool(session.get("ok"))):
            return None
        text = render_report(session)

        # primary copy: next to the converted file (only when one was actually
        # produced, otherwise the central copy below is the single source)
        out = Path(session.get("out_path") or "")
        primary: Optional[Path] = None
        if session.get("ok") and out.exists():
            cand = out.with_name(out.name + ".confas-debug.txt")
            try:
                cand.write_text(text, encoding="utf-8")
                primary = cand
            except OSError:
                primary = None

        # central copy: ~/.confas/debug/ (always, so failures are findable)
        try:
            from . import monitor
            data_dir = monitor.data_dir()
        except Exception:
            data_dir = Path.home() / ".confas"
        session["data_dir"] = str(data_dir)
        try:
            dd = data_dir / "debug"
            dd.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            stem = Path(session.get("src", "file")).stem[:60]
            status = "OK" if session.get("ok") else "FAIL"
            with _write_lock:
                central = dd / f"{stamp}-{stem}-{status}.confas-debug.txt"
                central.write_text(text, encoding="utf-8")
                _prune(dd, keep_count())
        except Exception:
            pass

        session["report_path"] = str(primary) if primary else ""
        return primary
    except Exception:
        # the debugging layer must never break conversions
        return None
