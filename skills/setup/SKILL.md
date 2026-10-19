---
description: Set up the devices file for billing-usage - find candidate devices, write the file, and test each connection
argument-hint: [devices-file-path]
disable-model-invocation: true
allowed-tools: Bash(python3 *billing_usage.py*)
---

Guide the user through creating or extending the devices file, then test every device. Take
one step at a time, ask rather than guess, and keep the user's other entries.

## The file

The path is `${XDG_CONFIG_HOME:-~/.config}/billing-usage/devices.json`, or `$ARGUMENTS` when
it holds a path. It holds addresses and user names, so it stays on the user's machine and
never goes into this plugin or its repository.

```json
{"devices": [
  {"name": "this-machine", "method": "local"},
  {"name": "dev-box", "method": "ssh", "address": "100.64.0.10", "user": "alice"}
]}
```

- `name` (unique) and `method` are required. `local` reads the machine that runs the report.
  `ssh` runs the same script on the device and reads back one line of JSON; nothing is
  installed or written there. These are the only two methods.
- `ssh` needs `address` (an IP or host name). Optional: `user`, `port`, `identity_file`,
  `python` (default `python3`) and `config_dir` (default: the device's own `~/.claude`).
- Every device to count must be listed, this machine included, as `method: local`.

## Steps

1. **Read what exists.** If the file exists, read it and show its devices. Ask whether to add
   to it, change an entry, or start over.
2. **Find candidates (read-only).** Run `tailscale status` if it is installed. Skip phones and
   tablets (iOS, Android); note which computers are offline, since they cannot be read until
   they are back. List host names from `~/.ssh/config` with
   `grep -iE '^\s*Host\s' ~/.ssh/config`, leaving out wildcards and `github.com`. Show the
   candidates and ask which to include. The user may also give an address and method directly.
3. **Fill in each ssh device.** Use the Tailscale IP, or the ssh host name, as `address`. Ask
   for `user` when it may differ from this machine's user, and never guess it. Add this
   machine as `local`, named after `hostname`, if it is not listed.
4. **Write the file.** Create the folder if needed. Write valid JSON.
5. **Test.** Run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/billing_usage.py" --start <today, UTC, YYYY-MM-DD>`
   (add `--devices <path>` when a path was given) and show the per-device table. For each
   `UNREACHABLE` device, relay the `fix:` line under the warning. When the fix is to trust a
   host key, ask the user to run `ssh <address>` themselves with the `!` prefix at the prompt.
   Do not turn off host key checking, edit `known_hosts`, or change the ssh configuration for
   them. After the user acts, run the test again.
6. **Finish** when every device shows `ok`, or the user decides to leave one out (offer to
   remove its entry). Give the file path and say that `/billing-usage:report <cycle-start>`
   now covers all listed devices.
