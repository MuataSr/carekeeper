#!/usr/bin/env python3
"""
Rig Keeper - care_agent.py (v1, Phase 0)

The on-device caretaker: reads health telemetry (watch-only, Class 0),
proposes safe fixes, and executes ONLY with a single-use approval token
(Class 1). Every write action is backed up first and appended to a
hash-chained audit trail.

One binary, tier-gated: tier=watch disables the fix executor entirely.

Zero third-party dependencies (Python stdlib only). The brain (plain-
language reporting) lives in brain.py and talks to a local llama-server.
"""
import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------
HOME = os.path.expanduser("~")
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.yaml")

DEFAULTS = {
    "device_id": "dev-box",
    "tier": "full",
    "audit": {"db": os.path.join(BASE, "audit", "rig-keeper.db")},
    "state_dir": os.path.join(BASE, "state"),
    "backup_dir": os.path.join(BASE, "backups"),
    "telemetry": {
        "disk_warn_pct": 85,
        "disk_crit_pct": 92,
        "backup_max_age_hours": 48,
        "load_warn": 1.5,
        "load_crit": 3.0,
        "backups": [],
    },
    "actions": {"enabled": []},
}


def load_config() -> dict:
    """Load config.yaml with sane defaults merged underneath."""
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if os.path.exists(CONFIG_PATH):
        try:
            import yaml  # optional; falls back to defaults if absent

            user_cfg = yaml.safe_load(open(CONFIG_PATH))
            if user_cfg:
                cfg = _deep_merge(cfg, user_cfg)
        except ImportError:
            # No pyyaml on the rig: fall back to a tiny subset parser
            cfg = _parse_config_mini(cfg)
    os.makedirs(cfg["state_dir"], exist_ok=True)
    os.makedirs(os.path.dirname(cfg["audit"]["db"]), exist_ok=True)
    os.makedirs(cfg["backup_dir"], exist_ok=True)
    return cfg


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _parse_config_mini(cfg: dict) -> dict:
    """Tiny YAML-subset parser: flat keys and simple lists (no pyyaml)."""
    try:
        for line in open(CONFIG_PATH):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            val = val.strip()
            if key.startswith(" ") or key in ("device_id", "tier") or val:
                key = key.strip()
                if key == "device_id":
                    cfg["device_id"] = val
                elif key == "tier":
                    cfg["tier"] = val
                elif key == "url" and "brain" not in cfg:
                    cfg.setdefault("brain", {})["url"] = val
    except OSError:
        pass
    return cfg


# ---------------------------------------------------------------------------
# Audit trail (hash-chained, tamper-evident)
# ---------------------------------------------------------------------------
def get_audit(db_path: str):
    """Import Agent OS AuditTrail if present; otherwise a local twin."""
    agent_os = os.path.join(HOME, ".agent-os")
    if os.path.isdir(agent_os):
        sys.path.insert(0, agent_os)
    try:
        from audit_trail import AuditTrail
    except ImportError:
        AuditTrail = _LocalAuditTrail  # pragma: no cover - dev fallback
    return AuditTrail(db_path)


class _LocalAuditTrail:
    """Minimal hash-chained trail (used only if Agent OS is unavailable)."""

    def __init__(self, db_path: str):
        import sqlite3

        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS audit_chain (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                prev_hash TEXT NOT NULL,
                timestamp REAL NOT NULL,
                agent TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_name TEXT NOT NULL,
                permission TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                reason TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                this_hash TEXT NOT NULL UNIQUE)"""
        )
        self._conn.commit()

    def _hash(self, row_data: dict) -> str:
        blob = json.dumps(row_data, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()

    def log(self, agent, resource_type, resource_name, permission,
            allowed, reason="", metadata=None) -> int:
        import sqlite3

        prev = self._conn.execute(
            "SELECT this_hash FROM audit_chain ORDER BY row_id DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev[0] if prev else "GENESIS"
        row_data = {
            "prev_hash": prev_hash,
            "timestamp": time.time(),
            "agent": agent,
            "resource_type": resource_type,
            "resource_name": resource_name,
            "permission": permission,
            "allowed": 1 if allowed else 0,
            "reason": reason,
            "metadata": json.dumps(metadata or {}),
        }
        this_hash = self._hash(row_data)
        try:
            self._conn.execute(
                """INSERT INTO audit_chain (prev_hash, timestamp, agent,
                   resource_type, resource_name, permission, allowed,
                   reason, metadata, this_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (prev_hash, row_data["timestamp"], agent, resource_type,
                 resource_name, permission, 1 if allowed else 0,
                 reason, row_data["metadata"], this_hash),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            raise
        return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def recent(self, limit=50):
        rows = self._conn.execute(
            "SELECT * FROM audit_chain ORDER BY row_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        cols = [d[0] for d in self._conn.execute("PRAGMA table_info(audit_chain)")]
        return [dict(zip(cols, r)) for r in rows]

    def verify_chain(self):
        rows = self._conn.execute(
            "SELECT row_id, prev_hash, this_hash FROM audit_chain ORDER BY row_id"
        ).fetchall()
        prev = "GENESIS"
        for row_id, ph, th in rows:
            if ph != prev:
                return False, f"chain break at row {row_id}"
            prev = th
        return True, None


# ---------------------------------------------------------------------------
# Telemetry (Class 0 - watch, read-only)
# ---------------------------------------------------------------------------
def _run(cmd, timeout=20):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
        return out.stdout.strip(), out.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "", -1


def telemetry_disk():
    out, rc = _run(["df", "-P", "/", "/home"])
    mounts = {}
    if rc == 0:
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 6:
                mounts[parts[5]] = int(parts[4].rstrip("%"))
    return {"mounts": mounts, "worst_pct": max(mounts.values()) if mounts else None}


def telemetry_smart():
    """Drive health via smartctl -H -A. Read-only."""
    out, rc = _run(["smartctl", "--scan"])
    devices = []
    for line in out.splitlines():
        if line.startswith("/dev/"):
            dev = line.split()[0]
            health, hrc = _run(["smartctl", "-H", dev], timeout=25)
            attrs, _ = _run(["smartctl", "-A", dev], timeout=25)
            status = "unknown"
            if "PASSED" in health or "OK" in health:
                status = "ok"
            elif "FAILED" in health:
                status = "FAIL"
            realloc = None
            temp = None
            for al in attrs.splitlines():
                if "Reallocated_Sector" in al or "Reallocated_Event" in al:
                    cols = al.split()
                    if len(cols) >= 10 and cols[9].isdigit():
                        realloc = int(cols[9])
                if "Temperature_Celsius" in al and temp is None:
                    cols = al.split()
                    if len(cols) >= 10 and cols[9].isdigit():
                        temp = int(cols[9])
            devices.append({"dev": dev, "health": status,
                            "reallocated_sectors": realloc, "temp_c": temp})
    return {"devices": devices}


def telemetry_backups(paths, max_age_hours):
    results = []
    now = time.time()
    for p in paths:
        if not os.path.isdir(p):
            results.append({"path": p, "present": False})
            continue
        newest = 0
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    m = os.path.getmtime(os.path.join(root, f))
                    newest = max(newest, m)
                except OSError:
                    pass
        age_h = (now - newest) / 3600 if newest else None
        results.append({
            "path": p, "present": True, "newest_age_hours": age_h,
            "stale": age_h is not None and age_h > max_age_hours,
        })
    return {"results": results}


def telemetry_patches():
    """Count pending system updates (Debian/Ubuntu)."""
    out, rc = _run(["apt-get", "-s", "upgrade"], timeout=60)
    if rc != 0:
        return {"pending": None, "note": "apt unavailable"}
    count = 0
    for line in out.splitlines():
        if line.startswith("Inst "):
            count += 1
    return {"pending": count}


def telemetry_uptime_load(cfg):
    load1 = None
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
    except OSError:
        pass
    uptime = None
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
    except OSError:
        pass
    nproc = os.cpu_count() or 1
    ratio = load1 / nproc if load1 is not None else None
    state = "ok"
    if ratio is not None:
        if ratio >= cfg["telemetry"]["load_crit"]:
            state = "crit"
        elif ratio >= cfg["telemetry"]["load_warn"]:
            state = "warn"
    return {"load1": load1, "uptime_hours": (uptime / 3600) if uptime else None,
            "ratio_vs_cores": ratio, "state": state}


def collect_telemetry(cfg):
    """All Class-0 watch data in one pass."""
    return {
        "device_id": cfg["device_id"],
        "collected_at": time.time(),
        "disk": telemetry_disk(),
        "smart": telemetry_smart(),
        "backups": telemetry_backups(
            cfg["telemetry"]["backups"],
            cfg["telemetry"]["backup_max_age_hours"],
        ),
        "patches": telemetry_patches(),
        "load": telemetry_uptime_load(cfg),
    }


# ---------------------------------------------------------------------------
# Fix executor (Class 1 - consent-gated, backup-first)
# ---------------------------------------------------------------------------
ACTIONS = {
    "rotate-logs": {
        "class": 1,
        "desc": "rotate the rig-keeper scratch log safely",
        "backup": lambda cfg: _copy_scratch_log(cfg, "backup"),
        "exec": lambda cfg: _copy_scratch_log(cfg, "rotate"),
    },
    # v1.5+: "apply-package-updates", "restart-service", "rotate-system-logs"
}


def _scratch_log(cfg):
    return os.path.join(cfg["state_dir"], "scratch.log")


def _copy_scratch_log(cfg, mode):
    src = _scratch_log(cfg)
    if not os.path.exists(src):
        with open(src, "w") as f:
            f.write("test log line\n" * 20)
    if mode == "backup":
        dest = os.path.join(cfg["backup_dir"],
                            f"scratch.log.{int(time.time())}.bak")
        subprocess.run(["cp", src, dest], check=True)
        return {"backup": dest}
    # rotate: move current log to rotated name, start fresh
    dest = os.path.join(cfg["state_dir"], f"scratch.log.rotated.{int(time.time())}")
    subprocess.run(["mv", src, dest], check=True)
    with open(src, "w") as f:
        f.write("")
    return {"rotated_to": dest}


class ApprovalStore:
    """Single-use consent tokens: propose() mints, fix() consumes."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "pending-approvals.json")
        self._ensure()

    def _ensure(self):
        if not os.path.exists(self.path):
            with open(self.path, "w") as f:
                json.dump({}, f)

    def _load(self):
        try:
            return json.load(open(self.path))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f)

    def mint(self, action: str, ttl_s: int = 600) -> str:
        token = secrets.token_hex(16)
        data = self._load()
        data[token] = {"action": action, "expires": time.time() + ttl_s,
                       "used": False}
        self._save(data)
        return token

    def consume(self, token: str, action: str) -> tuple[bool, str]:
        data = self._load()
        rec = data.get(token)
        if not rec:
            return False, "unknown token"
        if rec["used"]:
            return False, "token already used"
        if rec["action"] != action:
            return False, f"token is for action '{rec['action']}', not '{action}'"
        if time.time() > rec["expires"]:
            return False, "token expired"
        rec["used"] = True
        data[token] = rec
        self._save(data)
        return True, "approved"


def propose_fix(cfg, audit, action_name):
    """Propose a fix: mint a single-use token, audit the request."""
    action = ACTIONS.get(action_name)
    if not action:
        return {"error": f"unknown action '{action_name}'"}
    if cfg["tier"] == "watch":
        audit.log(cfg["device_id"], "tool", f"fix.{action_name}", "call",
                  False, "watch tier: fix executor disabled",
                  {"requested": action_name})
        return {"error": "watch tier: fixes are disabled (free plan). "
                         "Upgrade to enable safe fixes."}
    token = ApprovalStore(cfg["state_dir"]).mint(action_name)
    audit.log(cfg["device_id"], "tool", f"fix.{action_name}", "call",
              True, "fix proposed; approval token minted",
              {"token_hint": token[:8], "class": action["class"]})
    return {"token": token, "action": action_name,
            "expires_in_s": 600, "desc": action["desc"],
            "approve_with": f"care_agent.py --fix {action_name} --approve {token}"}


def execute_fix(cfg, audit, action_name, token):
    """Approve + execute a fix: backup first, then run, then audit."""
    action = ACTIONS.get(action_name)
    if not action:
        return {"error": f"unknown action '{action_name}'"}
    if cfg["tier"] == "watch":
        audit.log(cfg["device_id"], "tool", f"fix.{action_name}", "call",
                  False, "watch tier: fix executor disabled")
        return {"error": "watch tier: fixes are disabled"}
    ok, why = ApprovalStore(cfg["state_dir"]).consume(token, action_name)
    if not ok:
        audit.log(cfg["device_id"], "tool", f"fix.{action_name}", "call",
                  False, f"approval rejected: {why}", {"token_hint": token[:8]})
        return {"error": f"approval rejected: {why}"}
    # 1) backup
    before = action["backup"](cfg)
    audit.log(cfg["device_id"], "tool", f"fix.{action_name}", "write",
              True, "backup taken before write", {"backup": before})
    # 2) execute
    after = action["exec"](cfg)
    # 3) audit the write itself
    audit.log(cfg["device_id"], "tool", f"fix.{action_name}", "write",
              True, "approved fix executed", {"after": after,
                                              "token_hint": token[:8]})
    return {"ok": True, "backup": before, "result": after}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    cfg = load_config()
    audit = get_audit(cfg["audit"]["db"])
    agent = f"care-{cfg['device_id']}"

    ap = argparse.ArgumentParser(description="Rig Keeper care agent (v1)")
    ap.add_argument("--check", action="store_true",
                    help="collect telemetry (Class 0, read-only)")
    ap.add_argument("--report", action="store_true",
                    help="telemetry + plain-language brain report")
    ap.add_argument("--propose", metavar="ACTION",
                    help="propose a safe fix and mint an approval token")
    ap.add_argument("--fix", metavar="ACTION",
                    help="execute a fix (requires --approve)")
    ap.add_argument("--approve", metavar="TOKEN",
                    help="single-use approval token from --propose")
    ap.add_argument("--audit", action="store_true",
                    help="show the audit trail (readable)")
    ap.add_argument("--verify-chain", action="store_true",
                    help="verify hash-chain integrity")
    args = ap.parse_args()

    if args.check or args.report:
        tele = collect_telemetry(cfg)
        print(json.dumps(tele, indent=2))
        if args.report:
            sys.path.insert(0, BASE)
            from brain import make_report  # local import, keeps CLI lean

            report, violations = make_report(cfg, tele)
            print("\n===== PLAIN-LANGUAGE REPORT =====")
            print(report)
            if violations:
                print("\n[DICTIONARY VIOLATIONS]", violations)
                sys.exit(2)
            else:
                print("\n[dictionary check: PASS]")
        return

    if args.propose:
        res = propose_fix(cfg, audit, args.propose)
        print(json.dumps(res, indent=2))
        return

    if args.fix:
        if not args.approve:
            audit.log(agent, "tool", f"fix.{args.fix}", "call", False,
                      "fix attempted without approval token")
            print(json.dumps(
                {"error": "no approval token; run --propose first",
                 "hint": f"care_agent.py --propose {args.fix}"}, indent=2))
            sys.exit(1)
        res = execute_fix(cfg, audit, args.fix, args.approve)
        print(json.dumps(res, indent=2))
        return

    if args.audit:
        for e in audit.recent(25):
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["timestamp"]))
            verdict = "ALLOW" if e["allowed"] else "DENY"
            print(f"[{e['row_id']:>4}] {ts} "
                  f"{e['agent']:<20} {e['resource_type']}:{e['resource_name']:<25} "
                  f"{e['permission']:<8} {verdict:<5} {e['reason'][:70]}")
        return

    if args.verify_chain:
        ok, why = audit.verify_chain()
        print("CHAIN OK" if ok else f"CHAIN BROKEN: {why}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
