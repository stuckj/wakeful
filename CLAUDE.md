# Notes for AI agents

Repo-specific things that are easy to get wrong here. Everything useful to humans lives in the
normal docs: [CONTRIBUTING.md](CONTRIBUTING.md) for the checks, [DESIGN.md](DESIGN.md) for the
architecture, [RELEASING.md](RELEASING.md) for releases.

## The invariant

A block must never outlive the process. Inhibitions are held only on Wakeful's own D-Bus
connection; never move them to a helper process, a file, `systemd-inhibit`, or anything else
that can survive Wakeful. The lid option is the single exception and goes through the journal in
`lid.py`. `tests/integration/test_dbus.py` checks both, including after `kill -9`.

## Running the tests

`python3 -m pytest` runs only the unit tests. The D-Bus tests are deselected by marker and must
run under `dbus-run-session -- python3 -m pytest -m dbus tests/integration`. Never run them
against the real session bus. CI fails on any skipped test; give a test a marker rather than
skipping it.

## Facts about Plasma that the code relies on

Checked against the Plasma/6.6 sources of powerdevil and kscreenlocker:

- `org.freedesktop.PowerManagement.Inhibit` cookies are `int32`; `org.freedesktop.ScreenSaver`
  cookies are `uint32`. `UnInhibit` fails unless the cookie goes back with its own type.
- PowerDevil enforces an inhibition only 5 s after it is requested, so a replacement must be
  taken before the old one is released, and the old one kept past that delay.
- PowerDevil does not watch `powerdevilrc`; `refreshStatus` is what makes a lid change take
  effect.
- `systemd-inhibit --list` does not show PowerDevil's inhibitions.

The stub in `tests/integration/stub_services.py` mirrors these; keep it in step if they change.

## Facts about the coding agents that the code relies on

None of these is a documented interface. Each watcher module's docstring documents what it reads.

- **Claude Code**, checked by running 2.1.302 on Linux. `~/.claude/sessions/<pid>.json` (under
  `$CLAUDE_CONFIG_DIR` when set) exists per running process. `status` is `busy`, `shell`, `idle`
  or `waiting`; it reads `shell` after a turn ends while a background shell still runs, and
  `busy` while an in-process subagent runs. `procStart` is field 22 of `/proc/<pid>/stat`;
  `pidDomain` is `linux:<machine-id>:pid:[<pid namespace>]`.
- **Codex**, checked by running codex-cli 0.169.0 on Linux, and in its source
  (`codex-rs/rollout/src/policy.rs`, `writer_lock.rs`). Every turn writes `task_started` then
  `task_complete` (or `turn_aborted`) to its rollout; the first line is `session_meta` with the
  cwd. `thread-writer-locks/<thread>.lock` is flock-held, by the app-server daemon when one runs,
  for as long as the thread is open, not only during a turn. That directory also holds
  `.coordination.lock`, which is not a thread. Approval waits are not persisted.
- **opencode**, schema checked against an installed 1.19.3; behaviour read from source at
  `v1.19.3` and `v2.3.0`. 1.x writes an assistant message without `time.completed` when a step
  starts (`session/processor.ts`); streaming writes nothing. 2.x has `session_v2.time_busy` and
  `time_idle`. Neither records a pid, and the database also holds credentials.

## Surfaces that must stay in sync

A user-visible change to the commands or options touches `app.py`, `docs/wakeful.1` and the
README's usage section. A change to dependencies touches `nfpm.yaml` (both the deb and rpm
lists), `nix/package.nix`, `scripts/test-installed-package.sh`, CI, and the README.

## Tooling

- PyQt6 6.11 deadlocks if `setConnection()` is called on a default-constructed
  `QDBusServiceWatcher`; construct it with the connection.
- `gh pr edit` silently discards edits on repositories with the project-add workflow; use
  `gh api -X PATCH repos/stuckj/wakeful/pulls/<n> --input body.json` and read the result back.
- Never write a bracketed skip-ci marker in a commit message, even when describing one.
