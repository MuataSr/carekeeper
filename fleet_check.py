#!/usr/bin/env python3
"""
CareKeeper - fleet_check.py (v1)

Hub-side fleet health check: pull telemetry from every enrolled device
(over WireGuard SSH, or locally for the hub box), run the brain for one
plain-language family report, audit everything, and deliver via Manny.

Usage:
  fleet_check.py --once     run one fleet check now (timer-friendly)
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from care_agent import load_config, get_audit
from brain import fleet_report, weekly_report, FRIENDLY_NAMES
from manny_bot import TelegramBot

REGISTRY = os.path.join(BASE, "state", "devices.json")
TOKEN_PATH = os.path.join(BASE, "state", "bot_token.txt")

# How to reach each device: registry entries get a "reach" map at enroll
# time; local devices use {"local": true}. Defaults for our fleet:
REACH_DEFAULTS = {
    "m7-ultra": {"ssh_host": "m7-ultra", "user": "muatasr",
                 "path": "~/carekeeper/care_agent.py"},
}


def load_devices() -> dict:
    reg = json.load(open(REGISTRY)) if os.path.exists(REGISTRY) else {}
    return reg.get("devices", {})


def _run(cmd, timeout=60):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
        return out.stdout.strip(), out.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"error: {exc}", -1


def extract_json(text: str) -> dict:
    """Pull the first balanced {...} object out of command output."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON in output")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON")


def collect_local(cfg) -> dict:
    out, rc = _run([sys.executable, os.path.join(BASE, "care_agent.py"),
                    "--check"], timeout=90)
    if rc != 0:
        return {"error": out[-200:]}
    try:
        return extract_json(out)
    except ValueError as exc:
        return {"error": f"bad telemetry: {exc}"}


def collect_remote(dev: dict) -> dict:
    reach = dev.get("reach", {})
    host = reach.get("ssh_host") or dev.get("host")
    path = reach.get("path", "~/carekeeper/care_agent.py")
    user = reach.get("user", "")
    target = f"{user}@{host}" if user else host
    out, rc = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                    target, f"python3 {path} --check"], timeout=90)
    if rc != 0:
        return {"error": (out or "ssh failed")[-200:]}
    try:
        return extract_json(out)
    except ValueError as exc:
        return {"error": f"bad telemetry: {exc}"}


def collect_all(cfg) -> dict:
    devices = load_devices()
    machines = {}
    for name, dev in devices.items():
        if dev.get("status") != "enrolled":
            machines[name] = {"error": "not enrolled yet"}
            continue
        if dev.get("reach", {}).get("local"):
            machines[name] = collect_local(cfg)
        else:
            machines[name] = collect_remote(dev)
    # the hub itself always reports (og-rig-dev)
    if "og-rig-dev" not in machines:
        machines["og-rig-dev"] = collect_local(cfg)
    return machines


def week_audit_stats(cfg) -> dict:
    """Summarize the last 7 days of hub audit events (read-only).

    Reads the same hash-chained audit DB the daily checks write to.
    Never fabricates: if the logbook can't be read, readable=False and
    the weekly report says so honestly.
    """
    db = cfg["audit"]["db"]
    since = time.time() - 7 * 86400
    stats = {"readable": True, "checks_ok": {}, "checks_fail": {},
             "fixes": [], "denials": [], "messages": 0, "dashboards": 0}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT resource_type, resource_name, allowed, reason "
            "FROM audit_chain WHERE timestamp >= ?", (since,)).fetchall()
        conn.close()
    except Exception:
        stats["readable"] = False
        return stats
    for r in rows:
        rt, rn, ok = r["resource_type"], r["resource_name"], bool(r["allowed"])
        if rt == "telemetry" and rn.startswith("device."):
            dev = rn.split(".", 1)[1]
            bucket = stats["checks_ok"] if ok else stats["checks_fail"]
            bucket[dev] = bucket.get(dev, 0) + 1
        elif rt == "tool":
            (stats["fixes"] if ok else stats["denials"]).append(
                (rn, r["reason"]))
        elif rt == "report" and rn == "telegram.fleet" and ok:
            stats["messages"] += 1
        elif rt == "report" and rn == "telegram.dashboard" and ok:
            stats["dashboards"] += 1
    return stats


def _snapshot_telemetry(cfg, machines: dict):
    """Persist one row per device for trend history (hub audit DB).

    Called on every fleet check (daily + weekly). The snapshots table is
    a plain history store - NOT part of the hash chain. Pruned at 90 days.
    A snapshot failure must never fail the fleet check itself.
    """
    db = cfg["audit"]["db"]
    try:
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS telemetry_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                device TEXT NOT NULL,
                disk_worst_pct REAL,
                patches_pending INTEGER,
                backup_ok INTEGER,
                smart_ok INTEGER,
                reachable INTEGER)""")
        now = time.time()
        for dev, tele in machines.items():
            if isinstance(tele, dict) and "error" in tele:
                conn.execute(
                    "INSERT INTO telemetry_snapshots (ts, device, reachable) "
                    "VALUES (?,?,0)", (now, dev))
                continue
            disk = tele.get("disk", {}).get("worst_pct")
            patches = tele.get("patches", {}).get("pending")
            bres = tele.get("backups", {}).get("results", [])
            if bres and all(b.get("present") and not b.get("empty")
                            and not b.get("stale") for b in bres):
                backup_ok = 1
            elif bres:
                backup_ok = 0
            else:
                backup_ok = None  # no backup configured - unknown, not 'ok'
            smart = tele.get("smart", {}).get("devices", [])
            if smart and any(s.get("health") == "FAIL" for s in smart):
                smart_ok = 0
            elif smart:
                smart_ok = 1
            else:
                smart_ok = None  # couldn't check - unknown, not 'ok'
            conn.execute(
                "INSERT INTO telemetry_snapshots (ts, device, disk_worst_pct, "
                "patches_pending, backup_ok, smart_ok, reachable) "
                "VALUES (?,?,?,?,?,?,1)",
                (now, dev, disk, patches, backup_ok, smart_ok))
        conn.execute("DELETE FROM telemetry_snapshots WHERE ts < ?",
                     (now - 90 * 86400,))
        conn.commit()
        conn.close()
    except Exception:
        pass  # history is best-effort; the report still ships


def trend_summary(cfg, window_days: int = 14) -> list:
    """Deterministic plain-language trend facts from stored snapshots.

    Honesty rules: needs >= 3 reachable snapshots per device (fewer =
    say nothing - never fabricate a trend); storage trend only when the
    move is >= 3 percentage points ('grown'/'dropped'), otherwise 'held
    steady'; unreachable days are reported as exactly that. Output is
    clean by construction (passes the dictionary gate).
    """
    db = cfg["audit"]["db"]
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT device, ts, disk_worst_pct, patches_pending, backup_ok, "
            "smart_ok, reachable FROM telemetry_snapshots WHERE ts >= ? "
            "ORDER BY device, ts", (time.time() - window_days * 86400,)
        ).fetchall()
        conn.close()
    except Exception:
        return []  # no history yet - honest silence
    per = {}
    for dev, ts, disk, _patches, _bak, _smart, reach in rows:
        per.setdefault(dev, []).append((ts, disk, reach))
    lines = []
    for dev, pts in sorted(per.items()):
        name = FRIENDLY_NAMES.get(dev, dev)
        reachable = [p for p in pts if p[2] == 1]
        if len(reachable) < 3:
            continue  # not enough history - do not fabricate
        disks = [(ts, d) for ts, d, _r in reachable if d is not None]
        if len(disks) >= 3:
            first_pct, last_pct = disks[0][1], disks[-1][1]
            delta = last_pct - first_pct
            def _pct(v):
                return f"{v:g}"  # 61.0 -> "61", 61.5 -> "61.5"
            if abs(delta) >= 3:
                direction = "grown" if delta > 0 else "dropped"
                lines.append(
                    f"{name}'s storage has {direction} from {_pct(first_pct)}% "
                    f"full to {_pct(last_pct)}% full over the last "
                    f"{window_days} days.")
            else:
                lines.append(
                    f"{name}'s storage has held steady around "
                    f"{_pct(last_pct)}% full.")
        unreachable = len(pts) - len(reachable)
        if unreachable and len(pts) >= 3:
            lines.append(
                f"{name} couldn't be reached {unreachable} of the "
                f"last {len(pts)} checks.")
    return lines


def main():
    ap = argparse.ArgumentParser(description="CareKeeper fleet check")
    ap.add_argument("--once", action="store_true",
                    help="run one fleet check and deliver")
    ap.add_argument("--weekly", action="store_true",
                    help="run the weekly fleet review and deliver")
    ap.add_argument("--persona", metavar="NAME",
                    help="override the persona for this run "
                         "(manny|steady|sage|guardian|tidy)")
    ap.add_argument("--no-send", action="store_true",
                    help="check + audit but do not send Telegram")
    args = ap.parse_args()

    cfg = load_config()
    if args.persona:
        cfg["persona"] = args.persona
    audit = get_audit(cfg["audit"]["db"])
    devices = load_devices()

    machines = collect_all(cfg)

    # persist today's telemetry for trend history (both paths)
    _snapshot_telemetry(cfg, machines)

    # audit each device check
    for name, tele in machines.items():
        if isinstance(tele, dict) and "error" in tele:
            audit.log(cfg["device_id"], "telemetry", f"device.{name}",
                      "read", False, f"check failed: {tele['error'][:80]}")
        else:
            audit.log(cfg["device_id"], "telemetry", f"device.{name}",
                      "read", True, "fleet check ok",
                      {"disk_pct": tele.get("disk", {}).get("worst_pct"),
                       "patches": tele.get("patches", {}).get("pending")})

    # brain report (gate-checked internally: retry once, then clean template)
    if args.weekly:
        stats = week_audit_stats(cfg)
        trends = trend_summary(cfg)
        report, violations = weekly_report(cfg, machines, stats, trends)
    else:
        report, violations = fleet_report(cfg, machines)
    print(report)
    print(f"[dictionary check: {'PASS' if not violations else 'FAIL'}]")

    audit.log(cfg["device_id"], "report",
              "fleet.weekly" if args.weekly else "fleet.status",
              "write", True,
              "fleet weekly review generated" if args.weekly
              else "fleet report generated",
              {"violations": violations, "devices": len(machines)})

    if args.no_send:
        print("[no-send mode: report not delivered]")
        return

    try:
        token = open(TOKEN_PATH).read().strip()
        bot = TelegramBot(token, cfg["telegram"]["chat_id"])
        bot.send(cfg["telegram"]["chat_id"], report)
        audit.log(cfg["device_id"], "report", "telegram.fleet", "write", True,
                  "fleet report delivered via Manny")
        print("[delivered via Manny]")
        # attach the dashboard picture right after the text report
        try:
            from dashboard import render, DASH_PATH
            png = render(machines, cfg)
            bot.send_photo(cfg["telegram"]["chat_id"], png,
                           caption="Family fleet at a glance")
            audit.log(cfg["device_id"], "report", "telegram.dashboard",
                      "write", True, "dashboard picture delivered",
                      {"bytes": os.path.getsize(png)})
            print("[dashboard picture delivered]")
        except Exception as exc:
            audit.log(cfg["device_id"], "report", "telegram.dashboard",
                      "write", False, f"dashboard delivery failed: {exc}")
            print(f"[dashboard picture failed: {exc}]")
    except Exception as exc:
        audit.log(cfg["device_id"], "report", "telegram.fleet", "write",
                  False, f"delivery failed: {exc}")
        print(f"[delivery failed: {exc}]")
        # unattended service: a missed delivery must be visible to systemd
        # (OnFailure=carekeeper-alert.service), so exit non-zero.
        sys.exit(1)


if __name__ == "__main__":
    main()
