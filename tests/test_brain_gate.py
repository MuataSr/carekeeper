"""The plain-language dictionary gate is the locked design rule:
the LLM proposes, deterministic code verifies. These tests pin the
gate, the honesty bullets, and the offline/final-gate paths (fixes
#1 and #2 from the Aug 29 review).
"""
import json

import pytest

import brain


# ---------------------------------------------------------------------------
# Dictionary gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "71% storage available",            # the misleading 'left' flip
    "18% free",
    "82% left",
    "20% remaining",
    "the CPU is hot",
    "the SSD has 5GB",
    "install the patch",
    "the daemon restarted",
    "partition sda1",
    "reallocated sectors: 12",
    "uptime: 200 hours",
    "temperature is 70C",
    "the drives have been running for 3 days",
    "one device has a problem",
])
def test_gate_flags_banned(text):
    assert brain.check_dictionary(text), f"should flag: {text!r}"


@pytest.mark.parametrize("text", [
    "storage is 82% full",              # honest phrasing, allowed
    "Your main storage is 92% full - that's a lot.",
    "storage is 82% full - looking healthy.",
    "There are 3 security updates waiting.",
    "Backups are fresh - your files are being looked after.",
    "The computer has been running smoothly.",
    "Drive health checks passed - the drives are in good shape.",
])
def test_gate_allows_clean(text):
    assert brain.check_dictionary(text) == [], f"should allow: {text!r}"


def test_template_report_passes_gate(make_tele):
    rep = brain._template_report(make_tele())
    assert brain.check_dictionary(rep) == []


# ---------------------------------------------------------------------------
# Honesty bullets (fix #1)
# ---------------------------------------------------------------------------
def test_patches_none_is_never_up_to_date(make_tele):
    tele = make_tele(patches={"pending": None})
    line = brain.plain_bullets({"hub": tele})[0]
    assert "security updates couldn't be checked" in line
    assert "up to date" not in line


def test_disk_none_is_honest(make_tele):
    tele = make_tele(disk={"worst_pct": None})
    line = brain.plain_bullets({"hub": tele})[0]
    assert "storage could not be checked" in line


def test_empty_backup_folder_never_fresh(make_tele):
    tele = make_tele(backups={"results": [{"path": "/x", "present": True,
                                           "empty": True,
                                           "newest_age_hours": None,
                                           "stale": False}]})
    line = brain.plain_bullets({"hub": tele})[0]
    assert "nothing backed up yet" in line


def test_missing_backup_folder(make_tele):
    tele = make_tele(backups={"results": [{"path": "/x", "present": False}]})
    line = brain.plain_bullets({"hub": tele})[0]
    assert "couldn't be found" in line


def test_smart_unknown_is_honest(make_tele):
    tele = make_tele(smart={"devices": []})
    line = brain.plain_bullets({"hub": tele})[0]
    assert "drive health couldn't be checked" in line


def test_smart_fail_flagged(make_tele):
    tele = make_tele(smart={"devices": [{"dev": "/dev/sda", "health": "FAIL"}]})
    line = brain.plain_bullets({"hub": tele})[0]
    assert "drive health needs attention" in line


def test_error_device_honest():
    lines = brain.plain_bullets({"hub": {"error": "ssh failed"}})
    assert "could not be reached" in lines[0]


# ---------------------------------------------------------------------------
# Weekly review honesty (only genuine fixes/denials count)
# ---------------------------------------------------------------------------
def _stats(**kw):
    base = {"readable": True, "checks_ok": {}, "checks_fail": {},
            "fixes": [], "denials": [], "messages": 0, "dashboards": 0}
    base.update(kw)
    return base


def test_week_fix_counts_only_genuine_fixes():
    stats = _stats(fixes=[
        ("fix.rotate-logs", "approved fix executed"),
        ("fix.rotate-logs", "approved fix executed"),
        ("fix.rotate-logs", "owner denied the fix in Telegram"),  # not a fix
    ])
    lines = brain.week_bullets(stats)
    assert "2 safe fixes were completed" in "\n".join(lines)


def test_week_denial_counts_only_owner():
    stats = _stats(denials=[
        ("fix.rotate-logs", "owner denied the fix in Telegram"),
        ("fix.rotate-logs", "approval rejected: token already used"),
    ])
    text = "\n".join(brain.week_bullets(stats))
    assert "1 fix was declined" in text
    assert "safety locks were tested and held" in text


def test_week_no_fixes_line():
    text = "\n".join(brain.week_bullets(_stats()))
    assert "No fixes were needed this week" in text


def test_week_raw_reasons_never_leak():
    stats = _stats(denials=[
        ("fix.rotate-logs", "approval rejected: token already used")])
    text = "\n".join(brain.week_bullets(stats))
    assert "token already used" not in text
    assert "rejected" not in text


def test_week_unreadable_logbook_is_honest():
    lines = brain.week_bullets(_stats(readable=False))
    assert "couldn't be read" in "\n".join(lines)


# ---------------------------------------------------------------------------
# Offline note + final gate (fix #2)
# ---------------------------------------------------------------------------
def test_offline_note_static_and_clean():
    class Evil(Exception):
        pass

    note = brain._offline_note(Evil("CPU throttling SSD /dev/sda at /home/x"))
    assert note == "[brain was offline - this report used the backup template]"
    assert brain.check_dictionary(note) == []


def test_final_gate_drops_dirty():
    assert brain._final_gate("storage is fine and the CPU temp is 70C") == brain._SAFE_MIN


def test_final_gate_passes_clean():
    clean = "storage is 82% full - looking healthy."
    assert brain._final_gate(clean) == clean


# ---------------------------------------------------------------------------
# Gate retry-once, then guaranteed-clean template (fleet_report)
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, content):
        self._content = content

    def read(self):
        return json.dumps(
            {"choices": [{"message": {"content": self._content}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fleet_report_offline_template(tmp_cfg, monkeypatch, make_tele):
    def boom(*a, **k):
        raise ConnectionError("Connection refused CPU SSD")
    monkeypatch.setattr(brain.urllib.request, "urlopen", boom)
    report, viol = brain.fleet_report(tmp_cfg, {"hub": make_tele()})
    assert viol == []
    assert "brain was offline" in report
    assert "Connection refused" not in report      # exception never ships
    assert brain.check_dictionary(report) == []


def test_fleet_report_retry_once_then_clean(tmp_cfg, monkeypatch, make_tele):
    calls = []

    def fake_urlopen(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return FakeResp("storage is 82% full and the CPU is hot")
        return FakeResp("storage is 82% full - the computer is working hard")
    monkeypatch.setattr(brain.urllib.request, "urlopen", fake_urlopen)
    report, viol = brain.fleet_report(tmp_cfg, {"hub": make_tele()})
    assert len(calls) == 2                          # exactly one retry
    assert viol == []
    assert brain.check_dictionary(report) == []


def test_fleet_report_persistent_violations_use_template(tmp_cfg, monkeypatch, make_tele):
    def fake_urlopen(*a, **k):
        return FakeResp("CPU SSD RAM partition uptime malware patch")
    monkeypatch.setattr(brain.urllib.request, "urlopen", fake_urlopen)
    report, viol = brain.fleet_report(tmp_cfg, {"hub": make_tele()})
    assert viol == []
    assert "storage" in report                      # template facts present
    assert brain.check_dictionary(report) == []
