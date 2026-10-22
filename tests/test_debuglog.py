"""Tests for the full debugging layer (debuglog)."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / ".confas"
    monkeypatch.setenv("CONFAS_HOME", str(h))
    # reload monitor so DATA_DIR picks up env var
    for m in ("confas.monitor", "confas.engine", "confas.debuglog"):
        sys.modules.pop(m, None)
    from confas import monitor, debuglog
    return h, monitor, debuglog


def test_success_writes_report_next_to_output(home):
    h, monitor, debuglog = home
    src = Path(monitor.__file__).parent.parent.parent / "README.md"
    assert src.exists()
    from confas.engine import ConversionEngine
    eng = ConversionEngine(enable_ml=False)
    out_dir = h.parent / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    res = eng.convert(src, "txt", out_dir=out_dir)
    assert res.ok, res.error
    rep = Path(res.target + ".confas-debug.txt")
    assert rep.exists(), "debug txt must appear next to converted file"
    text = rep.read_text(encoding="utf-8")
    assert "ConFas CONVERSION DEBUG REPORT" in text
    assert "SUCCESS" in text
    assert "What went RIGHT" in text
    assert "[CANDIDATE CHAIN & ATTEMPTS]" in text
    assert "+ SUCCESS" in text
    # central copy too
    central = list((h / "debug").glob("*-OK.confas-debug.txt"))
    assert central, "central copy stored under ~/.confas/debug/"


def test_failure_report_shows_traceback(home):
    h, monitor, debuglog = home
    # corrupt pdf -> all backends should fail (no libreoffice/office here)
    bad = h.parent / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 not a real pdf at all \x00\x01garbage")
    from confas.engine import ConversionEngine
    eng = ConversionEngine(enable_ml=False)
    res = eng.convert(bad, "docx")
    if res.ok:  # environment with real backends may succeed; skip then
        pytest.skip("a backend succeeded on this platform")
    reps = list((h / "debug").glob("*-FAIL.confas-debug.txt"))
    assert reps, "failure must still produce a report"
    text = reps[-1].read_text(encoding="utf-8")
    assert "FAILURE" in text
    assert "What went WRONG" in text
    assert "error:" in text or "FAILED after" in text


def test_mode_off_suppresses(home, monkeypatch):
    h, monitor, debuglog = home
    monkeypatch.setenv("CONFAS_DEBUG_REPORTS", "off")
    cfgdir = h
    cfgdir.mkdir(parents=True, exist_ok=True)
    from confas.engine import ConversionEngine
    eng = ConversionEngine(enable_ml=False)
    src = Path(monitor.__file__).parent.parent.parent / "README.md"
    res = eng.convert(src, "txt", out_dir=h.parent / "o2")
    assert res.ok
    assert not (Path(res.target).with_name(Path(res.target).name + ".confas-debug.txt")).exists()
    assert not (h / "debug").exists() or not list((h / "debug").glob("*.txt"))


def test_mode_onerror_only_failures(home, monkeypatch):
    h, monitor, debuglog = home
    monkeypatch.setenv("CONFAS_DEBUG_REPORTS", "onerror")
    from confas.engine import ConversionEngine
    eng = ConversionEngine(enable_ml=False)
    src = Path(monitor.__file__).parent.parent.parent / "README.md"
    res = eng.convert(src, "txt", out_dir=h.parent / "o3")
    assert res.ok
    assert not list((h / "debug").glob("*.txt")) if (h / "debug").exists() else True


def test_report_never_breaks_conversion(home, monkeypatch):
    """Even if rendering/writing explodes, conversion still succeeds."""
    h, monitor, debuglog = home
    monkeypatch.setattr(debuglog, "render_report",
                        lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    from confas.engine import ConversionEngine
    eng = ConversionEngine(enable_ml=False)
    src = Path(monitor.__file__).parent.parent.parent / "README.md"
    res = eng.convert(src, "txt", out_dir=h.parent / "o4")
    assert res.ok, "conversion must be unaffected by broken debug writer"


def test_ml_scores_recorded(home):
    h, monitor, debuglog = home
    from confas.engine import ConversionEngine
    eng = ConversionEngine(enable_ml=True, history_file=h / "history.jsonl")
    src = Path(monitor.__file__).parent.parent.parent / "README.md"
    res = eng.convert(src, "txt", out_dir=h.parent / "o5")
    assert res.ok
    rep = Path(res.target + ".confas-debug.txt")
    text = rep.read_text(encoding="utf-8")
    assert "[ML BACKEND SELECTION]" in text
    assert "enabled        : True" in text


def test_retention_pruning(home, monkeypatch):
    h, monitor, debuglog = home
    dd = h / "debug"
    dd.mkdir(parents=True, exist_ok=True)
    for i in range(25):
        f = dd / f"old{i:02d}.confas-debug.txt"
        f.write_text("x")
        os.utime(f, (i, i))  # distinct old mtimes
    monkeypatch.setattr(debuglog, "keep_count", lambda: 10)
    sess = {
        "run_id": "t", "t_start": __import__("time").time(), "src": "a.txt",
        "dst_ext": ".docx", "planned_out": "", "out_path": "",
        "backends": {}, "ml": {"enabled": False}, "routes": [], "attempts": [],
        "ok": True, "winning_backend": "x", "total_ms": 1.0,
        "data_dir": str(h),
    }
    debuglog.write_report(sess)
    remaining = list(dd.glob("*.confas-debug.txt"))
    assert len(remaining) <= 11  # 10 kept + fresh one
