r"""ConFas always-on services:

  1. Tray daemon        - runs forever, shows an icon in the notification area
                          (Task Manager > Startup apps friendly), lets you
                          pause/quit and open your watch folder.
  2. Watch-folder       - drop any document into `%USERPROFILE%/ConFasDrop` (or a folder
     watcher              you pick) and it is converted automatically to its
                          "latest generation" format using the engine + ML.
  3. Startup registration - HKCU\Software\Microsoft\Windows\CurrentVersion\Run` entry so ConFas starts with Windows
                          (appears under Task Manager -> Startup apps).
  4. Context-menu       - per-extension shell keys (HKCU\Software\Classes)
     installer            that add "Convert with ConFas" rows to the Explorer
                          right-click menu for every supported document type.
                          No admin rights required, no external .dll handlers.

Everything degrades gracefully on non-Windows systems (the pure-python parts
are still importable there for development & CI testing).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

IS_WINDOWS = sys.platform == "win32"

APP_NAME = "ConFas"
DATA_DIR = Path(os.environ.get("CONFAS_HOME", str(Path.home() / ".confas")))
DROP_DIR = Path(os.environ.get("CONFAS_DROP", str(Path.home() / "ConFasDrop")))
CONFIG_FILE = DATA_DIR / "config.json"
LOG_FILE = DATA_DIR / "daemon.log"
PID_FILE = DATA_DIR / "daemon.pid"

# extensions that get a right-click entry
MENU_EXTENSIONS = [
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
    "rtf", "txt", "md", "csv", "html", "htm", "epub",
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "avif", "heic", "jxl",
]


# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "watch_folder": str(DROP_DIR),
    "watch_enabled": True,
    "auto_target": "pdf",          # what dropped files are converted to by default
    "ml_enabled": True,
    "notify": True,
    # --- shared-learnings cloud sync (opt-in, aggregate stats only) ------ #
    "cloud_enabled": False,        # enable with: confas config cloud_sync=on
    "cloud_mode": "gist",          # "gist" (zero-backend) or "url" (own server)
    "cloud_gist_id": "REPLACE_WITH_YOUR_GIST_ID",
    "cloud_url": "",               # used when cloud_mode=url
    "cloud_interval_s": 900,       # background push/pull cadence
    "cloud_debounce_s": 60,        # min seconds between uploads
    "cloud_pull_on_start": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def exe_path() -> str:
    """Path of the running executable (frozen) or python launcher."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return sys.executable


def launch_command(args: list[str]) -> list[str]:
    """Command list that re-launches this program with given args."""
    if getattr(sys, "frozen", False):
        return [sys.executable] + args
    return [sys.executable, "-m", "confas"] + args


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Startup registration  (Task Manager > Startup apps)
# --------------------------------------------------------------------------- #

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def register_startup() -> bool:
    """Add ConFas to the current user's Run key -> shows up in Task Manager."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        cmd = subprocess.list2cmdline(launch_command(["run"]))
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
        _log(f"startup registered: {cmd}")
        return True
    except Exception as exc:
        _log(f"register_startup failed: {exc}")
        return False


def unregister_startup() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def is_startup_registered() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  Right-click context menu (per-extension HKCU shell verbs)
# --------------------------------------------------------------------------- #

def _menu_cmd(placeholders: int) -> str:
    args = ["convert"] + ["%1"] * placeholders
    return subprocess.list2cmdline(launch_command(args))


def install_context_menu(extensions: Optional[list[str]] = None) -> int:
    """Create 'Convert with ConFas' entries for each extension. Returns count."""
    if not IS_WINDOWS:
        return 0
    import winreg

    exts = extensions or MENU_EXTENSIONS
    n = 0
    for ext in exts:
        base = rf"Software\Classes\.{ext}"
        try:
            # file type may not exist in HKCU yet; create our own subkey chain
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k_ext:
                progid = winreg.QueryValue(k_ext, "")   # associated ProgID, may fail
        except FileNotFoundError:
            progid = ""
        except OSError:
            progid = ""

        # If a ProgID exists, attach under it (cleanest); else under .ext directly
        targets = []
        if progid:
            targets.append(rf"Software\Classes\{progid}\shell\ConFas.Convert")
        targets.append(base + r"\shell\ConFas.Convert")

        for t in targets:
            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, t) as k:
                    winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, "Convert with ConFas")
                    winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, exe_path() + ",0")
                    winreg.SetValueEx(k, "MultiSelectedModel", 0, winreg.REG_SZ, "ConFas")
                    with winreg.CreateKey(k, "command") as kc:
                        winreg.SetValue(kc, "", winreg.REG_SZ, _menu_cmd(1))
                n += 1
                break  # one successful attachment is enough
            except Exception as exc:
                _log(f"context menu for .{ext} failed at {t}: {exc}")
    _log(f"context menu installed for {n}/{len(exts)} extensions")
    return n


def remove_context_menu(extensions: Optional[list[str]] = None) -> int:
    if not IS_WINDOWS:
        return 0
    import winreg

    exts = extensions or MENU_EXTENSIONS
    n = 0
    for ext in exts:
        for base in (rf"Software\Classes\.{ext}",):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as k:
                    progid = winreg.QueryValue(k, "") or ""
            except FileNotFoundError:
                progid = ""
            paths = []
            if progid:
                paths.append(rf"Software\Classes\{progid}\shell\ConFas.Convert")
            paths.append(base + r"\shell\ConFas.Convert")
            for p in paths:
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, p)
                    n += 1
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
    return n


def refresh_explorer() -> None:
    """Best-effort notify Shell about changes (no kill needed, no admin)."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SHChangeNotify = ctypes.windll.shell32.SHChangeNotify  # type: ignore[attr-defined]
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Watch folder daemon
# --------------------------------------------------------------------------- #

class WatchFolder(threading.Thread):
    """Poll-based watcher (dependency-free, works from frozen EXE reliably)."""

    POLL_SECONDS = 1.0
    STABLE_CHECKS = 2   # file size must be unchanged N polls before we convert

    def __init__(self, folder: Path, cfg: dict, on_done=None) -> None:
        super().__init__(daemon=True, name="confas-watch")
        self.folder = folder
        self.cfg = cfg
        self.on_done = on_done
        self.stop_event = threading.Event()
        self._seen: dict[str, tuple[int, int]] = {}   # path -> (size, stable_count)

    def run(self) -> None:
        from .engine import newest_target_for, get_engine

        self.folder.mkdir(parents=True, exist_ok=True)
        _log(f"watching {self.folder} (auto target: {self.cfg.get('auto_target')})")
        while not self.stop_event.is_set():
            try:
                now = {}
                for p in self.folder.iterdir():
                    if p.is_file() and not p.name.startswith("~$"):
                        try:
                            now[str(p)] = p.stat().st_size
                        except OSError:
                            continue
                for path_str, size in now.items():
                    prev = self._seen.get(path_str)
                    if prev is None:
                        self._seen[path_str] = (size, 0)
                        continue
                    old_size, stable = prev
                    if size != old_size:
                        self._seen[path_str] = (size, 0)      # still being written
                        continue
                    if stable < self.STABLE_CHECKS:
                        self._seen[path_str] = (size, stable + 1)
                        continue
                    # file is complete -> convert once (skip our own outputs)
                    del self._seen[path_str]
                    src = Path(path_str)
                    out_ext = (self.cfg.get("auto_target") or "").lower()
                    if out_ext and src.stem.lower().endswith("_" + out_ext):
                        continue
                    self._convert(src, newest_target_for, get_engine)
                # forget vanished files
                for gone in set(self._seen) - set(now):
                    del self._seen[gone]
            except Exception as exc:
                _log(f"watch loop error: {exc}")
            self.stop_event.wait(self.POLL_SECONDS)

    def _convert(self, src: Path, newest_fn, engine_getter) -> None:
        target = self.cfg.get("auto_target") or "pdf"
        if src.suffix.lstrip(".").lower() == target:
            return
        eng = engine_getter(enable_ml=bool(self.cfg.get("ml_enabled", True)))
        res = eng.convert(src, target)
        _log(f"watch-converted {src.name} -> {res.target} via {res.backend} "
             f"({res.elapsed_ms:.0f} ms, ok={res.ok}{', err=' + res.error if res.error else ''})")
        if self.on_done:
            try:
                self.on_done(res)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
#  Single-instance guard
# --------------------------------------------------------------------------- #

def acquire_single_instance() -> bool:
    """Return True if this process is the only ConFas daemon."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip() or "0")
            if pid and _pid_alive(pid):
                return False
        PID_FILE.write_text(str(os.getpid()))
        return True
    except Exception:
        return True


def _pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
#  System tray daemon
# --------------------------------------------------------------------------- #

def run_daemon(headless: bool = False) -> int:
    """Main entry for `ConFas.exe run` - stays alive forever."""
    cfg = load_config()
    if not acquire_single_instance():
        _log("another instance already running; exiting")
        return 0

    DROP_DIR.mkdir(parents=True, exist_ok=True)
    Path(cfg["watch_folder"]).mkdir(parents=True, exist_ok=True)

    watch = None
    if cfg.get("watch_enabled", True):
        watch = WatchFolder(Path(cfg["watch_folder"]), cfg)
        watch.start()
        globals()["_ACTIVE_WATCH"] = watch

    if headless or not IS_WINDOWS:
        _log(f"daemon started headless (pid {os.getpid()})")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return 0

    # ---- pystray tray icon ------------------------------------------------ #
    try:
        import pystray
        from PIL import Image, ImageDraw

        def _make_icon_img():
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(37, 99, 235, 255))
            d.polygon([(20, 26), (44, 26), (32, 44)], fill=(255, 255, 255, 255))
            d.line([(20, 26), (20, 18), (44, 18), (44, 26)], fill=(255, 255, 255, 255), width=4)
            return img

        paused = {"v": False}

        def toggle_pause(icon, item):
            paused["v"] = not paused["v"]
            if watch:
                if paused["v"]:
                    watch.stop_event.set()
                else:
                    # restart a fresh watcher thread (stop_event is one-shot)
                    w2 = WatchFolder(Path(cfg["watch_folder"]), cfg)
                    w2.start()
                    globals()["_ACTIVE_WATCH"] = w2
            icon.notify(f"ConFas {'paused' if paused['v'] else 'active'}")

        def open_drop(icon, item):
            subprocess.Popen(["explorer", str(Path(cfg["watch_folder"]))])

        def quit_app(icon, item):
            icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem(lambda i: f"ConFas - {'paused' if paused['v'] else 'watching ' + cfg['watch_folder']}",
                             None, enabled=False),
            pystray.MenuItem("Pause/Resume watching", toggle_pause),
            pystray.MenuItem("Open drop folder", open_drop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit ConFas", quit_app),
        )
        icon = pystray.Icon(APP_NAME, _make_icon_img(), "ConFas - document converter", menu)
        _log("tray daemon started")
        icon.run()
        return 0
    except Exception as exc:
        _log(f"tray unavailable ({exc}); falling back to headless loop")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
