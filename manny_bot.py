#!/usr/bin/env python3
"""
CareKeeper - manny_bot.py (v1, Phase 0)

Manny's Telegram interface: status home base, plain-language reports,
one-tap approval flow for safe fixes. Zero third-party dependencies
(urllib + stdlib only).

Modes:
  --once   process all pending updates once and exit (dev/cron friendly)
  --serve  long-poll loop (deployment)
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from care_agent import (load_config, collect_telemetry, propose_fix,
                        execute_fix, get_audit, ACTIONS)
from brain import make_report, _template_report

TOKEN_PATH = os.path.join(BASE, "state", "bot_token.txt")

API = "https://api.telegram.org/bot{token}/{method}"

WELCOME = (
    "Hi! I'm Manny, your CareKeeper. 👋\n\n"
    "I watch over your computers so you don't have to think about them. "
    "I check their health, explain what's going on in plain language, and "
    "only change things with your OK.\n\n"
    "Here's what I can do:\n"
    "/status - how are my computers doing right now\n"
    "/propose - propose a safe fix (you approve before I touch anything)\n"
    "/recent - what I've done lately (the logbook)\n"
    "/help - this message"
)

HELP = (
    "I'm Manny, your CareKeeper. The care IS the product - I watch, warn, "
    "and fix the small stuff *with your permission*.\n\n"
    "Commands:\n"
    "/status - plain-language health report right now\n"
    "/propose - list a safe fix for approval\n"
    "/recent - the last things I did (the logbook)\n"
    "/help - this message\n\n"
    "Rules I live by: I never touch your stuff without a clear yes, I back "
    "everything up first, and I'm honest when I don't know something."
)


class TelegramBot:
    def __init__(self, token: str, chat_id: int):
        self.token = token
        self.chat_id = int(chat_id)

    def _call(self, method: str, payload: dict) -> dict:
        url = API.format(token=self.token, method=method)
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())

    def get_updates(self, offset: int, timeout: int = 25) -> list:
        try:
            data = self._call("getUpdates", {
                "offset": offset, "timeout": timeout,
                "allowed_updates": ["message", "callback_query"]})
            return data.get("result", []) if data.get("ok") else []
        except Exception:
            return []

    def send(self, chat_id, text, reply_markup=None, parse_mode=None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._call("sendMessage", payload)

    def answer_callback(self, cb_id: str, text: str):
        try:
            self._call("answerCallbackQuery",
                       {"callback_query_id": cb_id, "text": text})
        except Exception:
            pass


def approval_keyboard(action: str, token: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Yes, fix it", "callback_data": f"fix:approve:{action}:{token}"},
            {"text": "❌ No", "callback_data": f"fix:deny:{action}:{token}"},
        ]]
    }


def handle_message(bot: TelegramBot, cfg: dict, msg: dict):
    text = (msg.get("text") or "").strip()
    chat_id = msg["chat"]["id"]
    if chat_id != cfg["telegram"]["chat_id"]:
        bot.send(chat_id, "Sorry - CareKeeper isn't set up for this chat yet.")
        return
    cmd = text.split()[0].lower() if text else ""
    if cmd in ("/start", "/help"):
        bot.send(chat_id, HELP, parse_mode="Markdown")
    elif cmd == "/status":
        try:
            tele = collect_telemetry(cfg)
            report, violations = make_report(cfg, tele)
            # gate-before-ship: violations are impossible now (make_report
            # retries then falls back to the clean template), but guard anyway
            if violations:
                report = ("I tried twice but my report came out too technical. "
                          "Here's the plain version:\n\n"
                          + _template_report(tele))
            bot.send(chat_id, report)
        except Exception as exc:
            bot.send(chat_id, f"I hit a snag checking things: {exc}")
    elif cmd == "/propose":
        action = text.split()[1] if len(text.split()) > 1 else ""
        if not action or action not in ACTIONS:
            bot.send(chat_id, "Available safe fixes:\n"
                              + "\n".join(f"• {a} - {v['desc']}" for a, v in ACTIONS.items()))
            return
        audit = get_audit(cfg["audit"]["db"])
        res = propose_fix(cfg, audit, action)
        if "token" in res:
            bot.send(chat_id,
                     f"Here's what I'd like to do:\n\n"
                     f"**{action}** - {res['desc']}\n\n"
                     f"I'll back up everything first, and you can see every "
                     f"step in the logbook after. Approve?",
                     reply_markup=approval_keyboard(action, res["token"]),
                     parse_mode="Markdown")
        else:
            bot.send(chat_id, res.get("error", "Couldn't propose that fix."))
    elif cmd == "/recent":
        audit = get_audit(cfg["audit"]["db"])
        lines = []
        for e in audit.recent(8):
            ts = time.strftime("%m-%d %H:%M", time.localtime(e["timestamp"]))
            verdict = "✅" if e["allowed"] else "⛔"
            lines.append(f"{ts} {verdict} {e['resource_name']} - {e['reason'][:60]}")
        bot.send(chat_id, "The logbook (last 8):\n" + "\n".join(lines)
                 if lines else "The logbook is empty.")
    else:
        bot.send(chat_id,
                 "I'm Manny, your CareKeeper - I keep your computers healthy, "
                 "I don't chat for sport. Try /status to see how things are "
                 "looking, or /help.")


def handle_callback(bot: TelegramBot, cfg: dict, cb: dict):
    cb_id = cb["id"]
    chat_id = cb["message"]["chat"]["id"]
    if chat_id != cfg["telegram"]["chat_id"]:
        bot.answer_callback(cb_id, "Not authorized")
        return
    data = cb.get("data", "")
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "fix":
        bot.answer_callback(cb_id, "Unknown action")
        return
    _, decision, action, token = parts
    audit = get_audit(cfg["audit"]["db"])
    if decision == "deny":
        audit.log(cfg["device_id"], "tool", f"fix.{action}", "call", False,
                  "owner denied the fix in Telegram",
                  {"token_hint": token[:8]})
        bot.answer_callback(cb_id, "Understood - nothing was changed.")
        bot.send(chat_id, f"Understood, {action} is cancelled. Nothing was touched.")
        return
    # approve
    res = execute_fix(cfg, audit, action, token)
    if res.get("ok"):
        bot.answer_callback(cb_id, "Done - backed up first, as promised.")
        bot.send(chat_id,
                 f"✅ Done: **{action}**\n\n"
                 f"Backup taken first: `{res['backup'].get('backup')}`\n"
                 f"Every step is in the logbook (/recent).",
                 parse_mode="Markdown")
    else:
        bot.answer_callback(cb_id, "Couldn't run that fix.")
        bot.send(chat_id, f"⚠️ {res.get('error', 'The fix failed.')}")


def process_updates(bot: TelegramBot, cfg: dict):
    offset = 0
    updates = bot.get_updates(offset)
    for upd in updates:
        offset = upd["update_id"] + 1
        if "message" in upd:
            handle_message(bot, cfg, upd["message"])
        elif "callback_query" in upd:
            handle_callback(bot, cfg, upd["callback_query"])
    # persist offset so processed updates are not replayed
    with open(os.path.join(cfg["state_dir"], "bot-offset.txt"), "w") as f:
        f.write(str(offset))


def load_offset(cfg: dict) -> int:
    try:
        return int(open(os.path.join(cfg["state_dir"], "bot-offset.txt")).read().strip())
    except (OSError, ValueError):
        return 0


def main():
    import argparse

    ap = argparse.ArgumentParser(description="CareKeeper Manny bot")
    ap.add_argument("--once", action="store_true",
                    help="process pending updates once, then exit")
    ap.add_argument("--serve", action="store_true",
                    help="long-poll loop")
    ap.add_argument("--send", metavar="TEXT",
                    help="send a message to the owner chat (test)")
    args = ap.parse_args()

    cfg = load_config()
    try:
        token = open(TOKEN_PATH).read().strip()
    except OSError:
        print("no bot token at", TOKEN_PATH)
        sys.exit(1)
    bot = TelegramBot(token, cfg["telegram"]["chat_id"])

    if args.send:
        bot.send(cfg["telegram"]["chat_id"], args.send)
        print("sent")
        return

    offset = load_offset(cfg)
    if args.once or args.serve:
        while True:
            updates = bot.get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                if "message" in upd:
                    handle_message(bot, cfg, upd["message"])
                elif "callback_query" in upd:
                    handle_callback(bot, cfg, upd["callback_query"])
            with open(os.path.join(cfg["state_dir"], "bot-offset.txt"), "w") as f:
                f.write(str(offset))
            if args.once:
                break
            time.sleep(1)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
