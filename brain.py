#!/usr/bin/env python3
"""
Rig Keeper - brain.py (v1, Phase 0)

Plain-language report generator. Talks to a local llama-server running
Granite 4.1 3B (the edge brain) and turns raw telemetry into Manny's
weekly status report. A dictionary gate refuses to ship technical jargon.

Design rule (from the Aug 26 spike): the LLM PROPOSES, deterministic
code verifies. If the brain is unreachable, we fall back to a template
and say so - we never fabricate a report.
"""
import json
import re
import time
import urllib.request

# Plain-language dictionary (persona doc): the left column never ships.
BANNED_TERMS = [
    r"\bdisk partition\b", r"\bpartition(s)?\b", r"\bpatch(es|ing)?\b",
    r"\breboot\b", r"\bmalware\b", r"\bvirus(es)?\b", r"\bdaemon\b",
    r"\bpackage(s)?\b", r"\bdefrag\b", r"\bregistry\b", r"\bBIOS\b",
    r"\bRAM\b", r"\bCPU\b", r"\bGPU\b", r"\bSSD\b", r"\buptime\b",
    r"\bsmartctl\b", r"\bSMART\b", r"\bthermals?\b", r"\btemp_c\b",
    r"\bloadavg\b", r"\bmounts?\b", r"\bdf -P\b", r"\bapt(-get)?\b",
    r"\btelemetry\b", r"\breallocated\b", r"\bmetric(s)?\b",
    r"\bload (of|is|number)?\b", r"\b[0-9]+(\.[0-9]+)? hours\b",
    r"\brunning for\b", r"\bhas been up\b", r"\buptime\b",
    r"\btemperature(s)?\b", r"\b[0-9]+°?C\b", r"\bcores?\b",
    r"\bGHz\b", r"\bdevice(s)?\b",
    r"\b\d+(\.\d+)?% (left|free|remaining|available)\b",
    r"\b\d+(\.\d+)?% of .* (left|free|remaining)\b",
]
_BANNED_RE = re.compile("|".join(BANNED_TERMS), re.IGNORECASE)

SYSTEM_PROMPT = """You are Manny, the friendly computer caretaker for the CareKeeper service. \
You talk to a family, not to technicians. Rules:
1. Explain the health of their computers in plain, warm language. No jargon.
2. Never use these words or ideas: partition, patch, reboot, malware, virus, daemon, package, RAM, CPU, GPU, SSD, uptime, SMART, temperature readings, telemetry, metric, mount, reallocated.
3. Say "storage" for disk space, "restart" for reboot, "security update" for patch, "check for bad stuff" for malware scans, "drive health" for SMART.
4. Never name drives by technical labels like sda or sdb - say "your main drive", "your second drive", or "the drives".
5. If a check could not run (drive health unknown), say the check could not run - do NOT claim the drives are fine or that there are no signs of trouble. Honesty over reassurance.
6. Never mention load numbers, temperatures, or how long a computer has been running - those are technician details. Say "running smoothly" or "working hard" instead.
Rule: Always say how FULL storage is (for example "storage is 82% full"). Never say how much is left ("82% left" or "18% free") - that flips the meaning and can hide a problem.
7. If something needs attention, say what a family member should know and what Manny recommends - always with their permission before anything is changed.
8. Keep it to 6-8 short sentences, friendly but professional. Never invent facts that are not in the telemetry provided."""


def _ask_brain(cfg, telemetry, extra_rule: str = "") -> str:
    url = cfg["brain"]["url"] + "/chat/completions"
    system = SYSTEM_PROMPT
    if extra_rule:
        system += "\n8. You previously used banned words. This time ABSOLUTELY avoid: " + extra_rule
    payload = {
        "model": cfg["brain"].get("model", "granite-4.1-3b-q4"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content":
             "Here is the device health telemetry as JSON. Write the weekly "
             "plain-language status report:\n" + json.dumps(telemetry)},
        ],
        "temperature": 0.4,
        "max_tokens": 250,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=cfg["brain"]["timeout_s"]) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()


def _template_report(tele) -> str:
    """Deterministic plain-language fallback when the brain is offline."""
    d = tele["disk"]
    worst = d["worst_pct"]
    lines = []
    if worst is not None:
        if worst >= 92:
            lines.append(f"Your main storage is {worst}% full - that's a lot. "
                         "Manny recommends making room soon.")
        elif worst >= 85:
            lines.append(f"Your main storage is {worst}% full. Worth keeping an eye on.")
        else:
            lines.append(f"Your main storage is {worst}% full - looking healthy.")
    smart = tele["smart"]["devices"]
    if smart:
        bad = [s for s in smart if s["health"] == "FAIL"]
        unknown = [s for s in smart if s["health"] == "unknown"]
        if bad:
            lines.append("The drive health check shows a problem on "
                         + ", ".join(s["dev"] for s in bad)
                         + ". Back up important files as soon as possible.")
        elif unknown:
            lines.append("Manny could not check the drive health on "
                         + ", ".join(s["dev"] for s in unknown)
                         + " - not a problem found, just a check that "
                           "couldn't run.")
        else:
            lines.append("Drive health checks passed - the drives are in good shape.")
    backups = tele["backups"]["results"]
    if backups:
        stale = [b for b in backups if b.get("stale")]
        if stale:
            lines.append("Some backups are older than expected - a fresh backup "
                         "is recommended.")
        else:
            lines.append("Backups are fresh - your files are being looked after.")
    pending = tele["patches"].get("pending")
    if pending is not None:
        if pending:
            lines.append(f"There are {pending} security updates waiting. "
                         "Manny can install them with your OK.")
        else:
            lines.append("No security updates waiting - everything is up to date.")
    load_state = tele["load"].get("state")
    if load_state == "crit":
        lines.append("The computer has been working unusually hard lately - "
                     "Manny suggests a restart when convenient.")
    elif load_state == "warn":
        lines.append("The computer has been a bit busy lately, but nothing to worry about.")
    else:
        lines.append("The computer has been running smoothly.")
    return "\n".join(lines)


def check_dictionary(report: str) -> list:
    """Return any banned terms that leaked into the report."""
    return sorted(set(m.group(0).lower() for m in _BANNED_RE.finditer(report)))


FLEET_PROMPT = """You are Manny, the friendly computer caretaker for the CareKeeper service. \
You look after a family's computers and report on ALL of them in one message. Rules:
1. Explain the health of every computer in plain, warm language. No jargon.
2. Never use these words or ideas: partition, patch, reboot, malware, virus, daemon, package, RAM, CPU, GPU, SSD, uptime, SMART, temperature readings, telemetry, metric, mount, reallocated.
3. Say "storage" for disk space, "restart" for reboot, "security update" for patch, "drive health" for SMART.
4. Never name drives by technical labels (sda, sdb...) or show file paths - say "your main drive", "your backup folder".
5. If a check could not run (drive health unknown, or a computer could not be reached), say so honestly - do NOT claim things are fine.
6. Never mention load numbers, temperatures, or how long a computer has been running - those are technician details. Say "running smoothly" or "working hard" instead.
Rule: Always say how FULL storage is (for example "storage is 82% full"). Never say how much is left ("82% left" or "18% free") - that flips the meaning and can hide a problem.
7. Never name drives by technical labels (sda, sdb...) or show file paths - say "your main drive", "your backup folder".
8. If a check could not run (drive health unknown, or a computer could not be reached), say so honestly - do NOT claim things are fine.
9. Start with one friendly opening line, then one short line per computer (name it the way a family would: "M7", "the main machine", "the Dell"), then one closing line with any recommendation. Never invent facts not in the telemetry.
10. Keep it to 8-12 short sentences total."""


FRIENDLY_NAMES = {
    "m7-ultra": "M7",
    "og-rig-dev": "this computer",
    "dell-inspiron": "the Dell",
}


def plain_bullets(machines: dict) -> list:
    """Deterministic plain-language facts per machine (no LLM, no jargon).

    The brain composes from these; the template fallback reuses them.
    """
    bullets = []
    for dev, tele in machines.items():
        name = FRIENDLY_NAMES.get(dev, dev)
        if isinstance(tele, dict) and "error" in tele:
            bullets.append(f"{name}: could not be reached right now.")
            continue
        facts = []
        disk = tele.get("disk", {})
        pct = disk.get("worst_pct")
        if pct is not None:
            facts.append(f"storage {pct}% full")
        else:
            facts.append("storage could not be checked")
        patches = tele.get("patches", {}).get("pending", 0)
        if patches:
            facts.append(f"{patches} security update"
                         + ("s" if patches != 1 else "") + " waiting")
        elif patches == 0:
            facts.append("security updates are up to date")
        backups = tele.get("backups", {})
        if backups.get("error"):
            facts.append("backup check couldn't run")
        else:
            results = backups.get("results", [])
            if not results:
                facts.append("no backup folder found")
            elif any(r.get("present") is False for r in results):
                facts.append("a backup folder couldn't be found")
            elif any(r.get("empty") for r in results):
                facts.append("a backup folder is empty - nothing backed up yet")
            elif any(r.get("stale") for r in results):
                facts.append("backup folder is older than expected")
            else:
                facts.append("backup folder looks fresh")
        smart = tele.get("smart", {}).get("devices", [])
        if not smart:
            facts.append("drive health couldn't be checked")
        elif any(s.get("health") == "FAIL" for s in smart):
            facts.append("drive health needs attention")
        else:
            facts.append("drives look healthy")
        bullets.append(f"{name}: " + "; ".join(facts) + ".")
    return bullets


def fleet_report(cfg, machines: dict):
    """One plain-language report for the whole fleet.

    machines: {device_id: telemetry_dict or {"error": "..."}}
    Returns (report, violations). Discipline: code prepares the facts in
    plain language, brain composes the friendly message, code verifies the
    dictionary. Retry ONCE with banned words spelled out; if it still
    fails, fall back to the guaranteed-clean template. Never ship a
    report that fails the gate.
    """
    bullets = plain_bullets(machines)
    facts = "\n".join(f"- {b}" for b in bullets)

    def _ask(extra_rule: str = None):
        system = FLEET_PROMPT
        if extra_rule:
            system += ("\n10. You previously used banned words. This time "
                       "ABSOLUTELY avoid: " + extra_rule)
        url = cfg["brain"]["url"] + "/chat/completions"
        req = urllib.request.Request(
            url, data=json.dumps({
                "model": cfg["brain"].get("model", "granite-4.1-3b-q4"),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content":
                     "Here are the verified facts about the family's "
                     "computers (one line per computer). Write the friendly "
                     "status report using ONLY these facts, in this order:\n"
                     + facts},
                ],
                "temperature": 0.4,
                "max_tokens": 350,
            }).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req,
                                    timeout=cfg["brain"]["timeout_s"]) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()

    def _template():
        lines = ["Hi! Here's how your family's computers are doing:"]
        lines += [f"- {b}" for b in bullets]
        lines.append("If anything needs attention, I'll let you know here.")
        return "\n".join(lines)

    try:
        report = _ask()
    except Exception as exc:  # brain offline / timeout - honest template
        return (_template() + f"\n\n[brain was offline - this report used "
                f"the backup template: {exc}]"), []

    violations = check_dictionary(report)
    if violations:
        try:
            report = _ask(extra_rule=", ".join(sorted(violations)))
        except Exception:
            pass
        violations = check_dictionary(report)
        if violations:
            return _template(), []
    return report, []


def make_report(cfg, telemetry: dict):
    """Return (report, violations). Violations non-empty = report fails gate.

    Discipline: the LLM proposes, code verifies. If the brain's report uses
    banned words, retry ONCE with explicit avoidance instruction; if it still
    fails, fall back to the guaranteed-clean template. Never ship a report
    that fails the gate.
    """
    try:
        report = _ask_brain(cfg, telemetry)
    except Exception as exc:  # brain offline / timeout - be honest, use template
        return _template_report(telemetry) + (
            f"\n\n[brain was offline - this report used the backup template: {exc}]"
        ), []

    violations = check_dictionary(report)
    if violations:
        # one retry with the banned words spelled out
        try:
            report = _ask_brain(cfg, telemetry,
                                extra_rule=", ".join(sorted(violations)))
        except Exception:
            pass
        violations = check_dictionary(report)
        if violations:
            report = _template_report(telemetry)
            return report, []
    return report, []


if __name__ == "__main__":
    import sys

    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from care_agent import collect_telemetry, load_config

    cfg = load_config()
    report, violations = make_report(cfg, collect_telemetry(cfg))
    print(report)
    if violations:
        print("\n[DICTIONARY VIOLATIONS]", violations)
        sys.exit(2)
    print("\n[dictionary check: PASS]")
