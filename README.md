# Rig Keeper — care agent v1 (Phase 0, dev box)

The on-device caretaker: watch telemetry, plain-language reports, consent-gated fixes, hash-chained audit.

## Layout

| File | Purpose |
|------|---------|
| `care_agent.py` | telemetry (Class 0) + fix executor (Class 1, tier-gated) + audit |
| `brain.py` | plain-language weekly report via local Granite 4.1 3B + dictionary gate |
| `config.yaml` | device id, tier (watch/full), brain URL, thresholds |
| `rig-keeper.service` | systemd unit (oneshot; timer in Phase 1) |

## Commands

```bash
# Watch (Class 0): read-only health telemetry
python3 care_agent.py --check

# Weekly plain-language report (brain + dictionary gate)
python3 care_agent.py --report

# Propose a fix -> mints a single-use approval token (10 min TTL)
python3 care_agent.py --propose rotate-logs

# Approve + execute (backup first, then write, then audit)
python3 care_agent.py --fix rotate-logs --approve <TOKEN>

# Readable audit trail / integrity check
python3 care_agent.py --audit
python3 care_agent.py --verify-chain
```

## Design rules (locked)

- **Watch always, write never without consent.** Class 0 is automatic; every Class 1 write needs a single-use token.
- **Backup before every write.** The audit records the backup path and before/after state.
- **One binary, tier-gated.** `tier: watch` disables the executor (the free plan); `full` enables it (paid).
- **The brain proposes, code verifies.** LLM output passes the plain-language dictionary gate or it does not ship. Brain offline -> honest template, never fabricated.
- **Hash-chained audit** via Agent OS `AuditTrail` (same class the bus uses; local fallback included).

## Next (Phase 1)

- Timer-driven daily checks + weekly report delivery (systemd timer)
- Telegram Manny interface + real approval buttons
- Enrollment on the Agent OS bus + WireGuard key
- Port `rotate-logs` to real actions (package updates, service restarts) after the scratch path is proven
