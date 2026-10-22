"""PyInstaller entry point for ConFas.exe.

Behaviour when double-clicked with NO arguments (first run):
  * installs the right-click menu entries,
  * registers itself in Startup apps (Task Manager > Startup apps),
  * starts the always-on tray daemon + watch-folder converter.
"""
import multiprocessing
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from confas.cli import main                      # noqa: E402
from confas import monitor                       # noqa: E402


def first_run_wizard() -> int:
    """Double-click experience: install everything then stay running."""
    rc = main(["install"])
    print("Starting ConFas in the background... (it will now start automatically"
          " with Windows)")
    return monitor.run_daemon(headless=False) or rc


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) == 1:
        sys.exit(first_run_wizard())
    sys.exit(main())
