"""ConFas command line interface.

This is what the Windows right-click menu invokes:

    ConFas.exe convert "C:\\path\\file.pdf"              # -> newest generation (docx)
    ConFas.exe convert "C:\\path\\file.pdf" -t docx      # explicit target
    ConFas.exe convert file1 file2 file3 -t pdf          # batch, parallel
    ConFas.exe run [--headless]                          # tray daemon + watcher
    ConFas.exe install                                     # startup + context menu
    ConFas.exe uninstall
    ConFas.exe status | stats | config
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .engine import get_engine, newest_target_for, BACKENDS, _norm_ext
from . import monitor
from . import debuglog as _dbg


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #

def _is_ascii_console() -> bool:
    try:
        "✓".encode(sys.stdout.encoding or "utf-8")
        return True
    except Exception:
        return False


OK = "OK " if not _is_ascii_console() else "OK "
ERRSIGN = "×" if _is_ascii_console() else "x"


def _popup(title: str, text: str) -> None:
    """Small native notification when launched from Explorer right-click."""
    if monitor.IS_WINDOWS and os.environ.get("CONFAS_NO_POPUP") != "1":
        try:
            import ctypes

            MB_OK = 0x0
            MB_ICONINFORMATION = 0x40
            MB_ICONWARNING = 0x30
            flags = MB_OK | (MB_ICONWARNING if "fail" in title.lower() or "error" in title.lower()
                             else MB_ICONINFORMATION)
            ctypes.windll.user32.MessageBoxW(None, text, title, flags)
            return
        except Exception:
            pass
    print(f"{title}: {text}")


# --------------------------------------------------------------------------- #
#  commands
# --------------------------------------------------------------------------- #

def _latest_report(res) -> str:
    """Path of the debug txt report produced for this conversion (if any)."""
    try:
        out = Path(res.target or "")
        cand = out.with_name(out.name + ".confas-debug.txt")
        if cand.exists():
            return str(cand)
        dd = monitor.DATA_DIR / "debug"
        if dd.exists():
            stem = Path(res.source).stem[:60]
            status = "OK" if res.ok else "FAIL"
            matches = sorted(dd.glob(f"*-{stem}-{status}.confas-debug.txt"),
                             key=lambda p: p.stat().st_mtime)
            if matches:
                return str(matches[-1])
    except Exception:
        pass
    return ""


def cmd_convert(args) -> int:
    files = [Path(f) for f in args.files]
    files = [f for f in files if f.exists()]
    if not files:
        print("No input files found.", file=sys.stderr)
        return 2

    jobs: list[tuple[str, str]] = []
    for f in files:
        t = args.target
        if not t:
            sugg = newest_target_for(f.suffix)
            t = sugg[0] if sugg else "pdf"
        jobs.append((str(f), t))

    engine = get_engine(enable_ml=not args.no_ml)
    # (progress printed per file as conversions finish)
    results = []

    def on_done(res):
        results.append(res)
        mark = OK if res.ok else ERRSIGN
        ms = f"{res.elapsed_ms:.0f} ms"
        line = f"  {mark} {Path(res.source).name} -> {Path(res.target).name}" \
               f"  [{res.backend}, {ms}]"
        print(line)
        if not res.ok:
            print(f"      error: {res.error}", file=sys.stderr)
        # full debugging layer: point at the txt report for this conversion
        rep = _latest_report(res)
        if rep:
            print(f"      debug report: {rep}")

    t0 = time.perf_counter()
    engine.convert_many(jobs, progress=on_done)
    total_ms = (time.perf_counter() - t0) * 1000

    ok_n = sum(1 for r in results if r.ok)
    fail_n = len(results) - ok_n
    print(f"\n{ok_n} converted, {fail_n} failed - total {total_ms:.0f} ms")

    # When invoked from the shell context menu give visual feedback.
    if getattr(sys, "frozen", False) or args.popup:
        if fail_n == 0:
            outs = "\n".join(Path(r.target).name for r in results)
            _popup("ConFas", f"Converted {ok_n} file(s):\n{outs}\n({total_ms:.0f} ms)")
        else:
            errs = "\n".join(f"{Path(r.source).name}: {r.error}" for r in results if not r.ok)
            _popup("ConFas - errors", f"{fail_n} conversion(s) failed:\n{errs[:900]}")
    return 0 if fail_n == 0 else 1


def cmd_run(args) -> int:
    return monitor.run_daemon(headless=args.headless)


def cmd_install(args) -> int:
    print("ConFas setup")
    monitor.DATA_DIR.mkdir(parents=True, exist_ok=True)
    monitor.save_config(monitor.load_config())
    Path(monitor.DEFAULT_CONFIG["watch_folder"]).mkdir(exist_ok=True)

    if monitor.IS_WINDOWS:
        n = monitor.install_context_menu()
        print(f"  right-click menu entries installed : {n} extensions")
        if monitor.register_startup():
            print("  added to Startup apps (Task Manager > Startup apps)")
        else:
            print("  WARNING: could not write the Run key")
        monitor.refresh_explorer()
        print("\nDone. Sign out/in once so Explorer refreshes its menus.")
    else:
        print("  non-Windows: only watch-folder daemon available (`confas run`)")
    return 0


def cmd_uninstall(args) -> int:
    if monitor.IS_WINDOWS:
        monitor.remove_context_menu()
        monitor.unregister_startup()
        monitor.refresh_explorer()
    print("ConFas removed from Startup apps and context menus.")
    return 0


def cmd_status(args) -> int:
    cfg = monitor.load_config()
    info = {
        "version": __version__,
        "platform": sys.platform,
        "backends": BACKENDS.summary(),
        "startup_registered": monitor.is_startup_registered(),
        "watch_folder": cfg["watch_folder"],
        "auto_target": cfg["auto_target"],
        "ml_enabled": cfg["ml_enabled"],
        "debug_reports": _dbg.get_mode(),
        "data_dir": str(monitor.DATA_DIR),
        "log": str(monitor.LOG_FILE),
    }
    print(json.dumps(info, indent=2))
    return 0


def cmd_stats(args) -> int:
    hist = monitor.DATA_DIR / "history.jsonl"
    if not hist.exists():
        print("No conversions recorded yet.")
        return 0
    lines = hist.read_text(encoding="utf-8").splitlines()[-args.limit:]
    rows = [json.loads(l) for l in lines if l.strip()]
    if not rows:
        print("(empty)")
        return 0
    pairs: dict[str, list] = {}
    for r in rows:
        pairs.setdefault(r["pair"], []).append(r)
    print(f"{'pair':<14}{'backend':<16}{'runs':>5}{'ok%':>6}{'avg ms':>9}")
    for pair, rs in sorted(pairs.items()):
        by_arm: dict[str, list] = {}
        for r in rs:
            by_arm.setdefault(r["arm"].split("|", 1)[1], []).append(r)
        for arm, items in sorted(by_arm.items(), key=lambda kv: -len(kv[1])):
            oks = sum(1 for i in items if i["ok"])
            avg = sum(i["ms"] for i in items) / max(len(items), 1)
            print(f"{pair:<14}{arm:<16}{len(items):>5}{100*oks//len(items):>5}%"
                  f"{avg:>9.0f}")
    ml_on = bool(monitor.load_config().get("ml_enabled")) and os.environ.get("CONFAS_DISABLE_ML") != "1"
    print(f"\nML backend-selection: {'ENABLED' if ml_on else 'disabled'} "
          f"(toggle with `confas config ml=off` or CONFAS_DISABLE_ML=1)")
    # cloud sync status
    try:
        from .cloud import CloudSync, DEFAULT_GIST_ID
        cfg = monitor.load_config()
        sel_stub = _StubSelector()
        cs = CloudSync(sel_stub, cfg)
        gist_cfgured = (cfg.get("cloud_gist_id") or "") not in ("", DEFAULT_GIST_ID)
        print(f"Cloud learnings sync: {'ON' if cs.enabled() else 'off'} "
              f"(mode={cs.mode}, endpoint={'set' if cs.enabled() else 'not configured'})")
        if not cs.enabled() and not gist_cfgured and not cfg.get("cloud_url"):
            print("  -> share your ML improvements with other users:")
            print("     ConFas.exe config cloud_gist=<your-gist-id>")
            print("     setx CONFAS_CLOUD_TOKEN <github-pat-with-gist-scope>")
            print("     ConFas.exe config cloud_sync=on   (see README: Shared learnings)")
        elif cs.cfg.get("cloud_last_pull"):
            print(f"  last pull: {time.strftime('%Y-%m-%d %H:%M', time.localtime(cs.cfg['cloud_last_pull']))}"
                  f" | last push: "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(cs.cfg['cloud_last_push'])) if cs.cfg.get('cloud_last_push') else 'never'}")
            if cs.cfg.get("cloud_last_sha256"):
                print(f"  pooled state sha256: {cs.cfg['cloud_last_sha256']}")
        if cs.enabled():
            print("  security: HTTPS-only, aggregate numbers only (no file names/"
                  "paths/content), remote state fully validated before import.")
    except Exception as exc:  # pragma: no cover
        print(f"Cloud sync: unavailable ({exc})")
    return 0


class _StubSelector:
    """Minimal object so cmd_stats can read cloud status without a real bandit."""
    class _B:
        n: dict = {}
    bandit = _B()
    median_ms: dict = {}


def cmd_config(args) -> int:
    cfg = monitor.load_config()
    changed = False
    for assign in args.assignments:
        if "=" not in assign:
            print(f"ignoring '{assign}' (expected key=value)", file=sys.stderr)
            continue
        k, v = assign.split("=", 1)
        k = k.strip().lower()
        if k in ("watch_folder", "folder"):
            Path(v).mkdir(parents=True, exist_ok=True)
            cfg["watch_folder"] = str(Path(v).resolve())
        elif k == "auto_target":
            cfg["auto_target"] = _norm_ext(v).lstrip(".")
        elif k in ("ml", "ml_enabled"):
            cfg["ml_enabled"] = v.lower() in ("1", "on", "true", "yes")
        elif k in ("watch", "watch_enabled"):
            cfg["watch_enabled"] = v.lower() in ("1", "on", "true", "yes")
        elif k in ("debug", "debug_reports"):
            vv = v.strip().lower()
            if vv not in ("always", "onerror", "off"):
                print("debug must be one of: always | onerror | off")
                return 1
            cfg["debug_reports"] = vv
        elif k == "debug_keep":
            cfg["debug_keep"] = max(10, int(float(v)))
        elif k in ("cloud_sync", "cloud", "cloud_enabled"):
            cfg["cloud_enabled"] = v.lower() in ("1", "on", "true", "yes")
        elif k == "cloud_gist":
            cfg["cloud_gist_id"] = v.strip()
            cfg["cloud_mode"] = "gist"
        elif k == "cloud_url":
            from .cloud import _check_url
            if not _check_url(v.strip()):
                print("error: cloud_url must be a valid https:// URL "
                      "(plaintext http is refused for security)")
                return 1
            cfg["cloud_url"] = v.strip()
            cfg["cloud_mode"] = "url"
        elif k == "cloud_token":
            from .cloud import save_token
            p = save_token(v)
            print(f"token saved to {p} (owner-only). It should be a fine-grained "
                  f"PAT with ONLY the 'gist' scope.")
        elif k in ("cloud_interval", "cloud_interval_s"):
            cfg["cloud_interval_s"] = max(60, int(float(v)))
        else:
            print(f"unknown key '{k}'", file=sys.stderr)
            continue
        changed = True
    if changed:
        monitor.save_config(cfg)
        print("saved:", json.dumps(cfg, indent=2))
    else:
        print(json.dumps(cfg, indent=2))
    return 0


def cmd_targets(args) -> int:
    ext = args.ext
    print(", ".join(newest_target_for(ext)))
    return 0


# --------------------------------------------------------------------------- #
#  parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ConFas",
        description=f"ConFas v{__version__} - ultra-fast local document converter "
                    "(right-click menu + watch folder + adaptive ML routing)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="convert one or more documents")
    c.add_argument("files", nargs="+", help="source file(s)")
    c.add_argument("-t", "--target", default=None,
                   help="target extension (default: latest generation for that type)")
    c.add_argument("--no-ml", action="store_true", help="disable adaptive backend learning")
    c.add_argument("--popup", action="store_true", help="show result popup even when not frozen")
    c.set_defaults(fn=cmd_convert)

    r = sub.add_parser("run", help="run the always-on daemon (tray + watch folder)")
    r.add_argument("--headless", action="store_true", help="no tray icon")
    r.set_defaults(fn=cmd_run)

    i = sub.add_parser("install", help="add to Startup apps + install right-click menu")
    i.set_defaults(fn=cmd_install)

    u = sub.add_parser("uninstall", help="remove startup entry + context menu")
    u.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("status", help="show detected backends & registration state")
    s.set_defaults(fn=cmd_status)

    st = sub.add_parser("stats", help="show learned conversion performance")
    st.add_argument("--limit", type=int, default=200)
    st.set_defaults(fn=cmd_stats)

    cf = sub.add_parser("config", help="show/set configuration (key=value ...)")
    cf.add_argument("assignments", nargs="*", help="e.g. ml=off auto_target=docx watch_folder=D:\\drop")
    cf.set_defaults(fn=cmd_config)

    tg = sub.add_parser("targets", help="list suggested newest targets for an extension")
    tg.add_argument("ext")
    tg.set_defaults(fn=cmd_targets)
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Explorer may pass bare file paths without a subcommand (defensive)
    if argv and not argv[0] in {"convert", "run", "install", "uninstall", "status",
                                "stats", "config", "targets", "-h", "--help"}:
        if all(Path(a).exists() or a.startswith("%") for a in argv):
            argv = ["convert"] + argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
