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
import subprocess
import sys
import time
import urllib.request
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from care_agent import (load_config, propose_fix,
                        execute_fix, get_audit, ACTIONS)
from brain import fleet_report, get_persona, FRIENDLY_NAMES

TOKEN_PATH = os.path.join(BASE, "state", "bot_token.txt")

API = "https://api.telegram.org/bot{token}/{method}"


def welcome_text(cfg: dict) -> str:
    p = get_persona(cfg)
    return (
        f"Hi! I'm {p['name']}, your CareKeeper. 👋\n\n"
        "I watch over your computers so you don't have to think about them. "
        "I check their health, explain what's going on in plain language, and "
        "only change things with your OK.\n\n"
        "Here's what I can do:\n"
        "/status - how are my computers doing right now\n"
        "/dashboard - the family fleet at a glance (picture)\n"
        "/propose - propose a safe fix (you approve before I touch anything)\n"
        "/recent - what I've done lately (the logbook)\n"
        "/help - this message"
    )


def help_text(cfg: dict) -> str:
    p = get_persona(cfg)
    return (
        f"I'm {p['name']}, your CareKeeper. The care IS the product - I watch, "
        "warn, and fix the small stuff *with your permission*.\n\n"
        "Commands:\n"
        "/status - plain-language health report right now\n"
        "/dashboard - the family fleet at a glance (picture)\n"
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

    def send_photo(self, chat_id, photo_path: str, caption: str = ""):
        """Send a photo file as multipart/form-data (urllib, no deps)."""
        import uuid

        boundary = "----CK" + uuid.uuid4().hex
        with open(photo_path, "rb") as f:
            file_bytes = f.read()
        filename = os.path.basename(photo_path)
        parts = []
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                     f"{chat_id}\r\n")
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                     f"{caption}\r\n")
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="photo"; '
                     f'filename="{filename}"\r\n'
                     f"Content-Type: image/png\r\n\r\n")
        body = ("".join(parts)).encode() + file_bytes + \
            f"\r\n--{boundary}--\r\n".encode()
        url = API.format(token=self.token, method="sendPhoto")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type":
                     f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode())


def approval_keyboard(action: str, token: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Yes, fix it", "callback_data": f"fix:approve:{action}:{token}"},
            {"text": "❌ No", "callback_data": f"fix:deny:{action}:{token}"},
        ]]
    }


# ---------------------------------------------------------------------------
# Remote fixes: the consent token is minted ON the device (care_agent.py owns
# single-use + TTL + replay denial). The hub keeps only a routing record
# (token -> device) so the Telegram approve button knows where to relay.
# ---------------------------------------------------------------------------
PENDING_REMOTE = os.path.join(BASE, "state", "pending-remote.json")


def _load_pending_remote() -> dict:
    try:
        data = json.load(open(PENDING_REMOTE))
    except (OSError, json.JSONDecodeError):
        data = {}
    now = time.time()
    stale = [t for t, r in data.items() if r.get("expires", 0) < now]
    for t in stale:
        del data[t]
    if stale:
        _save_pending_remote(data)
    return data


def _save_pending_remote(data: dict):
    os.makedirs(os.path.dirname(PENDING_REMOTE), exist_ok=True)
    with open(PENDING_REMOTE, "w") as f:
        json.dump(data, f)


def _resolve_device(arg: str):
    """Map a user-supplied name to (registry_key, device_dict) or (None, None).

    Accepts the registry key, its friendly name, or an unambiguous prefix
    ('m7' -> m7-ultra, 'dell' -> dell-inspiron, 'the Dell' -> dell-inspiron).
    """
    from fleet_check import load_devices

    devices = load_devices()
    if not devices:
        return None, None
    arg = arg.strip().lower()
    for key, dev in devices.items():
        if key.lower() == arg:
            return key, dev
        if dev.get("friendly_name", "").lower() == arg:
            return key, dev
        if FRIENDLY_NAMES.get(key, "").lower() == arg:
            return key, dev
    matches = [k for k in devices if k.lower().startswith(arg)]
    if len(matches) == 1:
        return matches[0], devices[matches[0]]
    return None, None


def _device_ssh(dev: dict, command: str) -> tuple:
    """Run a care_agent command on a remote device over SSH (BatchMode)."""
    reach = dev.get("reach", {})
    host = reach.get("ssh_host") or dev.get("host")
    path = reach.get("path", "~/carekeeper/care_agent.py")
    user = reach.get("user", "")
    target = f"{user}@{host}" if user else host
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           target, f"python3 {path} {command}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return out.stdout.strip(), out.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"error: {exc}", -1


def _propose_remote(cfg, audit, action, device_key, dev) -> dict:
    """Mint the consent token on the device and record hub-side routing."""
    from fleet_check import extract_json

    out, rc = _device_ssh(dev, f"--propose {action}")
    if rc != 0:
        return {"error": f"couldn't reach {FRIENDLY_NAMES.get(device_key, device_key)}: "
                         f"{out[-120:]}"}
    try:
        res = extract_json(out)
    except ValueError as exc:
        return {"error": f"bad response from {device_key}: {exc}"}
    if "error" in res:
        return res  # e.g. watch-tier denial - pass through verbatim
    token = res.get("token")
    if not token:
        return {"error": f"no approval token from {device_key}"}
    recs = _load_pending_remote()
    recs[token] = {"action": action, "device": device_key,
                   "expires": time.time() + 600}
    _save_pending_remote(recs)
    audit.log(cfg["device_id"], "tool", f"fix.{action}.{device_key}", "call",
              True, "remote fix proposed; device token minted",
              {"device": device_key, "token_hint": token[:8]})
    res["device"] = device_key
    return res


def _execute_remote(cfg, audit, action, token) -> dict:
    """Relay an approved token to the device; the device enforces + audits."""
    from fleet_check import extract_json, load_devices

    recs = _load_pending_remote()
    rec = recs.get(token)
    if not rec:
        return {"error": "approval unknown or expired - run /propose again"}
    device_key = rec.get("device", "")
    dev = load_devices().get(device_key)
    if not dev:
        return {"error": f"{device_key} is no longer in the registry"}
    if time.time() > rec.get("expires", 0):
        del recs[token]
        _save_pending_remote(recs)
        return {"error": "approval expired"}
    out, rc = _device_ssh(dev, f"--fix {action} --approve {token}")
    if rc != 0:
        audit.log(cfg["device_id"], "tool", f"fix.{action}.{device_key}",
                  "write", False, f"remote fix failed: {out[-80:]}",
                  {"device": device_key})
        return {"error": f"the fix couldn't run on "
                         f"{FRIENDLY_NAMES.get(device_key, device_key)}: {out[-120:]}"}
    try:
        res = extract_json(out)
    except ValueError as exc:
        return {"error": f"bad response from {device_key}: {exc}"}
    del recs[token]
    _save_pending_remote(recs)
    if res.get("ok"):
        audit.log(cfg["device_id"], "tool", f"fix.{action}.{device_key}",
                  "write", True, "approved fix executed (remote)",
                  {"device": device_key, "backup": res.get("backup")})
        res["device"] = device_key
        return res
    return {"error": res.get("error", "the fix failed on the device")}


def handle_message(bot: TelegramBot, cfg: dict, msg: dict):
    text = (msg.get("text") or "").strip()
    chat_id = msg["chat"]["id"]
    if chat_id != cfg["telegram"]["chat_id"]:
        bot.send(chat_id, "Sorry - CareKeeper isn't set up for this chat yet.")
        return
    cmd = text.split()[0].lower() if text else ""
    if cmd in ("/start", "/help"):
        bot.send(chat_id, help_text(cfg), parse_mode="Markdown")
    elif cmd == "/status":
        try:
            # fleet-wide, matching the promise ("how are my computers doing")
            from fleet_check import collect_all
            machines = collect_all(cfg)
            report, violations = fleet_report(cfg, machines)
            # fleet_report never returns violations (it retries once, then
            # falls back to the clean template), but guard anyway
            if violations:
                report = "I tried twice but my report came out too technical."
            bot.send(chat_id, report)
        except Exception as exc:
            bot.send(chat_id, f"I hit a snag checking things: {exc}")
    elif cmd == "/propose":
        parts = text.split()
        action = parts[1] if len(parts) > 1 else ""
        device_arg = parts[2] if len(parts) > 2 else ""
        if not action or action not in ACTIONS:
            bot.send(chat_id, "Available safe fixes:\n"
                              + "\n".join(f"• {a} - {v['desc']}" for a, v in ACTIONS.items())
                              + "\n\nUsage: /propose <fix> [device] "
                              "(e.g. /propose rotate-logs dell)")
            return
        audit = get_audit(cfg["audit"]["db"])
        device_disp = None
        if device_arg:
            key, dev = _resolve_device(device_arg)
            if not key:
                bot.send(chat_id, f"I don't see a device called '{device_arg}'. "
                                  f"Try /dashboard for the list.")
                return
            device_disp = FRIENDLY_NAMES.get(key, key)
            if dev and dev.get("reach", {}).get("local"):
                res = propose_fix(cfg, audit, action)  # this computer
            else:
                res = _propose_remote(cfg, audit, action, key, dev)
        else:
            res = propose_fix(cfg, audit, action)
        if "token" in res:
            where = f" on **{device_disp}**" if device_disp else ""
            bot.send(chat_id,
                     f"Here's what I'd like to do{where}:\n\n"
                     f"**{action}** - {res['desc']}\n\n"
                     f"I'll back up everything first, and you can see every "
                     f"step in the logbook after. Approve?",
                     reply_markup=approval_keyboard(action, res["token"]),
                     parse_mode="Markdown")
        else:
            bot.send(chat_id, res.get("error", "Couldn't propose that fix."))
    elif cmd == "/dashboard":
        try:
            sys.path.insert(0, BASE)
            from dashboard import render
            from fleet_check import collect_all
            machines = collect_all(cfg)
            path = render(machines, os.path.join(BASE, "state", "dashboard.png"))
            bot.send_photo(chat_id, path,
                           f"Your family fleet at a glance - "
                           f"{get_persona(cfg)['name']}")
        except Exception as exc:
            bot.send(chat_id, f"I hit a snag rendering the dashboard: {exc}")
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
        p = get_persona(cfg)
        bot.send(chat_id,
                 f"{p['reframe']} Try /status to see how things are looking, "
                 "or /help.")


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
        recs = _load_pending_remote()
        meta = {"token_hint": token[:8]}
        if token in recs:
            meta["device"] = recs[token].get("device", "")
            del recs[token]
            _save_pending_remote(recs)
        audit.log(cfg["device_id"], "tool", f"fix.{action}", "call", False,
                  "owner denied the fix in Telegram", meta)
        bot.answer_callback(cb_id, "Understood - nothing was changed.")
        bot.send(chat_id, f"Understood, {action} is cancelled. Nothing was touched.")
        return
    # approve: remote token if the hub has a routing record, else local
    if token in _load_pending_remote():
        res = _execute_remote(cfg, audit, action, token)
    else:
        res = execute_fix(cfg, audit, action, token)
    if res.get("ok"):
        bot.answer_callback(cb_id, "Done - backed up first, as promised.")
        where = f" on **{FRIENDLY_NAMES.get(res['device'], res['device'])}**" \
            if res.get("device") else ""
        bot.send(chat_id,
                 f"✅ Done: **{action}**{where}\n\n"
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
