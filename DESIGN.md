# Design

Wakeful keeps a Plasma 6 session awake for a chosen time and then lets it sleep. This document
describes how it is put together and the decisions that constrain it. The code is the authority
on the details.

## The one invariant

**A block never outlives the process that took it.** The bug that started this project was a
sleep block left behind. Every part below is shaped by this: the block exists only as
inhibitions held on Wakeful's own D-Bus connection, and the session's power management drops
them when that connection closes, whatever the reason it closed. Wakeful writes nothing to disk
for a block.

The lid option is the one exception, because the only lever for the lid is a saved setting. It
is handled separately, under [Closing the lid](#closing-the-lid).

## Tray app, not a plasmoid

Wakeful is a Python and PyQt6 program that shows a StatusNotifierItem, not a QML plasmoid. The
deciding question was how each could hold an inhibition tied to its own lifetime.

- A plasmoid runs inside plasmashell, so anything it holds over D-Bus is held by plasmashell. It
  is released when plasmashell exits, not when the widget is removed. The Plasma5Support
  `powermanagement` engine still offers `beginSuppressingSleep`, but it keeps one process-wide
  cookie per kind, files the inhibition under plasmashell, and the battery applet hides
  plasmashell's entries. The 6.6 battery applet's own control is a private QML plugin whose
  state is shared with the applet's manual switch.
- A separate process gets the invariant for free from its D-Bus connection. It can also run
  the lid watchdog and journal described below, which QML cannot do without a compiled plugin.
- Python keeps the deb and rpm architecture-independent and the timer logic unit-testable.

## Components

```text
tray.py      StatusNotifierItem: icon, tooltip, menu, notifications, preferences
control.py   D-Bus interface io.github.stuckj.Wakeful, and the client the CLI uses
app.py       entry point: wires the parts together, runs the tick, handles signals
controller.py   the operations (start, extend, set mode, stop, tick, shutdown)
block.py     the block and its timing rules; no Qt, no D-Bus
inhibit.py   holds the inhibitions over D-Bus
lid.py       lid setting: journal, change, restore; no Qt
lidwatch.py  watchdog process that restores the lid if the main process dies
claude.py    which Claude Code sessions are working; no Qt
```

Dependencies point downwards. `block.py`, `controller.py`, `lid.py` and `claude.py` know nothing
of Qt or D-Bus; the controller sees the inhibitor, the lid guard and the Claude Code watcher only
through small protocols. That is
what lets the timer and lid rules be tested without a bus.

The tray and the D-Bus control interface are two front ends to one controller. Neither keeps
state of its own about the block. Owning the bus name `io.github.stuckj.Wakeful` is also what
makes Wakeful a single instance per session; the command line (`wakeful start 1h`) is a client
of that interface, which is how it serves keyboard shortcuts and scripts.

## Inhibitions

| Mode | Holds | Effect in Plasma 6 |
| --- | --- | --- |
| Screen can turn off | `org.freedesktop.PowerManagement.Inhibit` | PowerDevil's InterruptSession: idle suspend only |
| Screen on | the above and `org.freedesktop.ScreenSaver.Inhibit` | also dimming, screen-off and idle lock (via kscreenlocker) |

These are the interfaces the battery applet itself uses, and `kde-inhibit --power` and
`--screenSaver` call the same two. Wakeful does not use `systemd-inhibit` or logind directly:
PowerDevil, not logind, decides on idle suspend in a Plasma session, and a logind inhibitor
would not show in the battery applet.

The application name passed is `wakeful`, which the battery applet resolves to
`wakeful.desktop` for its name and icon. The reason carries the time left, so it is replaced as
the minutes go by. A replacement is always taken before the old inhibition is released, and the
old one is held until PowerDevil has started enforcing the new one, so the block never lapses.

If one of the services restarts, its inhibitions went with the old process. Wakeful watches the
names and takes them again from the new owner. It never sends an old cookie to a new owner,
where it could match someone else's inhibition.

## Time

Blocks are timed on `CLOCK_BOOTTIME`, which keeps counting through suspend and does not jump
when the wall clock is changed. A block that spent twenty minutes suspended (the lid closed,
with the lid option off) has twenty minutes fewer left on resume.

## Closing the lid

PowerDevil treats closing the lid as an explicit request and bypasses every inhibition. There is
no runtime-only way to suppress it: the lid action is `LidAction` in `powerdevilrc`, read when
PowerDevil loads its profile, and PowerDevil re-reads it only when asked (`refreshStatus`, which
is what System Settings calls after saving).

So the option changes the saved setting, and the design is about getting it back:

1. Record the current value for every profile in a journal file under `$XDG_STATE_HOME`, and
   fsync it, **before** changing anything.
2. Set "Do nothing" for every profile and ask PowerDevil to reload.
3. On release, put back each recorded value, but only where the value is still the one Wakeful
   wrote, so a change the user made meanwhile wins. Delete the journal last.

Three things can do step 3: the main process when the block ends; a watchdog process, which
does it the moment the main process dies (it holds a pipe from the main process and wakes on
EOF); and the next start of Wakeful, which restores any journal it finds. The watchdog runs in
its own session so that a signal to Wakeful's process group does not take it too.

Reading and writing go through `kreadconfig6` and `kwriteconfig6` rather than a parser here, so
the file is handled by the same KConfig code that PowerDevil and System Settings use. Without
those tools the option is not offered.

What it does not do: it does not cover a reboot during a block unless Wakeful starts again
afterwards (hence *Start at login*), and it does not change the separate "when an external
monitor is connected" lid rule.

## Following Claude Code

The controller has two reasons to hold an inhibition: the block the user started, and, with the
option on, Claude Code sessions that are working. It holds one inhibition for both, in the
block's mode while there is a block and sleep-only otherwise, and releases it only when neither
asks. The lid follows the user's block alone: Claude activity never changes a saved setting, so
it adds nothing that can outlive Wakeful.

Claude Code is observed, not told. Wakeful reads the per-process files Claude Code already
writes and checks each against the live process, on the controller's existing tick. Hooks were
the alternative: documented, but they need the user to edit Claude Code's settings, they do not
report a shell left running after a turn, and a hook that announced work and then died would
leave Wakeful holding for a process that is gone. Reading the files costs a dependency on a
format Claude Code does not document; `claude.py` is the only place that knows it.

## What Wakeful deliberately does not do

- Hold anything that persists, other than the lid setting under the journal above.
- Install anything into Claude Code, or keep the computer awake for a Claude Code session that
  is waiting on the user.
- Talk to logind, or need any privilege.
- Support desktops other than Plasma. The freedesktop interfaces exist elsewhere, but the
  verification and the lid handling are Plasma-specific.
