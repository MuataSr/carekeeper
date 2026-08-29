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
import sys
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
    r"\b\d+(\.\d+)?%[^.\n]{0,40}?\b(available|free|left|remaining)\b",
    r"\b\d+(\.\d+)?% of .* (left|free|remaining)\b",
]
_BANNED_RE = re.compile("|".join(BANNED_TERMS), re.IGNORECASE)

# ---------------------------------------------------------------------------
# Personas (CAREKEEPER-PERSONA.md, locked roster). Profiles change the voice,
# never the skeleton: every persona shares the dictionary, honesty rules,
# permission classes, and templates below. Switching is a config line.
# ---------------------------------------------------------------------------
PERSONAS = {
    "manny": {
        "name": "Manny",
        "role": "the friendly handyman - the neighbor who's good with his "
                "hands. Warm, plain-spoken, a little dry humor. Explains "
                "just enough, never too much.",
        "voice": "Warm and friendly. Light humor. Medium explanation depth.",
        "opening": "Hi! Here's how your computers are doing:",
        "reframe": "I keep computers healthy, not crowds laughing. But I can "
                   "tell you everything I've been doing on your machines - "
                   "that's my kind of fun.",
    },
    "steady": {
        "name": "Steady",
        "role": "the quiet professional - the property manager who runs a "
                "tight building. Calm, direct, zero small talk, never wastes "
                "your time. Task-first, always.",
        "voice": "Calm and direct. No small talk, no emoji, no fluff. "
                 "Minimal explanation. Every sentence carries information.",
        "opening": "Status:",
        "reframe": "I'm your computer caretaker, not a chat assistant. "
                   "Status report is ready when you want it.",
    },
    "sage": {
        "name": "Sage",
        "role": "the patient teacher - the shop teacher who genuinely loves "
                "when you learn something. Explains generously when invited, "
                "never talks down, always encourages.",
        "voice": "Warm and encouraging. Gentle humor. Explains a little more "
                 "when it helps, never talks down.",
        "opening": "Here's the health of your family's computers:",
        "reframe": "Good question to ask a chatbot - but I'm your machines' "
                   "keeper, so my skills are all about them. Want the tour "
                   "of what I watch?",
    },
    "guardian": {
        "name": "Guardian",
        "role": "the quiet security professional who is always on your side. "
                "Vigilant, reassuring, framed around safety and privacy.",
        "voice": "Vigilant and reassuring. No humor. Frames everything "
                 "around safety and privacy. Medium explanation depth.",
        "opening": "All clear. Status:",
        "reframe": "I'm your family's computer guardian - I watch the "
                   "machines, I don't chat. Status report is ready when "
                   "you want it.",
    },
    "tidy": {
        "name": "Tidy",
        "role": "the housekeeper who keeps things in order and clucks fondly "
                "at clutter. Cozy, domestic warmth, gentle humor about "
                "digital messes.",
        "voice": "Cozy and warm. Gentle domestic humor. Medium explanation "
                 "depth.",
        "opening": "Good morning, dear! The house is in order - here's how "
                   "things stand:",
        "reframe": "I keep your computers tidy, dear - I don't chat for "
                   "sport. But I can show you exactly what I've been "
                   "tidying!",
    },
}


def get_persona(cfg: dict) -> dict:
    """Return the persona dict from config (default: Manny)."""
    name = str(cfg.get("persona", "manny")).strip().lower()
    return PERSONAS.get(name, PERSONAS["manny"])


_SYSTEM_RULES = """Rules:
1. Explain the health of their computers in plain, warm language. No jargon.
2. Never use these words or ideas: partition, patch, reboot, malware, virus, daemon, package, RAM, CPU, GPU, SSD, uptime, SMART, temperature readings, telemetry, metric, mount, reallocated.
3. Say "storage" for disk space, "restart" for reboot, "security update" for patch, "check for bad stuff" for malware scans, "drive health" for SMART.
4. Never name drives by technical labels like sda or sdb - say "your main drive", "your second drive", or "the drives".
5. If a check could not run (drive health unknown), say the check could not run - do NOT claim the drives are fine or that there are no signs of trouble. Honesty over reassurance.
6. Never mention load numbers, temperatures, or how long a computer has been running - those are technician details. Say "running smoothly" or "working hard" instead.
Rule: Always say how FULL storage is (for example "storage is 82% full"). Never say how much is left ("82% left" or "18% free") - that flips the meaning and can hide a problem.
7. If something needs attention, say what a family member should know and what you recommend - always with their permission before anything is changed.
8. Keep it to 6-8 short sentences, friendly but professional. Never invent facts that are not in the telemetry provided."""


def build_system_prompt(persona: dict) -> str:
    return (f"You are {persona['name']}, {persona['role']} You talk to a "
            f"family, not to technicians. Voice: {persona['voice']}\n"
            + _SYSTEM_RULES)


def _ask_brain(cfg, telemetry, extra_rule: str = "", persona: dict = None) -> str:
    url = cfg["brain"]["url"] + "/chat/completions"
    persona = persona or get_persona(cfg)
    system = build_system_prompt(persona)
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


def _template_report(tele, persona: dict = None) -> str:
    """Deterministic plain-language fallback when the brain is offline."""
    p = persona or PERSONAS["manny"]
    d = tele["disk"]
    worst = d["worst_pct"]
    lines = []
    if worst is not None:
        if worst >= 92:
            lines.append(f"Your main storage is {worst}% full - that's a lot. "
                         f"{p['name']} recommends making room soon.")
        elif worst >= 85:
            lines.append(f"Your main storage is {worst}% full. Worth keeping an eye on.")
        else:
            lines.append(f"Your main storage is {worst}% full - looking healthy.")
    smart_data = tele["smart"]
    smart = smart_data["devices"]
    if smart:
        bad = [s for s in smart if s["health"] == "FAIL"]
        unknown = [s for s in smart if s["health"] == "unknown"]
        if bad:
            lines.append("The drive health check shows a problem on "
                         + ", ".join(s["dev"] for s in bad)
                         + ". Back up important files as soon as possible.")
        elif unknown:
            lines.append(f"{p['name']} could not check the drive health on "
                         + ", ".join(s["dev"] for s in unknown)
                         + " - not a problem found, just a check that "
                           "couldn't run.")
        else:
            lines.append("Drive health checks passed - the drives are in good shape.")
    elif smart_data.get("note") == "smartctl not installed":
        lines.append(
            "The drive health check couldn't run because the tool it needs isn't installed."
        )
    elif smart_data.get("note") == "no SMART-capable drives":
        lines.append("The drive health check couldn't run because no compatible drives were found.")
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
                         f"{p['name']} can install them with your OK.")
        else:
            lines.append("No security updates waiting - everything is up to date.")
    load_state = tele["load"].get("state")
    if load_state == "crit":
        lines.append("The computer has been working unusually hard lately - "
                     f"{p['name']} suggests a restart when convenient.")
    elif load_state == "warn":
        lines.append("The computer has been a bit busy lately, but nothing to worry about.")
    else:
        lines.append("The computer has been running smoothly.")
    return "\n".join(lines)


def check_dictionary(report: str) -> list:
    """Return any banned terms that leaked into the report."""
    return sorted(set(m.group(0).lower() for m in _BANNED_RE.finditer(report)))


_SAFE_MIN = ("Your computers were checked, but I can't write the report "
             "properly right now. I'll try again at the next check.")


def _offline_note(exc: Exception) -> str:
    """The family copy gets a static, gate-clean note. The raw exception
    goes to stderr (systemd journal) for the operator - never into the
    shipped report, where it could leak jargon past the dictionary gate."""
    print(f"[carekeeper brain unavailable: {exc}]", file=sys.stderr)
    return "[brain was offline - this report used the backup template]"


def _final_gate(report: str) -> str:
    """Last line of defense: anything that still fails the dictionary
    (template regressions included) is replaced with a hardcoded minimal
    message that is clean by construction. Never ship a failing report."""
    viol = check_dictionary(report)
    if not viol:
        return report
    print(f"[carekeeper final gate: dropped report with {len(viol)} "
          f"violations: {', '.join(viol)}]", file=sys.stderr)
    return _SAFE_MIN


_FLEET_RULES = """Rules (the same for every report):
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


def build_fleet_prompt(persona: dict) -> str:
    return (f"You are {persona['name']}, {persona['role']} You look after a "
            f"family's computers and report on ALL of them in one message. "
            f"Voice: {persona['voice']}\n" + _FLEET_RULES)


_WEEKLY_RULES = _FLEET_RULES + """
11. This is the WEEKLY review. Structure it as: one warm opening line; then
    one or two lines per computer (how the week went + current health);
    then what was fixed or approved this week (or that nothing needed
    touching); then one closing line with any recommendation for the week
    ahead. Keep it to 12-16 short sentences."""


def build_weekly_prompt(persona: dict) -> str:
    return (f"You are {persona['name']}, {persona['role']} You look after a "
            f"family's computers and give them a weekly review. "
            f"Voice: {persona['voice']}\n" + _WEEKLY_RULES)


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
        patches = tele.get("patches", {}).get("pending")
        if patches:
            facts.append(f"{patches} security update"
                         + ("s" if patches != 1 else "") + " waiting")
        elif patches == 0:
            facts.append("security updates are up to date")
        else:
            # honesty: a check that couldn't run is never 'up to date'
            facts.append("security updates couldn't be checked")
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
        smart_data = tele.get("smart", {})
        smart = smart_data.get("devices", [])
        if not smart:
            if smart_data.get("note") == "smartctl not installed":
                facts.append("drive health couldn't be checked because its tool isn't installed")
            elif smart_data.get("note") == "no SMART-capable drives":
                facts.append(
                    "drive health couldn't be checked because no compatible drives were found"
                )
            else:
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
    persona = get_persona(cfg)

    def _ask(extra_rule: str = None):
        system = build_fleet_prompt(persona)
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
        lines = [persona["opening"]]
        lines += [f"- {b}" for b in bullets]
        lines.append("If anything needs attention, I'll let you know here.")
        return "\n".join(lines)

    try:
        report = _ask()
    except Exception as exc:  # brain offline / timeout - honest template
        return _final_gate(_template() + "\n\n" + _offline_note(exc)), []

    violations = check_dictionary(report)
    if violations:
        try:
            report = _ask(extra_rule=", ".join(sorted(violations)))
        except Exception:
            pass
        violations = check_dictionary(report)
        if violations:
            return _final_gate(_template()), []
    return report, []


_FIX_LABELS = {
    "rotate-logs": "log housekeeping",
    "apply-package-updates": "installing the updates",
}


def week_bullets(stats: dict) -> list:
    """Deterministic plain-language week facts from audit stats (no LLM).

    Honesty rules: only genuine executions count as fixes; only owner
    denials count as denials; safety-lock tests are reported as exactly
    that. Output is clean by construction (never contains banned words),
    so the deterministic template always passes the dictionary gate.
    """
    lines = []
    if not stats.get("readable"):
        lines.append("The week's logbook couldn't be read - the daily checks "
                     "still ran, but this review is partial.")
        return lines
    checks_ok = stats.get("checks_ok", {})
    checks_fail = stats.get("checks_fail", {})
    if checks_ok:
        parts = []
        for dev, count in sorted(checks_ok.items()):
            name = FRIENDLY_NAMES.get(dev, dev)
            fail = checks_fail.get(dev, 0)
            if fail == 0:
                parts.append(f"{name} was reached {count} times - every day")
            elif fail == 1:
                parts.append(f"{name} was reached {count} times - once it "
                             "couldn't be reached")
            else:
                parts.append(f"{name} was reached {count} times - {fail} "
                             "times it couldn't be reached")
        lines.append("The weekly checks ran: " + "; ".join(parts) + ".")
    # only genuine executions count as fixes
    fixes = [rn for rn, reason in stats.get("fixes", [])
             if reason.startswith("approved fix executed")
             or reason.startswith("approved upgrade")]
    # only owner denials count as denials; the rest are safety-lock tests
    denials = [rn for rn, reason in stats.get("denials", [])
               if reason.startswith("owner denied")]
    locks = [rn for rn, reason in stats.get("denials", [])
             if not reason.startswith("owner denied")]
    if fixes:
        n = len(fixes)
        labels = [_FIX_LABELS.get(rn.split(".", 1)[-1], "a safe fix")
                  for rn in fixes]
        lines.append(f"{n} safe {'fix' if n == 1 else 'fixes'} "
                     f"{'was' if n == 1 else 'were'} completed this week: "
                     + ", ".join(labels) + ".")
    else:
        lines.append("No fixes were needed this week - nothing had to be "
                     "touched.")
    if denials:
        n = len(denials)
        lines.append(f"{n} fix {'was' if n == 1 else 'were'} declined - "
                     "nothing was changed.")
    if locks:
        lines.append("The safety locks were tested and held - no fix ran "
                     "without a fresh approval.")
    msg = stats.get("messages", 0)
    if msg:
        s = "s" if msg != 1 else ""
        lines.append(f"{msg} check-in message{s} "
                     f"{'were' if msg != 1 else 'was'} sent to the family "
                     "chat this week.")
    return lines


def weekly_report(cfg, machines: dict, stats: dict):
    """Week-in-review report. Same gate discipline as fleet_report:
    code prepares the facts, brain composes, dictionary verifies,
    retry once, else guaranteed-clean template."""
    wb = week_bullets(stats)
    bullets = plain_bullets(machines)
    facts = "\n".join(f"- {b}" for b in wb + bullets)
    persona = get_persona(cfg)

    def _ask(extra_rule: str = None):
        system = build_weekly_prompt(persona)
        if extra_rule:
            system += ("\n12. You previously used banned words. This time "
                       "ABSOLUTELY avoid: " + extra_rule)
        url = cfg["brain"]["url"] + "/chat/completions"
        req = urllib.request.Request(
            url, data=json.dumps({
                "model": cfg["brain"].get("model", "granite-4.1-3b-q4"),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content":
                     "Here are the verified facts about the family's "
                     "computers this week (week facts first, then current "
                     "health). Write the weekly review using ONLY these "
                     "facts:\n" + facts},
                ],
                "temperature": 0.4,
                "max_tokens": 500,
            }).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req,
                                    timeout=cfg["brain"]["timeout_s"]) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()

    def _template():
        lines = [persona["opening"]]
        lines += [f"- {b}" for b in wb]
        lines += [f"- {b}" for b in bullets]
        lines.append("If anything needs attention, I'll let you know here.")
        return "\n".join(lines)

    try:
        report = _ask()
    except Exception as exc:  # brain offline / timeout - honest template
        return _final_gate(_template() + "\n\n" + _offline_note(exc)), []

    violations = check_dictionary(report)
    if violations:
        try:
            report = _ask(extra_rule=", ".join(sorted(violations)))
        except Exception:
            pass
        violations = check_dictionary(report)
        if violations:
            return _final_gate(_template()), []
    return report, []


def make_report(cfg, telemetry: dict):
    """Return (report, violations). Violations non-empty = report fails gate.

    Discipline: the LLM proposes, code verifies. If the brain's report uses
    banned words, retry ONCE with explicit avoidance instruction; if it still
    fails, fall back to the guaranteed-clean template. Never ship a report
    that fails the gate.
    """
    persona = get_persona(cfg)
    try:
        report = _ask_brain(cfg, telemetry, persona=persona)
    except Exception as exc:  # brain offline / timeout - be honest, use template
        return _final_gate(_template_report(telemetry, persona)
                           + "\n\n" + _offline_note(exc)), []

    violations = check_dictionary(report)
    if violations:
        # one retry with the banned words spelled out
        try:
            report = _ask_brain(cfg, telemetry,
                                extra_rule=", ".join(sorted(violations)),
                                persona=persona)
        except Exception:
            pass
        violations = check_dictionary(report)
        if violations:
            return _final_gate(_template_report(telemetry, persona)), []
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
