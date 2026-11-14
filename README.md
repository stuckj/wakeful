# Wakeful

[![CI](https://github.com/stuckj/wakeful/actions/workflows/ci.yml/badge.svg)](https://github.com/stuckj/wakeful/actions/workflows/ci.yml)

A tray app for KDE Plasma 6 that keeps the computer awake for a set time — sleep only, or with the screen on too — then releases the block by itself, and leaves nothing behind if it crashes. Packaged for apt, dnf and Nix.

Plasma's own "Manually block sleep and screen locking" switch has no time limit and always keeps the screen on and unlocked too. Tools such as caffeine-ng don't combine a time limit with blocking sleep alone. Wakeful does both: a block lasts 30 minutes, a few hours, or until you turn it off, and by default only sleep is blocked, so the screen still dims, turns off and locks on its usual timers.

## Features

- **Timed blocks** — 30 minutes, 1, 2 or 4 hours, any custom length, or until turned off. Add time to a running block, or turn it off early.
- **Two modes** — *Keep awake, screen can turn off* (the default) blocks idle sleep only. *Keep awake and screen on* also blocks dimming, screen-off and screen locking.
- **Nothing left behind** — the block ends when the time runs out, when you turn it off, and when Wakeful exits or crashes.
- **Visible where you expect it** — the icon shows whether a block is active and which mode; the tooltip shows the time left; the battery applet lists Wakeful among the applications blocking sleep, with the reason *Keeping awake: 47 min left*.
- **Optionally ignore closing the lid** (off by default) — see [Closing the lid](#closing-the-lid).
- **Optionally keep awake while Claude Code is working** (off by default), with the working sessions listed in the menu — see [Claude Code](#claude-code).
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

- **Left-click** starts a block of the length you last used (an hour, the first time), or turns a running block off.
- **Right-click** opens the menu: *Keep awake for* a preset or custom length, *Add time*, *Turn off*, the mode, and the options.

The icon is a closed grey eye while sleep is allowed, an open orange eye while a block runs, and an open eye with rays when the screen is kept on too.

From a terminal, or bound to a key under *System Settings → Keyboard → Shortcuts → Add New → Command or Script*:

```bash
wakeful start 1h            # keep awake for an hour; the screen may still turn off
wakeful start --screen 90   # 90 minutes, screen kept on
wakeful start               # until stopped
wakeful extend 30m
wakeful status
wakeful stop
```

These talk to the running tray, so it must be running first. `wakeful status` also lists the Claude Code sessions keeping the computer awake, if any.

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

Closing the lid is an explicit request to sleep, and Plasma ignores every application's block when it happens. With the option *Also ignore closing the lid* on (it is off by default), Wakeful instead changes Plasma's lid setting — *System Settings → Power Management → When laptop lid closed* — to *Do nothing* for every power profile while a block runs, and puts it back when the block ends.

Because that is a saved setting rather than a block that dies with the app, Wakeful takes care to put it back:

- The previous values are written to `~/.local/state/wakeful/lid-restore.json` **before** anything is changed.
- When the block ends, each value is put back — unless you changed it in System Settings meanwhile, in which case your change is kept.
- If Wakeful is killed or crashes, a small watchdog process it started puts the values back straight away.
- If both are gone (a power cut, or a reboot during the block), the next start of Wakeful puts them back. Turn on *Start at login* if you use this option.

While it is on, a laptop closed and put in a bag stays awake until the block ends.

## Claude Code

With *Keep awake while Claude is working* on (it is off, and only offered when `~/.claude` or `$CLAUDE_CONFIG_DIR` exists), Wakeful blocks idle sleep while any [Claude Code](https://claude.com/claude-code) session of yours on this computer is working, and lets it sleep again once none is. A session counts while it is running a turn, including any subagents, and while a shell it started is still running after the turn has ended, even one that is only waiting. A session sitting at its prompt, or waiting for you to answer a question or approve a command, does not.

While Claude is the reason, the icon shows the block, the menu has a *Claude is working (N)* submenu listing each session by name and whether it is working or running a shell, and the battery applet shows *Claude is working:* followed by the names. A block you start yourself runs alongside, in its own mode; when it ends, the computer stays awake for as long as Claude is still working. The screen still dims, turns off and locks as usual, and the lid option applies only to blocks you start.

Wakeful finds out by reading the files Claude Code keeps for each running process in `~/.claude/sessions`, every five seconds. A file left behind by a process that has gone, or whose process number has been reused, is ignored. These files are Claude Code's own and not a documented interface, so a future Claude Code release could change them and the option would stop seeing sessions until Wakeful catches up.

A background shell that never ends, such as a forgotten `tail -f`, keeps the computer awake until it is stopped. The submenu shows which session has it.

## Documentation

- [DESIGN.md](DESIGN.md) — how it is put together, and why
- [CONTRIBUTING.md](CONTRIBUTING.md) — building, testing and submitting changes
- [RELEASING.md](RELEASING.md) — releases and the package repositories

## License

MIT — see [LICENSE](LICENSE).
