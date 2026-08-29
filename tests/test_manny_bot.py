"""Remote-fix routing (fix #4): device resolution + pending-remote store.

The consent token is minted on the device; the hub keeps only a routing
record so the approve button knows where to relay. These tests pin the
routing layer (resolution, record lifecycle, consume-on-approve).
"""
import json
import time

from care_agent import get_audit
import manny_bot


def test_resolve_device(monkeypatch):
    devices = {
        "m7-ultra": {"status": "enrolled", "wg_ip": "10.0.0.2"},
        "dell-inspiron": {"status": "enrolled", "wg_ip": "10.0.0.3"},
        "og-rig-dev": {"status": "enrolled", "reach": {"local": True}},
    }
    monkeypatch.setattr("fleet_check.load_devices", lambda: devices)
    assert manny_bot._resolve_device("m7")[0] == "m7-ultra"
    assert manny_bot._resolve_device("dell")[0] == "dell-inspiron"
    assert manny_bot._resolve_device("the Dell")[0] == "dell-inspiron"
    assert manny_bot._resolve_device("this computer")[0] == "og-rig-dev"
    assert manny_bot._resolve_device("M7-ULTRA")[0] == "m7-ultra"
    assert manny_bot._resolve_device("bogus")[0] is None


def test_pending_remote_roundtrip_and_prune(tmp_path, monkeypatch):
    monkeypatch.setattr(manny_bot, "PENDING_REMOTE",
                        str(tmp_path / "pending.json"))
    assert manny_bot._load_pending_remote() == {}
    manny_bot._save_pending_remote({
        "tok1": {"action": "rotate-logs", "device": "m7-ultra",
                 "expires": time.time() + 600}})
    data = manny_bot._load_pending_remote()
    assert data["tok1"]["device"] == "m7-ultra"
    # expired entries are pruned on load
    manny_bot._save_pending_remote({
        "old": {"action": "rotate-logs", "device": "m7-ultra",
                "expires": time.time() - 10}})
    data = manny_bot._load_pending_remote()
    assert "old" not in data


def test_execute_remote_relay_and_consume(tmp_path, monkeypatch, tmp_cfg):
    monkeypatch.setattr(manny_bot, "PENDING_REMOTE",
                        str(tmp_path / "pending.json"))
    monkeypatch.setattr("fleet_check.load_devices", lambda: {
        "dell-inspiron": {"reach": {"ssh_host": "10.0.0.3"}}})
    token = "abc123"
    manny_bot._save_pending_remote({
        token: {"action": "rotate-logs", "device": "dell-inspiron",
                "expires": time.time() + 600}})
    fake_out = json.dumps({"ok": True, "backup": {"backup": "/dev/x.bak"},
                           "result": {"rotated_to": "/dev/y"}})
    monkeypatch.setattr(manny_bot, "_device_ssh", lambda dev, cmd: (fake_out, 0))
    audit = get_audit(tmp_cfg["audit"]["db"])
    res = manny_bot._execute_remote(tmp_cfg, audit, "rotate-logs", token)
    assert res.get("ok") is True
    assert res.get("device") == "dell-inspiron"
    assert token not in manny_bot._load_pending_remote()  # consumed
    rows = audit.recent(5)
    assert rows and "approved fix executed (remote)" in rows[0]["reason"]


def test_execute_remote_unknown_token(tmp_path, monkeypatch, tmp_cfg):
    monkeypatch.setattr(manny_bot, "PENDING_REMOTE",
                        str(tmp_path / "pending.json"))
    audit = get_audit(tmp_cfg["audit"]["db"])
    res = manny_bot._execute_remote(tmp_cfg, audit, "rotate-logs", "nope")
    assert "error" in res
