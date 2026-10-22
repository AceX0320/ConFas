"""Tests for the shared-learnings cloud sync module."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from confas import cloud  # noqa: E402
from confas.learning import BanditSelector, _features, DIM  # noqa: E402


def _mk_state(seed, n_updates=8, arm="pdf>docx|libreoffice"):
    rng = np.random.default_rng(seed)
    b = {"version": 1, "clock": seed, "bandit": {}, "median_ms": {arm.split("|")[0]: 500.0 + seed}}
    A = np.eye(DIM) * 8.0
    v = np.zeros(DIM)
    for i in range(n_updates):
        x = rng.normal(size=DIM) * 0.1
        x[0] = 1.0
        r = float(rng.normal(0.5, 0.2))
        A += np.outer(x, x)
        v += r * x
    b["bandit"][arm] = {"A": A.tolist(), "b": v.tolist(), "n": n_updates}
    return b


def test_merge_states_combines():
    s1, s2 = _mk_state(1), _mk_state(2)
    m = cloud.merge_states([s1, s2])
    arm = "pdf>docx|libreoffice"
    assert m["bandit"][arm]["n"] == 16                      # samples summed
    assert m["clock"] == max(s1["clock"], s2["clock"]) + 1  # Lamport bump
    A = np.array(m["bandit"][arm]["A"])
    assert A.shape == (DIM, DIM)
    # averaged A keeps ridge prior scale
    assert float(A[0, 0]) > 8.0 and float(A[0, 0]) <= 24.0  # pooled evidence sharpens the prior
    # median min wins
    assert m["median_ms"]["pdf>docx"] == 501.0


def test_merge_empty_and_bad():
    m = cloud.merge_states([])
    assert m["bandit"] == {} and m["median_ms"] == {}
    m = cloud.merge_states([{"garbage": True}, None, _mk_state(3)])
    assert m["bandit"], "valid contributor still merged"


def test_selector_records_and_warms(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFAS_HOME", str(tmp_path))
    sel = BanditSelector(history_file=tmp_path / "history.jsonl")
    src = tmp_path / "a.pdf"
    src.write_bytes(b"x" * 100)
    sel.record(src, ".docx", "libreoffice", 400.0, True)
    sel.record(src, ".docx", "pdf2docx", 900.0, False)
    st = json.loads(sel.state_file.read_text())
    assert st["bandit"]["pdf>docx|libreoffice"]["n"] == 1
    # a fresh selector loads persisted state
    sel2 = BanditSelector(history_file=tmp_path / "history.jsonl")
    assert sel2.bandit.n.get("pdf>docx|libreoffice") == 1
    # simulate warm download: merge another user's pooled state into ours
    pooled = _mk_state(7, n_updates=20, arm="pdf>docx|pdf2docx")
    sel2.bandit.import_state(pooled["bandit"])
    assert sel2.bandit.n["pdf>docx|pdf2docx"] == 20   # starts warm, no cold exploration


def test_cloudsync_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFAS_HOME", str(tmp_path))
    from confas.monitor import load_config
    sel = BanditSelector(history_file=tmp_path / "history.jsonl")
    cs = cloud.CloudSync(sel, load_config())
    assert not cs.enabled()               # opt-in only
    cs.start()                            # must be a harmless no-op
    cs.stop()


def test_cloudsync_pull_merges_local(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFAS_HOME", str(tmp_path))
    sel = BanditSelector(history_file=tmp_path / "history.jsonl")
    cfg = {"cloud_enabled": True, "cloud_mode": "url",
           "cloud_url": "http://example.invalid"}
    cs = cloud.CloudSync(sel, cfg)
    assert cs.enabled()
    pooled = _mk_state(5, n_updates=12)
    monkeypatch.setattr(cloud, "_request", lambda *a, **k: pooled)
    assert cs.pull() is True
    assert sel.bandit.n["pdf>docx|libreoffice"] == 12
    assert sel.median_ms["pdf>docx"] == 505.0
    # persisted -> survives restart
    sel2 = BanditSelector(history_file=tmp_path / "history.jsonl")
    assert sel2.bandit.n["pdf>docx|libreoffice"] == 12


def test_push_payload_has_no_filenames(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFAS_HOME", str(tmp_path))
    sel = BanditSelector(history_file=tmp_path / "history.jsonl")
    src = tmp_path / "secret_report.pdf"
    src.write_bytes(b"x" * 10)
    sel.record(src, ".docx", "libreoffice", 100.0, True)
    cs = cloud.CloudSync(sel, {"cloud_enabled": True, "cloud_mode": "url",
                               "cloud_url": "http://example.invalid"})
    payload = json.dumps(cs.build_payload())
    assert "secret_report" not in payload      # privacy: aggregate numbers only
    assert "tmp_path" not in payload or str(tmp_path) not in payload
    captured = {}
    def fake_req(url, method="GET", body=None, headers=None, timeout=15):
        captured["body"] = body
        return {}
    monkeypatch.setattr(cloud, "_request", fake_req)
    assert cs.push()
    assert captured["body"]["bandit"]          # what the server receives
    assert "file" not in json.dumps(captured["body"])


def test_engine_attaches_cloud(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFAS_HOME", str(tmp_path))
    from confas.engine import ConversionEngine
    eng = ConversionEngine(enable_ml=True, history_file=tmp_path / "history.jsonl")
    assert eng._selector is not None
    assert eng.cloud is not None               # CloudSync object exists...
    assert not eng.cloud.enabled()             # ...but stays off unless configured
    eng.shutdown()
