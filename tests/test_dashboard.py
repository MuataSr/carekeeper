"""Dashboard honesty (fix #1): an unrun check is NEVER 'up to date'.

status_of/row_text treat None checks as unknown (gray), and real
problems (full disk, failing drive, stale/missing backups, pending
updates) always outrank an unknown check.
"""
import pytest

PIL = pytest.importorskip("PIL")  # dashboard imports PIL at module load

from dashboard import status_of, row_text, GRAY, GREEN, AMBER, RED  # noqa: E402


def test_patches_none_shows_unknown(make_tele):
    label, color = status_of(make_tele(patches={"pending": None}))
    assert label == "update check couldn't run"
    assert color == GRAY


def test_disk_none_shows_unknown(make_tele):
    label, color = status_of(make_tele(disk={"worst_pct": None}))
    assert label == "storage couldn't be checked"
    assert color == GRAY


def test_healthy_is_green(make_tele):
    label, color = status_of(make_tele())
    assert color == GREEN
    assert "up to date" in label


def test_full_disk_outranks_unknown(make_tele):
    tele = make_tele(disk={"worst_pct": 95}, patches={"pending": None})
    label, color = status_of(tele)
    assert label == "your main drive is nearly full"
    assert color == RED


def test_warn_disk_outranks_unknown(make_tele):
    tele = make_tele(disk={"worst_pct": 88}, patches={"pending": None})
    label, color = status_of(tele)
    assert label == "your main drive is getting full"
    assert color == AMBER


def test_stale_backup_outranks_unknown(make_tele):
    tele = make_tele(backups={"results": [{"present": True, "empty": False,
                                           "stale": True}]},
                     patches={"pending": None})
    label, color = status_of(tele)
    assert label == "a backup is behind"
    assert color == AMBER


def test_empty_backup_amber(make_tele):
    tele = make_tele(backups={"results": [{"present": True, "empty": True,
                                           "stale": False}]})
    label, color = status_of(tele)
    assert color == AMBER


def test_pending_updates_amber(make_tele):
    label, color = status_of(make_tele(patches={"pending": 3}))
    assert color == AMBER
    assert "waiting on your OK" in label


def test_smart_fail_red(make_tele):
    tele = make_tele(smart={"devices": [{"dev": "/dev/sda", "health": "FAIL"}]})
    label, color = status_of(tele)
    assert color == RED


def test_unreachable_gray():
    label, color = status_of({"error": "ssh failed"})
    assert color == GRAY
    assert "couldn't be reached" in label


def test_row_text_matches_status_semantics(make_tele):
    _, c1 = row_text(make_tele(patches={"pending": None}))
    assert c1 == GRAY
    _, c2 = row_text(make_tele(disk={"worst_pct": 95}))
    assert c2 == RED
