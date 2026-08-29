# CareKeeper

A local-first computer-care agent for families: watch telemetry, explain health in plain language, and fix with consent. Built by [Mu2 Solutions](https://mu2.solutions).

CareKeeper is a small harness around a local LLM (llama.cpp + Granite). The model proposes; code verifies. Nothing ships to a customer unless it passes the plain-language dictionary gate, and no write happens without a single-use approval token.

**Status:** v0.1.0 — family-network dogfood on 3 rigs (daily fleet checks + weekly review live via systemd timers).

## Architecture

```
CUSTOMER RIG (edge)                        MU2 HUB
  care_agent.py (systemd timer, tier-gated)  Agent OS (bus / registry /
  llama.cpp + Granite edge brain       <-WireGuard->  capability gate / audit trail)
  local audit + fix executor (Class 1)        fleet_check (daily + weekly)
                                              brain report (5 personas)
                                              Telegram bot (status/approve)
                                              dashboard.py (report-style PNG)
                                              failure alert (OnFailure)
```

- **Edge** (`care_agent.py`): telemetry (disk, SMART, backups, patches, load) + consent-gated fix executor + hash-chained audit.
- **Brain** (`brain.py`): plain-language reports via a local llama-server, then a banned-words dictionary gate — retry once, then a guaranteed-clean template. The LLM proposes, code verifies. Five personas share the same rules; only the voice differs.
- **Fleet** (`fleet_check.py`): pull telemetry from every enrolled device over WireGuard, compose one family report (`--once` daily, `--weekly` week-in-review from the audit trail), audit, deliver via the bot.
- **Bot** (`manny_bot.py`): Telegram — `/status`, `/dashboard`, `/propose`, `/recent`, inline approve/deny.
- **Dashboard** (`dashboard.py`): report-style customer view rendered to PNG (zero extra deps) and delivered with every report.
- **Enrollment** (`enroll.py`): allocate a WireGuard address, stage the agent bundle, deploy over SSH, verify telemetry round-trip.
- **Services** (`deploy/`): user systemd units + timers for the edge brain, the Telegram bot, the daily fleet check (07:30), the weekly review (Sun 08:00), and a failure alert that pings the owner when a check misses its delivery.

## Quick start

```bash
cp config.yaml.example config.yaml   # set device_id, tier, persona, telegram chat_id
python3 care_agent.py --check        # real telemetry
python3 care_agent.py --report       # plain-language + [dictionary check: PASS]
python3 care_agent.py --audit        # readable audit trail
python3 care_agent.py --verify-chain # CHAIN OK (tamper-evident audit)
```

### Personas

`persona:` in `config.yaml` — `manny` (default) | `steady` | `sage` | `guardian` | `tidy`. Voice only: the trust contract, permission classes, and plain-language dictionary are identical across all five. Preview any voice with `python3 fleet_check.py --once --no-send --persona sage`.

### Backup folders

Point `telemetry.backups` at real folders (one per device, absolute paths). The agent checks freshness every run and is honest about it: missing folder → "couldn't be found", empty folder → "nothing backed up yet", stale → "older than expected". It never calls an empty folder "fresh".

### Fleet + weekly

```bash
python3 fleet_check.py --once        # daily family report (also 07:30 via timer)
python3 fleet_check.py --weekly      # week-in-review from the audit trail (Sun 08:00)
python3 fleet_check.py --no-send     # dry-run: report + audit, no Telegram
```

### Consent-gated fix (Class 1)

```bash
python3 care_agent.py --propose rotate-logs
python3 care_agent.py --fix rotate-logs --approve <TOKEN>   # backup -> fix -> audit
```

The `watch` tier disables the fix executor entirely (free plan). Every fix writes a backup first and records before/after in the hash-chained audit.

### Fleet enrollment

```bash
python3 enroll.py new NAME --ip 10.0.0.N     # allocate + stage
python3 enroll.py deploy NAME --host HOST --user-install
python3 enroll.py list
```

## Design rules (locked)

- **Watch always, write never without consent.** Class 0 telemetry is automatic; every Class 1 write needs a single-use token (10 min TTL, consumed on use, replay-denied).
- **Backup before every write.** The audit records the backup path and before/after state.
- **One binary, tier-gated.** `tier: watch` disables the executor (free); `full` enables it (paid).
- **Gate-before-ship.** Brain output must pass the plain-language dictionary (no partition, patch, reboot, malware, RAM, CPU, SSD, uptime, file paths, or misleading "X% left/available" framing) or it does not ship. Never ship a failing report.
- **Honesty over reassurance.** Unknown drive health → "the check couldn't run", never "drives are fine". Empty backup folder → "nothing backed up yet", never "all current".
- **Hash-chained audit.** Tamper-evident trail via Agent OS `AuditTrail` (local fallback included).

## Security notes

- `state/`, `enroll/`, `audit/`, `backups/`, and live `config.yaml` are gitignored — never commit tokens, keys, or runtime state.
- The dashboard is a report, not a control panel: customers read; they don't operate. Anything needing a click lives in the Telegram message.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — including the honesty rules that every report must pass.

## License

MIT — see LICENSE.

CareKeeper by Mu2 Solutions · AI literacy for rural and underserved communities.
