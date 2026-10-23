import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
_tmp = Path(__file__).parent / "_tmp_home"
os.environ["CONFAS_HOME"] = str(_tmp)
os.environ["CONFAS_DROP"] = str(_tmp / "drop")

from confas import monitor  # noqa: E402
from confas.monitor import WatchFolder  # noqa: E402


def test_config_roundtrip():
    cfg = monitor.load_config()
    cfg["auto_target"] = "docx"
    monitor.save_config(cfg)
    assert monitor.load_config()["auto_target"] == "docx"


def test_watch_folder_converts(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "hello.txt").write_text("line one\nline two\n", encoding="utf-8")
    done = []
    w = WatchFolder(drop, {"auto_target": "docx", "ml_enabled": False},
                    on_done=done.append)
    w.start()
    deadline = time.time() + 15
    while time.time() < deadline and not done:
        time.sleep(0.3)
    w.stop_event.set()
    assert done, "watcher did not convert the dropped file"
    assert done[0].ok, done[0].error
    assert (drop / "hello.docx").exists()


def test_nonwindows_no_registry_calls():
    if not monitor.IS_WINDOWS:
        assert monitor.register_startup() is False
        assert monitor.install_context_menu() == 0
