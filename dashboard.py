#!/usr/bin/env python3
"""
CareKeeper - dashboard.py (v1)

Renders the family fleet health dashboard as a PNG (PIL, zero new deps)
for delivery through Telegram as a photo.

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

from care_agent import load_config
from fleet_check import collect_all

OUT = os.path.join(BASE, "state", "dashboard.png")

# palette (dark theme)
BG = (15, 23, 42)          # slate-900
CARD = (30, 41, 59)        # slate-800
CARD_OFF = (51, 65, 85)    # slate-700 (offline)
TEXT = (226, 232, 240)     # slate-100
MUTED = (148, 163, 184)    # slate-400
ACCENT = (56, 189, 248)    # sky-400
OK = (34, 197, 94)
WARN = (245, 158, 11)
CRIT = (239, 68, 68)
UNKNOWN = (148, 163, 184)

FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def rounded_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def status_of(tele: dict) -> tuple[str, tuple]:
    """Return (label, color) for a device telemetry dict."""
    if isinstance(tele, dict) and "error" in tele:
        return "offline", UNKNOWN
    disk = tele.get("disk", {}).get("worst_pct")
    if disk is not None and disk >= 92:
        return "critical", CRIT
    if disk is not None and disk >= 85:
        return "warning", WARN
    smart = tele.get("smart", {}).get("devices", [])
    if any(s.get("health") == "FAIL" for s in smart):
        return "drive problem", CRIT
    stale = any(b.get("stale") for b in tele.get("backups", {}).get("results", []))
    if stale:
        return "attention", WARN
    pending = tele.get("patches", {}).get("pending", 0)
    if pending and pending > 0:
        return "updates waiting", WARN
    return "healthy", OK


def render(machines: dict, cfg: dict = None, out_path: str = None) -> str:
    out_path = out_path or DASH_PATH
    W = 900
    H = 130 + 130 * len(machines) + 60
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # header
    d.text((36, 26), "CAREEKEEPER", font=font(28, bold=True), fill=ACCENT)
    d.text((36, 64), "FAMILY FLEET HEALTH", font=font(15, bold=True),
           fill=MUTED)
    stamp = time.strftime("%b %d, %Y  %H:%M")
    d.text((W - 36 - len(stamp) * 9, 38), stamp, font=font(16), fill=MUTED)
    d.line([(36, 104), (W - 36, 104)], fill=CARD, width=2)

    y = 130
    for name, tele in machines.items():
        offline = isinstance(tele, dict) and "error" in tele
        label, color = status_of(tele)
        card = (36, y, W - 36, y + 108)
        rounded_rect(d, card, 14, CARD_OFF if offline else CARD)
        # status dot
        d.ellipse([(58, y + 24), (78, y + 44)], fill=color)
        # name
        d.text((94, y + 18), name, font=font(22, bold=True), fill=TEXT)
        # status label
        d.text((94, y + 50), label.upper(), font=font(12, bold=True),
               fill=color)

        if offline:
            d.text((W - 300, y + 42), "couldn't be reached",
                   font=font(16), fill=MUTED)
            y += 130
            continue

        # storage bar
        disk = tele.get("disk", {})
        worst = disk.get("worst_pct")
        bar_x, bar_y, bar_w, bar_h = 300, y + 24, 380, 14
        rounded_rect(d, (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
                     7, (51, 65, 85))
        if worst is not None:
            bar_color = CRIT if worst >= 92 else (WARN if worst >= 85 else OK)
            fill_w = int(bar_w * worst / 100)
            rounded_rect(d, (bar_x, bar_y, bar_x + fill_w, bar_y + bar_h),
                         7, bar_color)
        d.text((bar_x + bar_w + 16, y + 18),
               f"storage {worst}%" if worst is not None else "storage ?",
               font=font(15, bold=True), fill=TEXT)

        # metric row
        pending = tele.get("patches", {}).get("pending")
        upd_txt = f"updates: {pending}" if pending is not None else "updates: ?"
        backups = tele.get("backups", {}).get("results", [])
        if backups:
            stale = any(b.get("stale") for b in backups)
            b_txt = "backups: stale!" if stale else "backups: fresh"
        else:
            b_txt = "backups: not set"
        smart = tele.get("smart", {}).get("devices", [])
        if any(s.get("health") == "FAIL" for s in smart):
            s_txt = "drives: PROBLEM"
        elif any(s.get("health") == "unknown" for s in smart):
            s_txt = "drives: uncheckable"
        elif smart:
            s_txt = "drives: ok"
        else:
            s_txt = "drives: not checked"
        d.text((300, y + 52), upd_txt, font=font(14), fill=TEXT)
        d.text((440, y + 52), b_txt, font=font(14), fill=TEXT)
        d.text((640, y + 52), s_txt, font=font(14), fill=TEXT)

        # load state
        load_state = tele.get("load", {}).get("state", "ok")
        load_color = OK if load_state == "ok" else (
            WARN if load_state == "warn" else CRIT)
        d.text((820, y + 46), "●", font=font(18), fill=load_color)
        d.text((806, y + 74), "activity", font=font(10), fill=MUTED)

        y += 130

    d.line([(36, H - 44), (W - 36, H - 44)], fill=CARD, width=2)
    d.text((36, H - 32), "Manny · CareKeeper — your computers, watched over",
           font=font(13), fill=MUTED)
    img.save(out_path)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="CareKeeper dashboard render")
    ap.add_argument("--send", action="store_true",
                    help="render and send via Manny")
    args = ap.parse_args()

    cfg = load_config()
    machines = collect_all(cfg)
    path = render(machines, OUT)
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
                       "Your family fleet at a glance — Manny")
        print("sent via Manny")


if __name__ == "__main__":
    main()
