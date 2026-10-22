#!/usr/bin/env python3
"""One-shot builder: creates the Windows EXE with PyInstaller.

Run on Windows:      python build.py
Produces:            dist/ConFas.exe   (single file, windowed/no-console)

The same script sanity-checks imports before freezing so you get an early,
clear error instead of a broken EXE.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
SPEC = ROOT / "confas.spec"


def check_imports() -> None:
    sys.path.insert(0, str(SRC))
    import confas.engine, confas.learning, confas.monitor, confas.cli  # noqa: F401
    print("imports OK")


def build() -> int:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "ConFas",
        "--onefile",
        "--noconsole",                      # GUI subsystem: no black console window
        "--specpath", str(ROOT),
        "--paths", str(SRC),
        "--collect-all", "pdf2docx",
        "--hidden-import", "win32com.client",
        "--hidden-import", "pythoncom",
        "--hidden-import", "pywintypes",
        "--hidden-import", "confas.engine",
        "--hidden-import", "confas.learning",
        "--hidden-import", "confas.monitor",
    ]
    icon = ROOT / "assets" / "confas.ico"
    if icon.exists():
        args += ["--icon", str(icon)]
    args.append(str(ROOT / "entry_point.py"))
    print("$", " ".join(args))
    return subprocess.call(args)


if __name__ == "__main__":
    check_imports()
    raise SystemExit(build())
