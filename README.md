# Wakeful

[![CI](https://github.com/stuckj/wakeful/actions/workflows/ci.yml/badge.svg)](https://github.com/stuckj/wakeful/actions/workflows/ci.yml)

A tray app for KDE Plasma 6 that keeps the computer awake for a set time, or while a coding agent (Claude Code, Codex, opencode) is working — sleep only, or with the screen on too — then releases the block by itself, and leaves nothing behind if it crashes. Packaged for apt, dnf and Nix.

Plasma's own "Manually block sleep and screen locking" switch has no time limit and always keeps the screen on and unlocked too. Tools such as caffeine-ng don't combine a time limit with blocking sleep alone. Wakeful does both: a block lasts 30 minutes, a few hours, or until you turn it off, and by default only sleep is blocked, so the screen still dims, turns off and locks on its usual timers.

## Features

- **Timed blocks** — 30 minutes, 1, 2 or 4 hours, any custom length, or until turned off. Add time to a running block, or stop it early.
- **Agentic checks** — keep awake while Claude Code, Codex or opencode is working, alongside a timer or on their own, and see in the menu which sessions are — see [Coding agents](#coding-agents).
- **Two modes** — *Keep awake, screen can turn off* (the default) blocks idle sleep only. *Keep awake and screen on* also blocks dimming, screen-off and screen locking.
- **Nothing left behind** — the block ends when the time runs out, when you turn it off, and when Wakeful exits or crashes.
- **Visible where you expect it** — the icon shows whether a block is keeping the computer awake and in which mode; the tooltip shows the time left; the battery applet lists Wakeful among the applications blocking sleep, with the reason *Keeping awake: 47 min left*.
- **Optionally ignore closing the lid** (off by default) — see [Closing the lid](#closing-the-lid).
- **A command line** to drive the running tray, for scripts and keyboard shortcuts.
- A notification when time runs out (can be turned off), and *Start at login*.

## Installation

Wakeful needs KDE Plasma 6, Python 3.11 or newer and PyQt6. Plasma 6 means Debian 13, Ubuntu 25.04 or Fedora 40 and later.

### Debian/Ubuntu (APT)

```bash
curl -fsSL https://stuckj.github.io/wakeful/gpg-key.asc | sudo gpg --dearmor -o /usr/share/keyrings/wakeful.gpg
echo "deb [signed-by=/usr/share/keyrings/wakeful.gpg] https://stuckj.github.io/wakeful/apt stable main" | sudo tee /etc/apt/sources.list.d/wakeful.list
sudo apt update
sudo apt install wakeful
```

### Fedora (DNF)

```bash
sudo tee /etc/yum.repos.d/wakeful.repo << 'EOF'
[wakeful]
name=wakeful
baseurl=https://stuckj.github.io/wakeful/yum
enabled=1
gpgcheck=1
gpgkey=https://stuckj.github.io/wakeful/yum/gpg-key.asc
EOF
sudo dnf install wakeful
```

Both repositories, and an APT archive holding every version, are described on the [package repository page](https://stuckj.github.io/wakeful/).

### Nix

The flake provides the package and a Home Manager module:

```nix
{
  inputs.wakeful.url = "github:stuckj/wakeful";

  # In your Home Manager configuration:
  imports = [ inputs.wakeful.homeManagerModules.default ];
  services.wakeful.enable = true;   # installs it and starts it with the Plasma session
}
```

Or just the package: `nix profile install github:stuckj/wakeful`, or add `inputs.wakeful.packages.${system}.default` to `home.packages` or `environment.systemPackages`.

### From source

```bash
git clone https://github.com/stuckj/wakeful && cd wakeful
python3 -m wakeful   # with src/ on PYTHONPATH, e.g. PYTHONPATH=src python3 -m wakeful
```

PyQt6 comes from your distribution: `python3-pyqt6` and `qt6-svg-plugins` on Debian/Ubuntu, `python3-pyqt6` and `qt6-qtsvg` on Fedora.

## Usage

Run `wakeful` (or start it from the application menu) and an icon appears in the system tray:

- **Right-click** opens the menu: *Keep awake for* a preset or custom length, or the agentic checks below them; *Add time* and *Stop the timer*; the mode; and the options.
- **Left-click** steps through three settings: off → agentic checks → agentic checks and a timer of the length you last used (an hour, the first time) → off. Turning off keeps which agents are ticked, so the next click watches the same ones. With no agent ticked, a click just starts or stops the timer.

The icon has three states:

| Icon | Meaning |
| --- | --- |
| Closed grey eye | Nothing is enabled: sleep is allowed. |
| Half-lidded orange eye | Agentic checks are on but no agent is working and no timer runs: sleep is allowed. |
| Open orange eye | The computer is being kept awake, by the timer or by a working agent. With rays when the screen is kept on too. |

From a terminal, or bound to a key under *System Settings → Keyboard → Shortcuts → Add New → Command or Script*:

```bash
wakeful start 1h            # keep awake for an hour; the screen may still turn off
wakeful start --screen 90   # 90 minutes, screen kept on
wakeful start               # until stopped
wakeful extend 30m
wakeful stop                # stop the timer; agentic checks carry on
wakeful agents on           # agentic checks on (off to turn them off)
wakeful agents codex off    # untick one agent: claude, codex or opencode
wakeful status
```

These talk to the running tray, so it must be running first.

## How it works

Wakeful asks the session's power management not to sleep, using the standard freedesktop D-Bus interfaces that Plasma's own battery applet uses:

| Mode | Interface | Plasma component | Blocks |
| --- | --- | --- | --- |
| Screen can turn off | `org.freedesktop.PowerManagement.Inhibit` | PowerDevil | idle sleep |
| Screen on | the above, plus `org.freedesktop.ScreenSaver` | PowerDevil, kscreenlocker | idle sleep, dimming, screen-off, screen locking |

Both services tie each inhibition to the D-Bus connection that asked for it and drop it when that connection closes. Wakeful holds them on its own connection, so when it exits — normally, by `kill -9`, or by crashing — the block goes with it. Nothing is written to disk for a block, and nothing persists.

Plasma only enforces a new inhibition five seconds after it is requested. When Wakeful updates the "N min left" text it takes the new inhibition first and releases the old one after that delay, so the block never lapses.

`systemd-inhibit --list` does not show these blocks: PowerDevil does not pass them on to logind. Use the battery applet, or:

```bash
busctl --user get-property org.kde.Solid.PowerManagement /org/kde/Solid/PowerManagement/PolicyAgent \
  org.kde.Solid.PowerManagement.PolicyAgent RequestedInhibitions
```

## Closing the lid

Closing the lid is an explicit request to sleep, and Plasma ignores every application's block when it happens. With the option *Also ignore closing the lid* on (it is off by default), Wakeful instead changes Plasma's lid setting — *System Settings → Power Management → When laptop lid closed* — to *Do nothing* for every power profile while a block keeps the computer awake, and puts it back when it stops: when the timer ends, or, for the agentic checks, once agents have stopped working (see [Coding agents](#coding-agents)).

Because that is a saved setting rather than a block that dies with the app, Wakeful takes care to put it back:

- The previous values are written to `~/.local/state/wakeful/lid-restore.json` **before** anything is changed.
- When the block stops keeping the computer awake, each value is put back — unless you changed it in System Settings meanwhile, in which case your change is kept.
- If Wakeful is killed or crashes, a small watchdog process it started puts the values back straight away.
- If both are gone (a power cut, or a reboot during the block), the next start of Wakeful puts them back. Turn on *Start at login* if you use this option.

While it is on, a laptop closed and put in a bag stays awake for as long as the block keeps it awake.

## Coding agents

Below the timer choices in *Keep awake for* are the agentic checks: *Agentic checks enabled*, and a checkmark for each agent Wakeful knows — [Claude Code](https://claude.com/claude-code), [Codex](https://github.com/openai/codex) and [opencode](https://opencode.ai). An agent that is not installed is shown greyed out, and the agents are greyed out while *Agentic checks enabled* is off; turning it off keeps which ones are ticked.

While agentic checks are on, Wakeful keeps the computer awake while any ticked agent of yours on this computer is working, and for a minute after the last one stops, so a pause between two turns does not count. The rest of the time they hold nothing: the computer dims, locks and sleeps on its usual timers, and the icon is the half-lidded eye. They are never turned off for you — not when the computer sleeps, not when a timer ends — and they are remembered when Wakeful restarts. A timer can run at the same time; the computer is kept awake while either wants it. Both use the mode chosen in the menu, and with the lid option on, closing the lid is ignored only while something is keeping the computer awake.

An agent counts as working while it is running a turn: thinking, running a tool or shell, or running a sub-agent. A session sitting at its prompt does not.

While the computer is kept awake, the menu has an *Agents working (N)* submenu listing each working session by agent and name; `wakeful status` lists them too. When the agents are the only reason, the battery applet shows *Agents working:* followed by the names.

How each agent is read, every five seconds while it matters (agentic checks on, or a timer running):

| Agent | Working when | Ignored when | Notes |
| --- | --- | --- | --- |
| Claude Code | its file in `~/.claude/sessions` says it is busy, or running a shell after the turn | the process is gone, or its number was reused | A session waiting for you to approve a command does not count. `$CLAUDE_CONFIG_DIR` is honoured if set in the environment Wakeful was started in. |
| Codex | the last turn event in its rollout file under `~/.codex/sessions` is a start | no process holds the thread's lock in `~/.codex/thread-writer-locks` | A turn waiting for your approval counts as working. `$CODEX_HOME` is honoured as above. |
| opencode | its database (`~/.local/share/opencode/opencode.db`, opened read-only) has an unfinished step | no opencode process that started before the step runs in the session's directory, as after a crash | Waiting for your approval counts as working. The database is not opened while no opencode process runs; while one does, SQLite records each read in the database's shared-memory file, as any reader does. |

None of these are documented interfaces, so a future release of an agent could change them, and Wakeful would stop seeing its sessions until it catches up.

A background shell or tool that never ends, such as a forgotten `tail -f`, keeps the computer awake until it is stopped. The submenu shows which session has it.

The checks cost little: each agent's check takes a few hundred microseconds to under two milliseconds of CPU every five seconds — about a second of CPU an hour at most — and Wakeful already wakes every five seconds whether or not anything is checked.

## Documentation

- [DESIGN.md](DESIGN.md) — how it is put together, and why
- [CONTRIBUTING.md](CONTRIBUTING.md) — building, testing and submitting changes
- [RELEASING.md](RELEASING.md) — releases and the package repositories

## License

MIT — see [LICENSE](LICENSE).
