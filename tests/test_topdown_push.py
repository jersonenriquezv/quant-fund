"""Phase 4b — top-down brief push: watch on-change diff logic."""
import json

import scripts.topdown_push as tp


def _setup(monkeypatch, states):
    """Patch the module so _poll_once is hermetic.

    states: dict pair -> {"side","confidence"} returned by build_brief_and_state.
    Returns a list that captures every message handed to _send_telegram.
    """
    sent: list[str] = []
    monkeypatch.setattr(tp, "PAIRS", list(states.keys()))
    monkeypatch.setattr(
        tp, "build_brief_and_state",
        lambda pair: (f"brief-{pair}", states[pair]),
    )
    monkeypatch.setattr(tp, "_send_telegram", lambda text, dry: sent.append(text) or True)
    return sent


def test_first_run_seeds_no_push(monkeypatch, tmp_path):
    states = {
        "BTC/USDT": {"side": "short", "confidence": "medium"},
        "ETH/USDT": {"side": "long", "confidence": "low"},
    }
    sent = _setup(monkeypatch, states)
    sf = tmp_path / "state.json"

    tp._poll_once(sf, dry_run=True)

    assert sent == []  # baseline seed never pushes
    saved = json.loads(sf.read_text())
    assert saved == states


def test_no_change_no_push(monkeypatch, tmp_path):
    states = {"BTC/USDT": {"side": "short", "confidence": "medium"}}
    sent = _setup(monkeypatch, states)
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps(states))  # pre-seeded, identical

    tp._poll_once(sf, dry_run=True)

    assert sent == []


def test_side_flip_pushes_changed_pair_only(monkeypatch, tmp_path):
    prev = {
        "BTC/USDT": {"side": "short", "confidence": "medium"},
        "ETH/USDT": {"side": "long", "confidence": "low"},
    }
    now = {
        "BTC/USDT": {"side": "long", "confidence": "high"},  # flipped
        "ETH/USDT": {"side": "long", "confidence": "low"},   # unchanged
    }
    sent = _setup(monkeypatch, now)
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps(prev))

    tp._poll_once(sf, dry_run=True)

    assert len(sent) == 1
    assert "BTC bias change" in sent[0]
    assert "SHORT → LONG" in sent[0]
    assert json.loads(sf.read_text()) == now  # state advanced


def test_confidence_change_pushes(monkeypatch, tmp_path):
    prev = {"BTC/USDT": {"side": "short", "confidence": "low"}}
    now = {"BTC/USDT": {"side": "short", "confidence": "high"}}
    sent = _setup(monkeypatch, now)
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps(prev))

    tp._poll_once(sf, dry_run=True)

    assert len(sent) == 1  # same side, confidence shift is still actionable
