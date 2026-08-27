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
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from care_agent import load_config, get_audit
from brain import fleet_report
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


def main():
    ap = argparse.ArgumentParser(description="CareKeeper fleet check")
    ap.add_argument("--once", action="store_true",
                    help="run one fleet check and deliver")
    ap.add_argument("--no-send", action="store_true",
                    help="check + audit but do not send Telegram")
    args = ap.parse_args()

    cfg = load_config()
    audit = get_audit(cfg["audit"]["db"])
    devices = load_devices()

    machines = collect_all(cfg)

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

    # brain report (gate-checked; falls back to clean template)
    report, violations = fleet_report(cfg, machines)
    if violations:
        report, violations = fleet_report(cfg, machines)  # one retry path
    print(report)
    print(f"[dictionary check: {'PASS' if not violations else 'FAIL'}]")

    audit.log(cfg["device_id"], "report", "fleet.status", "write", True,
              "fleet report generated",
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
    except Exception as exc:
        audit.log(cfg["device_id"], "report", "telegram.fleet", "write",
                  False, f"delivery failed: {exc}")
        print(f"[delivery failed: {exc}]")


if __name__ == "__main__":
    main()
