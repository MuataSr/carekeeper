# Contributing to CareKeeper

Thanks for caring about family computers. CareKeeper is a small project with a strict trust contract; the rules below are what make that contract real. Please read them before opening a PR.

## The trust contract (non-negotiable)

1. **Watch always, write never without consent.** Class 0 telemetry is automatic. Every Class 1 write needs a single-use approval token (10 min TTL, consumed on use, replay-denied). No exceptions.
2. **Backup before every write.** The audit trail records the backup path and before/after state.
3. **Honesty over reassurance.** If a check couldn't run, the report says so. Never claim "drives are fine", "backups all current", or "no problems found" when the data didn't come back. An empty backup folder is reported as *empty*, not *fresh*.
4. **Gate-before-ship.** Every report the brain produces must pass the plain-language dictionary in `brain.py`. The LLM proposes; deterministic code verifies. Never ship a failing report — retry once, then fall back to the guaranteed-clean template.

## Running the code

```bash
cp config.yaml.example config.yaml   # set device_id, tier, persona, telegram chat_id
python3 care_agent.py --check
python3 care_agent.py --report       # expect [dictionary check: PASS]
python3 care_agent.py --audit
python3 care_agent.py --verify-chain # expect CHAIN OK
python3 fleet_check.py --once --no-send   # fleet report without Telegram
python3 fleet_check.py --weekly --no-send # week-in-review from the audit trail
```

The brain is a local llama-server (see `config.yaml` → `brain.url`); if it's not running, the report falls back to the template and says so — that's the honest path, not a failure.

## What makes a good PR

- **Small and verifiable.** One behavior per PR, with the verification command in the description.
- **Plain language preserved.** Customer-facing output never contains: partition, patch, reboot, malware, virus, daemon, package, RAM, CPU, GPU, SSD, uptime, SMART, temperature, file paths, or misleading "X% left/available" framing. If you touch `brain.py`, run the dictionary tests.
- **No silent state.** If your change can fail (network, device unreachable, brain offline), it must fail honestly — report it, don't paper over it.
- **Secrets stay out.** `state/`, `enroll/`, `audit/`, `backups/`, and live `config.yaml` are gitignored. Never commit tokens, keys, or runtime state.
- **Tests are welcome, not required** — but a runnable `--no-send` verification line is required.

## Good first issues

Look for the `good first issue` label. A great first contribution: making a check fail *honestly* in an edge case we haven't covered yet (e.g., missing `smartctl` on a device, a backup folder that exists but is empty, a device that drops off the network mid-week).

## Questions

Open a discussion or an issue. If it's about the product direction (pricing, Windows port timing, personas), the roadmap is in the issue tracker — trade-offs welcome.
