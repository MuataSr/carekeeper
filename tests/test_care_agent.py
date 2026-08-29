"""Consent-gated fixes + hash-chained audit (the trust contract).

Every Class 1 write needs a single-use approval token (10-min TTL,
replay-denied, action-bound); watch tier disables the executor; the
audit trail is name-keyed and tamper-evident (chain breaks are
detectable).
"""
import sqlite3

import pytest

import care_agent


# ---------------------------------------------------------------------------
# Approval tokens
# ---------------------------------------------------------------------------
def test_propose_mints_single_use_token(tmp_cfg):
    audit = care_agent.get_audit(tmp_cfg["audit"]["db"])
    res = care_agent.propose_fix(tmp_cfg, audit, "rotate-logs")
    assert "token" in res and "error" not in res
    store = care_agent.ApprovalStore(tmp_cfg["state_dir"])
    assert res["token"] in store._load()


def test_execute_rejects_missing_token(tmp_cfg):
    audit = care_agent.get_audit(tmp_cfg["audit"]["db"])
    res = care_agent.execute_fix(tmp_cfg, audit, "rotate-logs", "deadbeef")
    assert "error" in res and "unknown token" in res["error"]


def test_token_single_use_replay_denied(tmp_cfg):
    audit = care_agent.get_audit(tmp_cfg["audit"]["db"])
    p = care_agent.propose_fix(tmp_cfg, audit, "rotate-logs")
    ok1 = care_agent.execute_fix(tmp_cfg, audit, "rotate-logs", p["token"])
    assert ok1.get("ok") is True
    ok2 = care_agent.execute_fix(tmp_cfg, audit, "rotate-logs", p["token"])
    assert "error" in ok2 and "already used" in ok2["error"]


def test_token_bound_to_action(tmp_cfg, monkeypatch):
    monkeypatch.setitem(care_agent.ACTIONS, "second-action", {
        "class": 1, "desc": "test", "backup": lambda c: {"backup": "x"},
        "exec": lambda c: {"ok": True}})
    audit = care_agent.get_audit(tmp_cfg["audit"]["db"])
    p = care_agent.propose_fix(tmp_cfg, audit, "rotate-logs")
    res = care_agent.execute_fix(tmp_cfg, audit, "second-action", p["token"])
    assert "error" in res and "not" in res["error"]  # token is for rotate-logs


def test_token_expiry(tmp_cfg):
    audit = care_agent.get_audit(tmp_cfg["audit"]["db"])
    token = care_agent.ApprovalStore(tmp_cfg["state_dir"]).mint(
        "rotate-logs", ttl_s=-1)
    res = care_agent.execute_fix(tmp_cfg, audit, "rotate-logs", token)
    assert "error" in res and "expired" in res["error"]


def test_watch_tier_denies_fixes(tmp_cfg):
    tmp_cfg["tier"] = "watch"
    audit = care_agent.get_audit(tmp_cfg["audit"]["db"])
    res = care_agent.propose_fix(tmp_cfg, audit, "rotate-logs")
    assert "error" in res and "watch tier" in res["error"]


def test_fix_backs_up_before_writing(tmp_cfg):
    audit = care_agent.get_audit(tmp_cfg["audit"]["db"])
    p = care_agent.propose_fix(tmp_cfg, audit, "rotate-logs")
    res = care_agent.execute_fix(tmp_cfg, audit, "rotate-logs", p["token"])
    assert res.get("ok") is True
    assert "backup" in res and res["backup"].get("backup", "").endswith(".bak")
    # the rotated scratch log exists in state
    import glob
    import os
    assert glob.glob(os.path.join(tmp_cfg["state_dir"], "scratch.log.rotated.*"))


# ---------------------------------------------------------------------------
# Audit trail (local twin - regression for the PRAGMA d[1] fix)
# ---------------------------------------------------------------------------
def test_audit_recent_has_named_keys(tmp_path):
    trail = care_agent._LocalAuditTrail(str(tmp_path / "audit.db"))
    trail.log("agent", "tool", "fix.rotate-logs", "write", True, "ok")
    rows = trail.recent(10)
    assert rows and "timestamp" in rows[0]
    assert rows[0]["resource_name"] == "fix.rotate-logs"
    assert rows[0]["allowed"] == 1


def test_audit_chain_integrity(tmp_path):
    trail = care_agent._LocalAuditTrail(str(tmp_path / "audit.db"))
    for i in range(5):
        trail.log("agent", "tool", f"fix.{i}", "call", True, f"row {i}")
    ok, why = trail.verify_chain()
    assert ok, why
    # deleting a middle row must break the chain
    conn = sqlite3.connect(str(tmp_path / "audit.db"))
    conn.execute("DELETE FROM audit_chain WHERE row_id = 3")
    conn.commit()
    conn.close()
    ok, why = trail.verify_chain()
    assert not ok


def test_audit_deny_logged(tmp_cfg):
    audit = care_agent.get_audit(tmp_cfg["audit"]["db"])
    care_agent.execute_fix(tmp_cfg, audit, "rotate-logs", "badtoken")
    rows = audit.recent(5)
    assert rows and rows[0]["allowed"] == 0
