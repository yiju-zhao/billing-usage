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
totals leave it out. `cost_usd` is what the same tokens would cost at API list price, not what
a subscription is billed. The Console Usage page remains the authority for billing.

## Devices

The devices file is `${XDG_CONFIG_HOME:-~/.config}/billing-usage/devices.json`, or the path
given with `--devices`. It lives on the user's machine, outside this plugin and its repository,
because it holds addresses and user names. Never write it into the repository.

```json
{"devices": [
  {"name": "this-machine", "method": "local"},
  {"name": "dev-box", "method": "ssh", "address": "100.64.0.10", "user": "alice"}
]}
```

- `name` (required, unique) and `method` (required): `local` reads this machine; `ssh` runs
  the same script on the device over `ssh` and reads back one line of JSON. Nothing is
  installed on the device and nothing is written there.
- `ssh` needs `address` (IP or host name). Optional: `user`, `port`, `identity_file`,
  `python` (default `python3`) and `config_dir` (default: the device's own `~/.claude`).
- `local` accepts `config_dir` too. Every device the user wants counted must be listed, this
  machine included.

When the user gives a device's address and connection method, add or edit that entry in the
file (create the folder if needed, keep the other entries), ask for the user name if `ssh`
needs one and it was not given, then run the report and check the device's row in the per-device
table. `ssh` runs with `BatchMode=yes`, so a device needs key or Tailscale SSH access that works
without a prompt. If a row says `Host key verification failed`, tell the user to run
`ssh <address>` once themselves to trust the key; do not turn off host key checking for them.
