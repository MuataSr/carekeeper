# CareKeeper

A local-first computer-care agent for families: watch telemetry, explain health in plain language, and fix with consent. Built by Mu2 Solutions.

CareKeeper is a small harness around a local LLM (llama.cpp + Granite). The model proposes; code verifies. Nothing ships to a customer unless it passes the plain-language dictionary gate, and no write happens without a single-use approval token.

## Architecture

```
CUSTOMER RIG (edge)                        MU2 HUB
  care_agent.py (systemd timer, tier-gated)  Agent OS (bus / registry /
  llama.cpp + Granite edge brain       <-WireGuard->  capability gate / audit trail)
  local audit + fix executor (Class 1)        fleet_check + brain report
                                              Telegram "Manny" (status/approve)
                                              dashboard.py (report-style PNG)
```

- **Edge** (`care_agent.py`): telemetry (disk, SMART, backups, patches, load) + consent-gated fix executor + hash-chained audit.
- **Brain** (`brain.py`): plain-language report via a local llama-server, then a banned-words dictionary gate — retry once, then a guaranteed-clean template. The LLM proposes, code verifies.
- **Fleet** (`fleet_check.py`): pull telemetry from every enrolled device over WireGuard, compose one family report, audit, deliver via Manny.
- **Manny** (`manny_bot.py`): Telegram bot — `/status`, `/propose`, `/recent`, inline approve/deny.
- **Dashboard** (`dashboard.py`): report-style customer view rendered to PNG (zero extra deps) and delivered with the daily report.
- **Enrollment** (`enroll.py`): allocate a WireGuard address, stage the agent bundle, deploy over SSH, verify telemetry round-trip.

## Quick start

```bash
cp config.yaml.example config.yaml   # set device_id, tier, telegram chat_id
python3 care_agent.py --check        # real telemetry
python3 care_agent.py --report       # plain-language + [dictionary check: PASS]
python3 care_agent.py --audit        # readable audit trail
python3 care_agent.py --verify-chain # CHAIN OK (tamper-evident audit)
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
- **Gate-before-ship.** Brain output must pass the plain-language dictionary (no partition, patch, reboot, malware, RAM, CPU, SSD, uptime, file paths) or it does not ship. Never ship a failing report.
- **Honesty over reassurance.** Unknown drive health -> "the check couldn't run", never "drives are fine".
- **Hash-chained audit.** Tamper-evident trail via Agent OS `AuditTrail` (local fallback included).

## Security notes

- `state/`, `enroll/`, `audit/`, `backups/`, and live `config.yaml` are gitignored — never commit tokens, keys, or runtime state.
- The dashboard is a report, not a control panel: customers read; they don't operate. Anything needing a click lives in the Telegram message.

## License

MIT — see LICENSE.

CareKeeper by Mu2 Solutions · AI literacy for rural and underserved communities.
