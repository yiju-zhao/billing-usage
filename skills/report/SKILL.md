---
description: Per-day (UTC) Claude Code token usage and list-price API-equivalent cost since a billing-cycle start date, summed across the devices in a devices file (this machine only when there is none)
argument-hint: <cycle-start YYYY-MM-DD> [--end YYYY-MM-DD] [--devices FILE] [--timeout SECONDS]
disable-model-invocation: true
allowed-tools: Bash(python3 *billing_usage.py*)
---

Run this command, then copy its output unchanged into your reply inside a code block (tool
output is collapsed or hidden in many views, so the reply must carry the tables):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/billing_usage.py" --start $ARGUMENTS
```

If `$ARGUMENTS` is empty, ask for the billing-cycle start date (YYYY-MM-DD) instead of running it.

After the output, add one line on scope: the figures cover the devices listed in the output
(this machine only when there is no devices file). They exclude other devices, claude.ai and
sessions already deleted by `cleanupPeriodDays`. If a device shows `UNREACHABLE`, say that the
totals leave it out and relay its `fix:` line. `cost_usd` is what the same tokens would cost at
API list price, not what a subscription is billed. The Console Usage page remains the
authority for billing.

When there is no per-device table and the user wants other devices counted, or a device stays
`UNREACHABLE`, suggest `/billing-usage:setup`, which creates the devices file and tests each
connection.
