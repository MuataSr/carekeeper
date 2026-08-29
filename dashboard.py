#!/usr/bin/env python3
"""
CareKeeper - dashboard.py (v2, report-style customer view)

Renders the family fleet health dashboard as a PNG (PIL, zero new deps)
for delivery through Telegram as a photo. v2 matches the locked customer
home-view design (mockups/customer-home-view.html): a report that reads
like a statement — brand header, one overall answer, devices by name,
plain phrases, and nothing to click.

Usage:
  dashboard.py             render + save PNG (prints path)
  dashboard.py --send      render + send via Manny (Telegram)
"""
import argparse
import json
import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

DASH_PATH = os.path.join(BASE, "state", "dashboard.png")

from care_agent import load_config, get_audit
from fleet_check import collect_all, load_devices

OUT = os.path.join(BASE, "state", "dashboard.png")

# --- palette (report-style, matches mockup v2) ---
NAVY = (13, 27, 42)          # #0D1B2A page / frame bg
NAVY2 = (27, 46, 74)         # #1B2E4A card bg
NAVY_HI = (15, 31, 51)       # header gradient top
GOLD = (200, 169, 81)        # #C8A951 brand
GOLD_DIM = (166, 138, 62)    # #A68A3E meta
CREAM = (245, 240, 232)      # #F5F0E8 primary text
CREAM_DIM = (212, 207, 199)  # #D4CFC7 secondary text
GREEN = (46, 204, 113)       # #2ECC71 ok
AMBER = (243, 156, 18)       # #F39C12 warn
RED = (231, 76, 60)          # #E74C3C critical
GRAY = (148, 163, 184)       # offline / muted
RULE = (33, 44, 64)          # row borders (gold at low alpha over navy)
FOOT_BG = (24, 34, 50)       # footer tint
FOOT_RULE = (78, 64, 34)     # footer top border

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
W = 900
PAD = 30


def font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def text_w(draw, txt, f):
    return draw.textbbox((0, 0), txt, font=f)[2]


def truncate(draw, txt, f, max_w):
    if text_w(draw, txt, f) <= max_w:
        return txt
    while txt and text_w(draw, txt + "…", f) > max_w:
        txt = txt[:-1]
    return txt + "…"


# ---------------------------------------------------------------------------
# Status → plain-language sentence (design rule: honest, no jargon)
# ---------------------------------------------------------------------------
def status_of(tele: dict) -> tuple:
    """Return (plain_label, color) for a device telemetry dict."""
    if isinstance(tele, dict) and "error" in tele:
        if "not enrolled yet" in tele["error"]:
            return "not set up yet", GRAY
        return "couldn't be reached", GRAY
    disk = tele.get("disk", {}).get("worst_pct")
    if disk is not None and disk >= 92:
        return "your main drive is nearly full", RED
    if disk is not None and disk >= 85:
        return "your main drive is getting full", AMBER
    smart = tele.get("smart", {}).get("devices", [])
    if any(s.get("health") == "FAIL" for s in smart):
        return "one of your drives has a problem", RED
    stale = any(b.get("stale") for b in tele.get("backups", {}).get("results", []))
    if stale:
        return "a backup is behind", AMBER
    bres = tele.get("backups", {}).get("results", [])
    if any(b.get("present") is False for b in bres):
        return "a backup folder is missing", AMBER
    if any(b.get("empty") for b in bres):
        return "backup folder is empty", AMBER
    pending = tele.get("patches", {}).get("pending", 0)
    if pending and pending > 0:
        n = "update" if pending == 1 else "updates"
        return f"{pending} {n} ready — waiting on your OK", AMBER
    if bres:
        return "up to date · backed up", GREEN
    return "up to date", GREEN


def row_text(tele: dict) -> tuple:
    """Return (plain_text, color) for the devices row body."""
    if isinstance(tele, dict) and "error" in tele:
        if "not enrolled yet" in tele["error"]:
            return "not set up yet", GRAY
        return "couldn't be reached this morning", GRAY
    disk = tele.get("disk", {}).get("worst_pct")
    if disk is not None and disk >= 92:
        return "your main drive is nearly full — Manny is on it", RED
    if disk is not None and disk >= 85:
        return "your main drive is getting full", AMBER
    smart = tele.get("smart", {}).get("devices", [])
    if any(s.get("health") == "FAIL" for s in smart):
        return "one of your drives has a problem", RED
    stale = any(b.get("stale") for b in tele.get("backups", {}).get("results", []))
    if stale:
        return "a backup is behind", AMBER
    bres = tele.get("backups", {}).get("results", [])
    if any(b.get("present") is False for b in bres):
        return "a backup folder is missing", AMBER
    if any(b.get("empty") for b in bres):
        return "backup folder is empty", AMBER
    pending = tele.get("patches", {}).get("pending", 0)
    if pending and pending > 0:
        n = "update" if pending == 1 else "updates"
        return f"{pending} {n} ready — waiting on your OK", AMBER
    if bres:
        return "up to date · backed up", CREAM_DIM
    return "up to date", CREAM_DIM


def device_display(name: str, devices: dict) -> str:
    """Friendly name if the registry has one; otherwise the plain hostname."""
    dev = devices.get(name, {})
    return dev.get("friendly_name") or name


def day_label(ts: float) -> str:
    now = time.time()
    days = (now - ts) / 86400
    if days < 0.35:
        return "Today"
    if days < 1.35:
        return "Yesterday"
    if days < 6.5:
        return time.strftime("%A", time.localtime(ts))
    return f"{int(days)} days ago"


def humanize_action(name: str) -> str:
    base = name.split(".")[-1]
    FIX_LABELS = {
        "rotate-logs": "Rotated old log files",
        "apply-package-updates": "Applied package updates",
        "clean-junk": "Cleared junk files",
        "empty-trash": "Emptied the trash",
    }
    if base in FIX_LABELS:
        return FIX_LABELS[base]
    for verb in ("apply-", "run-", "install-"):
        if base.startswith(verb):
            base = base[len(verb):]
            break
    return base.replace("-", " ").replace("_", " ").title()


def audit_lately(cfg, limit=4):
    """Recent real CareKeeper actions from the hub audit (write/delete only)."""
    if not cfg:
        return []
    try:
        audit = get_audit(cfg["audit"]["db"])
    except Exception:
        return []
    rows = audit.recent(60)
    devices = load_devices()
    out = []
    for r in rows:
        if len(out) >= limit:
            break
        if not r.get("allowed"):
            continue
        if r.get("permission") not in ("write", "delete"):
            continue
        rtype = r.get("resource_type", "")
        if rtype in ("report", "telemetry"):
            continue
        rname = r.get("resource_name", "")
        meta = {}
        try:
            meta = json.loads(r.get("metadata") or "{}")
        except ValueError:
            pass
        # device from resource name prefix, e.g. m7.maintenance -> m7-ultra
        dev_name = None
        prefix = rname.split(".")[0].lower()
        for key in devices:
            if key.split("-")[0].lower() == prefix or key.lower().startswith(prefix):
                dev_name = device_display(key, devices)
                break
        if not dev_name and cfg:
            dev_name = device_display(cfg.get("device_id", ""), devices) or None
        if not dev_name:
            dev_name = "your computer"
        freed = meta.get("freed_gb")
        if freed:
            text = f"Cleared {freed} GB of junk from {dev_name}"
        elif rtype == "cleanup":
            text = f"Ran cleanup on {dev_name}"
        elif rtype == "tool" and rname.startswith("fix."):
            text = f"{humanize_action(rname)} on {dev_name}"
        else:
            text = f"{humanize_action(rname)} on {dev_name}"
        out.append((day_label(r.get("timestamp", time.time())), text))
    return out


# ---------------------------------------------------------------------------
# Render (report style)
# ---------------------------------------------------------------------------
def render(machines: dict, cfg: dict = None, out_path: str = None) -> str:
    out_path = out_path or DASH_PATH
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    devices = load_devices()
    names = list(machines.keys())
    statuses = {n: status_of(t) for n, t in machines.items()}

    # severity for the hero
    order = {"critical": 0, "warn": 1, "offline": 2, "pending": 3, "ok": 4}
    worst = "ok"
    for n, (label, color) in statuses.items():
        if color == RED:
            sev = "critical"
        elif color == AMBER:
            sev = "warn"
        elif "couldn't be reached" in label:
            sev = "offline"
        elif "not set up yet" in label:
            sev = "pending"
        else:
            sev = "ok"
        if order[sev] < order[worst]:
            worst = sev

    checked = sum(1 for t in machines.values()
                  if not (isinstance(t, dict) and "not enrolled yet" in t.get("error", "")))
    warn_n = sum(1 for n in names if statuses[n][1] == AMBER)
    crit_n = sum(1 for n in names if statuses[n][1] == RED)
    off_n = sum(1 for n in names if statuses[n][1] == GRAY
                and "couldn't be reached" in statuses[n][0])
    pend_n = sum(1 for n in names if statuses[n][1] == GRAY
                 and "couldn't be reached" not in statuses[n][0])

    if worst == "critical":
        title = "Something needs your attention."
    elif worst == "warn":
        title = "One thing needs your OK." if warn_n == 1 else "A few things need your OK."
    elif worst == "offline":
        title = "One device couldn't be reached." if off_n == 1 else f"{off_n} devices couldn't be reached."
    elif worst == "pending":
        title = "One device is waiting to be set up." if pend_n == 1 else f"{pend_n} devices are waiting to be set up."
    else:
        title = "All clear — nothing needs you."

    sub_parts = [f"{checked} device{'s' if checked != 1 else ''} checked"]
    if warn_n or crit_n:
        sub_parts.append(f"{warn_n + crit_n} need{'s' if warn_n + crit_n == 1 else ''} your OK")
    if off_n:
        sub_parts.append(f"{off_n} couldn't be reached")
    if pend_n:
        sub_parts.append(f"{pend_n} not set up yet")
    if not sub_parts[1:]:
        sub_parts.append("no action needed")
    subtitle = " · ".join(sub_parts)

    # "lately" from the audit
    lately = audit_lately(cfg)

    # --- measure ---
    f_title = font(27, bold=True)
    f_sub = font(15)
    f_brand = font(17, bold=True)
    f_date = font(13)
    f_sec = font(12, bold=True)
    f_row = font(15)
    f_name = font(15, bold=True)
    f_safe = font(14)
    f_safe_b = font(14, bold=True)
    f_when = font(13)
    f_foot = font(13)
    f_foot_b = font(13, bold=True)
    f_logo = font(14, bold=True)

    hero_h = 46 + 34 + 24 + 8 + 24
    dev_head_h = 40
    row_h = 46
    safety_h = 30 + 30 + 18
    lat_head_h = 40 if lately else 0
    lat_row_h = 40
    foot_h = 92
    head_h = 76

    H = (head_h + hero_h + dev_head_h + row_h * len(names)
         + safety_h + lat_head_h + lat_row_h * len(lately) + foot_h + 24)

    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)

    # --- header ---
    d.rectangle([(0, 0), (W, head_h)], fill=NAVY_HI)
    d.line([(0, head_h - 1), (W, head_h - 1)], fill=RULE)
    logo_x, logo_y = PAD, (head_h - 28) // 2
    d.rounded_rectangle([(logo_x, logo_y), (logo_x + 28, logo_y + 28)],
                        radius=8, fill=GOLD)
    d.text((logo_x + 7, logo_y + 5), "CK", font=f_logo, fill=NAVY)
    d.text((logo_x + 40, logo_y + 4), "CareKeeper", font=f_brand, fill=GOLD)
    hour = time.strftime("%I").lstrip("0") or "12"
    date_txt = time.strftime(f"%A · {hour}:%M %p", time.localtime())
    d.text((W - PAD - text_w(d, date_txt, f_date), logo_y + 7),
           date_txt, font=f_date, fill=CREAM_DIM)

    # --- hero ---
    y = head_h + 30
    cx, cy = PAD + 24, y + 24
    hero_color = GREEN if worst == "ok" else (RED if worst == "critical" else AMBER)
    d.ellipse([(cx - 24, cy - 24), (cx + 24, cy + 24)],
              outline=hero_color, width=2)
    d.line([(cx - 10, cy), (cx - 3, cy + 8), (cx + 11, cy - 9)],
           fill=hero_color, width=3, joint="curve")
    d.text((PAD + 62, y + 2), title, font=f_title, fill=hero_color)
    d.text((PAD + 62, y + 36), subtitle, font=f_sub, fill=CREAM_DIM)
    y += hero_h

    # --- devices ---
    d.text((PAD, y), "YOUR DEVICES", font=f_sec, fill=CREAM_DIM)
    y += dev_head_h
    name_col = 150
    for n in names:
        label, color = statuses[n]
        dot_y = y + row_h // 2 - 4
        d.ellipse([(PAD, dot_y), (PAD + 9, dot_y + 9)], fill=color)
        disp = device_display(n, devices)
        d.text((PAD + 22, y + row_h // 2 - 10), disp, font=f_name, fill=CREAM)
        txt = truncate(d, row_text(machines[n])[0], f_row, W - PAD * 2 - 22 - name_col - 10)
        d.text((PAD + 22 + name_col, y + row_h // 2 - 10), txt, font=f_row,
               fill=row_text(machines[n])[1])
        if n != names[-1]:
            d.line([(PAD, y + row_h - 1), (W - PAD, y + row_h - 1)], fill=RULE)
        y += row_h

    # --- safety lines ---
    y += 14
    backup_results = [
        b for t in machines.values() if isinstance(t, dict) and "error" not in t
        for b in t.get("backups", {}).get("results", [])
    ]
    checked_devs = [t for t in machines.values()
                    if isinstance(t, dict) and "error" not in t]
    def _real_backup(t):
        return any(b.get("present") and not b.get("empty")
                   for b in t.get("backups", {}).get("results", []))
    devs_with_backups = [t for t in checked_devs if _real_backup(t)]
    if not backup_results:
        safe1 = ("Backups:", " not set up yet")
        safe1_color = GRAY
    elif any(b.get("empty") for b in backup_results) or \
            any(b.get("present") is False for b in backup_results):
        safe1 = ("Backups:", " a folder is empty or missing — Manny will check")
        safe1_color = AMBER
    elif len(devs_with_backups) < len(checked_devs):
        safe1 = ("Backups:", " not set up on every device")
        safe1_color = AMBER
    elif any(b.get("stale") for b in backup_results):
        safe1 = ("Backups:", " one is behind — Manny will check")
        safe1_color = AMBER
    else:
        safe1 = ("Backups:", " all current")
        safe1_color = GREEN
    d.line([(PAD + 4, y - 4), (PAD + 8, y - 4)], fill=safe1_color, width=2)
    d.text((PAD + 20, y - 12), safe1[0], font=f_safe_b, fill=safe1_color)
    d.text((PAD + 20 + text_w(d, safe1[0], f_safe_b), y - 12),
           safe1[1], font=f_safe, fill=CREAM_DIM)
    y += 30

    if worst == "ok":
        sec1, sec_color = ("Security:", " no problems found"), GREEN
    else:
        sec1, sec_color = ("Security:", " some things need your attention"), AMBER
    d.line([(PAD + 4, y - 4), (PAD + 8, y - 4)], fill=sec_color, width=2)
    d.text((PAD + 20, y - 12), sec1[0], font=f_safe_b, fill=sec_color)
    d.text((PAD + 20 + text_w(d, "Security:", f_safe_b), y - 12),
           sec1[1], font=f_safe, fill=CREAM_DIM)
    y += 30

    # --- what CareKeeper did lately ---
    if lately:
        y += 4
        d.text((PAD, y), "WHAT CAREEKEEPER DID LATELY", font=f_sec, fill=CREAM_DIM)
        y += lat_head_h
        for i, (when, text) in enumerate(lately):
            d.text((PAD, y + row_h // 2 - 10), when, font=f_when, fill=GOLD_DIM)
            txt = truncate(d, text, f_row, W - PAD * 2 - 110)
            # bold the leading verb ("Cleared X GB ...")
            d.text((PAD + 110, y + row_h // 2 - 10), txt, font=f_row, fill=CREAM_DIM)
            if i != len(lately) - 1:
                d.line([(PAD, y + lat_row_h - 1), (W - PAD, y + lat_row_h - 1)],
                       fill=RULE)
            y += lat_row_h

    # --- footer ---
    y = H - foot_h
    d.rectangle([(0, y), (W, H)], fill=FOOT_BG)
    d.line([(0, y), (W, y)], fill=FOOT_RULE)
    msg1 = "This is CareKeeper's automatic update. "
    msg1b = "Nothing here needs clicking"
    msg1c = " — if anything needs you, it comes to your messages."
    y2 = y + 20
    d.text((PAD, y2), msg1, font=f_foot, fill=CREAM_DIM)
    x = PAD + text_w(d, msg1, f_foot)
    d.text((x, y2), msg1b, font=f_foot_b, fill=GOLD)
    x += text_w(d, msg1b, f_foot_b)
    d.text((x, y2), msg1c, font=f_foot, fill=CREAM_DIM)
    d.text((PAD, y2 + 28), "CareKeeper by Mu2 Solutions", font=f_foot, fill=GOLD_DIM)

    img.save(out_path)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="CareKeeper dashboard render")
    ap.add_argument("--send", action="store_true",
                    help="render and send via Manny")
    args = ap.parse_args()

    cfg = load_config()
    machines = collect_all(cfg)
    path = render(machines, cfg, OUT)
    print("dashboard:", path)
    for name, tele in machines.items():
        label, _ = status_of(tele)
        print(f"  {name}: {label}")

    if args.send:
        sys.path.insert(0, BASE)
        from manny_bot import TelegramBot
        token = open(os.path.join(BASE, "state", "bot_token.txt")).read().strip()
        bot = TelegramBot(token, cfg["telegram"]["chat_id"])
        bot.send_photo(cfg["telegram"]["chat_id"], path,
                       "Your fleet at a glance — CareKeeper")
        print("sent via Manny")


if __name__ == "__main__":
    main()
