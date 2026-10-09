# hermes-omatoast

**Desktop toast notifications for [Hermes](https://github.com/NousResearch/hermes-agent) cron jobs on [Omarchy](https://omarchy.org) / Hyprland.**

Hermes can already send a job's output to Telegram — but that means having your phone in front of you. `hermes-omatoast` adds a **desktop delivery channel**: the result pops up as a native Quickshell notification card in the corner of the screen.

Point a cron job at both channels and you get a toast *and* a Telegram message:

```yaml
deliver: "telegram:YOUR_CHAT_ID,omatoast:desktop"
```

```
┌──────────────────────────────────────────┐
│  🍞 Hermes                          now  │
│                                          │
│  Backup Verification                     │
│  All 12 reference files present.         │
└──────────────────────────────────────────┘
```

Yes, that is a real toast, not a mockup — the headline is your job name and the body is its output.

---

## Requirements

- **Omarchy** (or any Hyprland setup running the Quickshell shell) — the toast is fired through `omarchy-notification-send`, which talks to the `org.freedesktop.Notifications` D-Bus interface. No `mako`/`dunst` needed: Quickshell *is* the notification daemon on Omarchy.
- **Hermes Agent** with plugin support.
- `omarchy-notification-send` on `PATH` (ships with Omarchy at `/usr/bin/omarchy-notification-send`).

Check the prerequisite:

```bash
command -v omarchy-notification-send
```

If that prints nothing, this plugin has nothing to call. On a non-Omarchy box, any `notify-send`-compatible tool works with a one-line change to `NOTIFY_BIN` (see [Porting](#porting-to-other-desktops)).

---

## Install

### From GitHub (recommended)

```bash
hermes plugins install brewmaister/hermes-omatoast --enable
```

`hermes plugins install` accepts an `owner/repo` shorthand or a full Git URL. Because this repo puts `plugin.yaml` at its **root**, no subdirectory fragment is needed. `--enable` skips the enable prompt.

Then:

```bash
hermes plugins validate hermes-omatoast   # optional sanity check
```

### Manually

```bash
git clone https://github.com/brewmaister/hermes-omatoast.git \
  ~/.hermes/plugins/hermes-omatoast
hermes plugins enable hermes-omatoast
```

Enabling it adds `omatoast` as a valid delivery target immediately — no gateway restart required for cron, because cron delivery resolves the platform through its own sender (see [How it works](#how-it-works)).

---

## Usage

### As a cron delivery target

Any Hermes cron job can deliver to a toast:

```yaml
deliver: "omatoast"               # home label
deliver: "omatoast:desktop"       # explicit label
deliver: "local,omatoast:desktop" # keep the file copy too
```

`deliver` accepts a comma-separated list, so combining channels is the normal pattern:

```yaml
deliver: "telegram:123456789,omatoast:desktop"
```

The label after the colon is **cosmetic** — a toast has no routing, so `desktop`, `alerts`, or `whatever` all land in the same place. It only exists to keep the `deliver` string readable and to let you reuse one home-channel convention.

### From the command line

```bash
hermes send -t omatoast:desktop "Build finished

3 tasks green, 0 red."
```

First line becomes the headline, the rest becomes the body.

---

## Settings

Every behaviour is configurable at runtime. Open the settings panel in:

- **Desktop app:** Plugins → `hermes-omatoast` → *manage* → **settings**
- **TUI:** same path
- **Or edit directly:** `config.yaml` → `plugins.entries.hermes-omatoast.settings.<key>`

Changes apply to the **next** message — settings are read per delivery, so there is no restart.

| Setting | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `true` | Master switch. Off = this platform silently drops every message. |
| `app_name` | string | `Hermes` | Name shown as the toast's source app. |
| `min_urgency` | enum → `low` \| `normal` \| `critical` | `normal` | Skip anything below this level. `critical` matches alert-style text only. |
| `only_jobs` | string | *(empty)* | Comma-separated job names or IDs to toast. Empty = every job. When set, non-cron messages are suppressed too. |
| `skip_jobs` | string | *(empty)* | Comma-separated job names or IDs to suppress. |
| `strip_cron_wrapper` | boolean | `true` | Show the job name as the headline instead of Hermes' raw `Cronjob Response: …` wrapper. |

### Filtering semantics

Cron deliveries arrive wrapped by Hermes:

```
Cronjob Response: <job name>
(job_id: <id>)
-------------
<payload>
```

* `only_jobs` / `skip_jobs` match **case-insensitively, as substrings**, against the job name *and* its ID. `only_jobs: "watchdog,backup"` works, and so does `only_jobs: "a1b2c3d4e5f6"`.
* A matching entry in `skip_jobs` always wins over `only_jobs`.
* `min_urgency` is derived from the message text. Anything containing `alert`, `latch`, `error`, `failed`, `failure`, or `critical` (case-insensitive) counts as `critical`; everything else is `normal`.
* A message suppressed by a filter is reported as a **successful no-op**, not an error — deliberately, so filtered toasts never spam the cron delivery error log.
* `strip_cron_wrapper: false` keeps the raw Hermes wrapper if you prefer the original framing.

### Recipes

Only let alert-style jobs interrupt you:

```yaml
min_urgency: critical
```

Toasts for two specific jobs, silence for everything else:

```yaml
only_jobs: "Backup Verification,EC Watchdog"
```

Everything except the chatty mail poller:

```yaml
skip_jobs: "Email Monitor"
```

---

## How it works

The non-obvious part, and the reason this is a plugin rather than a hook:

1. **Hermes has no cron lifecycle hook.** The hook catalog covers session, tool, LLM, kanban and approval events — nothing for "a cron job finished". So there is nothing to subscribe to.
2. **Gateway hooks never load in cron.** Hooks are explicitly skipped in the CLI, TUI, Desktop and cron contexts — cron delivery goes through the **platform send path** instead.
3. Therefore the supported way to catch cron output is `ctx.register_platform()`, which makes `omatoast` a first-class delivery target.
4. Cron may fire with **no live gateway holding the adapter** (that is the normal case for `hermes send` and for jobs that run while the gateway is restarting). `standalone_sender_fn` covers exactly that: it is the lower-level hook the docs recommend "when cron must send from a process without the live gateway".

So the delivery logic lives in **one** pure function that both paths share — the async adapter's `send()` and the standalone sender both call `_deliver()`. There is no code path where a toast behaves differently depending on how it was triggered.

Other design notes:

- **Outbound-only.** Nothing is ever received from a toast, so `handle_message` is never called and no listener is started; `connect()` just marks the adapter live.
- **No chunking.** `max_message_length=0` — a toast is a headline plus one body, so a long message is truncated for the toast (the full text still reaches your real channels).
- **D-Bus fallback.** Cron runs with a reduced environment, so `DBUS_SESSION_BUS_ADDRESS` is resolved explicitly to `unix:path=$XDG_RUNTIME_DIR/bus` when unset. Without this, toasts fire silently into the void from cron but work fine from your shell — a classic trap.
- **`subprocess` guard.** `omarchy-notification-send` is invoked with an argument vector, never a shell string, and `--app-name`/`-u` come before the positional message so message text can never be parsed as a flag.

---

## Troubleshooting

**Toasts work from my shell but not from cron.**
That is the D-Bus environment trap. Verify it resolves:

```bash
env -i HOME="$HOME" XDG_RUNTIME_DIR="/run/user/$(id -u)" \
  omarchy-notification-send --app-name Hermes "cron env test"
```

The plugin already falls back to the runtime-bus socket, so if this works and the plugin still doesn't, check `hermes plugins doctor`.

**`hermes send -t omatoast:desktop` prints `sent` and nothing appears.**
Check the notification state directly — Omarchy persists every popup to disk:

```bash
ls -t ~/.local/state/omarchy/notifications/ | head
ls -t ~/.local/state/omarchy/notifications/history/ | head
```

If the file is there, the notification daemon received it and the issue is your shell/DND settings, not Hermes.

**A job delivers but no toast shows.**
It is almost certainly a filter, not a failure. `enabled`, `min_urgency`, `only_jobs` and `skip_jobs` all suppress silently by design. Set `min_urgency: low` and clear both job fields to confirm, then narrow from there.

**Which target string should I use?**
`omatoast` and `omatoast:desktop` are equivalent. Use a label only if it helps you read the config.

---

## Porting to other desktops

The only Omarchy-specific piece is the binary name. One constant at the top of `adapter.py`:

```python
NOTIFY_BIN = "omarchy-notification-send"
```

Swap it for `notify-send` on a standard freedesktop desktop (`notify-send` takes `-a`/`-u` instead of `--app-name`/`-u`, so the argv construction needs the matching flag). The plugin is otherwise desktop-agnostic: no Quickshell API is used, only the notification D-Bus interface.

---

## Uninstall

```bash
hermes plugins disable omatoast
rm -rf ~/.hermes/plugins/hermes-omatoast
```

Any cron job still delivering to `omatoast` will log a delivery error until you remove it from its `deliver` string.

---

## License

MIT — see [LICENSE](LICENSE).
