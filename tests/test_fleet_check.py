"""extract_json + telemetry snapshots/trends (fix #5).

Trend facts are honest by construction: >= 3 reachable points required
(else silence), storage move >= 3 points to say grown/dropped, and
unreachable days are reported as exactly that.
"""
import sqlite3
import time

import pytest

import brain
import fleet_check

_SCHEMA = """CREATE TABLE IF NOT EXISTS telemetry_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, device TEXT NOT NULL,
    disk_worst_pct REAL, patches_pending INTEGER, backup_ok INTEGER,
    smart_ok INTEGER, reachable INTEGER)"""


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------
def test_extract_json_balanced():
    text = 'prefix noise\n{"a": 1, "b": {"c": [1,2,3]}}\ntrailing'
    assert fleet_check.extract_json(text) == {"a": 1, "b": {"c": [1, 2, 3]}}


def test_extract_json_unbalanced():
    with pytest.raises(ValueError):
        fleet_check.extract_json("no json here")


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
def test_snapshot_records_reachable_and_errors(tmp_path, make_tele):
    cfg = {"audit": {"db": str(tmp_path / "t.db")}}
    machines = {"hub": make_tele(), "offline": {"error": "ssh failed"}}
    fleet_check._snapshot_telemetry(cfg, machines)
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    rows = conn.execute(
        "SELECT device, disk_worst_pct, backup_ok, smart_ok, reachable "
        "FROM telemetry_snapshots ORDER BY device").fetchall()
    conn.close()
    by = {r[0]: r for r in rows}
    assert by["hub"][1] == 50 and by["hub"][4] == 1
    assert by["offline"][1] is None and by["offline"][4] == 0


def test_snapshot_unknown_checks_are_none_not_ok(tmp_path, make_tele):
    cfg = {"audit": {"db": str(tmp_path / "t.db")}}
    tele = make_tele(smart={"devices": []}, backups={"results": []})
    fleet_check._snapshot_telemetry(cfg, {"hub": tele})
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    row = conn.execute("SELECT backup_ok, smart_ok FROM telemetry_snapshots").fetchone()
    conn.close()
    assert row[0] is None and row[1] is None


def test_snapshot_stale_backup_is_not_ok(tmp_path, make_tele):
    cfg = {"audit": {"db": str(tmp_path / "t.db")}}
    tele = make_tele(backups={"results": [{"present": True, "empty": False,
                                           "stale": True}]})
    fleet_check._snapshot_telemetry(cfg, {"hub": tele})
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    row = conn.execute("SELECT backup_ok FROM telemetry_snapshots").fetchone()
    conn.close()
    assert row[0] == 0


# ---------------------------------------------------------------------------
# Trend summary
# ---------------------------------------------------------------------------
def _seed_trends(db_path, device, pcts, reachable=True):
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    now = time.time()
    for i, pct in enumerate(pcts):
        ts = now - (len(pcts) - 1 - i) * 86400
        if reachable:
            conn.execute(
                "INSERT INTO telemetry_snapshots (ts, device, disk_worst_pct, "
                "patches_pending, backup_ok, smart_ok, reachable) "
                "VALUES (?,?,?,?,?,?,1)", (ts, device, pct, 0, 1, 1))
        else:
            conn.execute(
                "INSERT INTO telemetry_snapshots (ts, device, reachable) "
                "VALUES (?,?,0)", (ts, device))
    conn.commit()
    conn.close()


def test_trend_growth(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_trends(db, "m7-ultra", [68, 71, 74, 77, 79])
    lines = fleet_check.trend_summary({"audit": {"db": db}})
    assert any("M7's storage has grown from 68% full to 79% full" in l
               for l in lines)


def test_trend_steady(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_trends(db, "dell-inspiron", [61] * 5)
    lines = fleet_check.trend_summary({"audit": {"db": db}})
    assert any("the Dell's storage has held steady around 61% full" in l
               for l in lines)


def test_trend_small_move_is_steady(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_trends(db, "m7-ultra", [70, 70, 71, 71, 72])  # delta 2 < 3
    lines = fleet_check.trend_summary({"audit": {"db": db}})
    assert any("held steady around 72% full" in l for l in lines)
    assert not any("grown" in l for l in lines)


def test_trend_sparse_is_silent(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_trends(db, "m7-ultra", [70, 72])  # only 2 points - no fabrication
    lines = fleet_check.trend_summary({"audit": {"db": db}})
    assert lines == []


def test_trend_unreachable_honest(tmp_path):
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute(_SCHEMA)
    now = time.time()
    for i in range(7):
        if i in (2, 5):
            conn.execute(
                "INSERT INTO telemetry_snapshots (ts, device, reachable) "
                "VALUES (?,?,0)", (now - (6 - i) * 86400, "og-rig-dev"))
        else:
            conn.execute(
                "INSERT INTO telemetry_snapshots (ts, device, disk_worst_pct, "
                "patches_pending, backup_ok, smart_ok, reachable) "
                "VALUES (?,?,?,?,?,?,1)",
                (now - (6 - i) * 86400, "og-rig-dev", 55, 0, 1, 1))
    conn.commit()
    conn.close()
    lines = fleet_check.trend_summary({"audit": {"db": db}})
    assert any("couldn't be reached 2 of the last 7 checks" in l for l in lines)


def test_trend_no_snapshots_is_silent(tmp_path):
    assert fleet_check.trend_summary(
        {"audit": {"db": str(tmp_path / "missing.db")}}) == []


def test_trend_lines_pass_gate(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_trends(db, "m7-ultra", [68, 71, 74, 77, 79])
    _seed_trends(db, "dell-inspiron", [61] * 5)
    lines = fleet_check.trend_summary({"audit": {"db": db}})
    assert brain.check_dictionary("\n".join(lines)) == []
