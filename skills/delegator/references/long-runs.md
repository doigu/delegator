# Long runs and host-wait mechanics

How to wait for `exec` when the task can exceed the host's ordinary command
timeout, and how to save host turns on waiting plus a check. The everyday cycle
launch → result → acceptance needs none of this and lives in
[SKILL.md](../SKILL.md).

## Long runs

If the task can exceed the host's ordinary command timeout, start `exec` using
the host's background-command facility with completion notification. Wait on the
same handle. A host wait returning early does not mean the worker timed out and
never authorizes a second launch. Codex desktop `exec_command` yields a session
handle; use `write_stdin` to await it. Claude Code ordinary Bash can kill a
foreground command at its timeout: use background execution there. Long runs:
omit `--max-runtime-seconds` unless the owner sets a limit; a runtime limit cuts
work mid-turn. Instead, wake periodically while waiting — at an interval just
under the host's prompt-cache TTL (Claude Code: a background `sleep 3300`; Codex
desktop: the `write_stdin` wait), else once per status interval — and check that
the log `run-*.jsonl` is still growing and stderr shows the process alive. Stop
the command through the host only if the log is stale for longer than one status
interval. Completion arrives through the host's wait or notification; do not
poll more often. Pass `--result-file` (exec only) for long unattended runs:
the path must be outside every Git repository and must not exist, and the
final JSON result of the whole call is written there atomically before
stdout. When the handle or the output is lost afterwards, the file carries
the final result; a missing file means the completion is unknown. Before any
repeat, check the session, the log, and the working tree; resume stays the
calling agent's decision, and an absent file never authorizes a rerun.

## Wrapper process mechanics

The wrapper remains a foreground process. Stderr shows a short start message and
status every 1800 seconds (`--status-interval-seconds 3600` for hourly, `0`
disables). Status uses native events only, without LLM calls or invented
progress. ZCode reports the last operation and the number of completed tools
from `tool.updated` events; `last_tool=unknown`/`completed_tools=unknown`
means no telemetry, not zero, and the tool count is a liveness signal, not a
measure of progress. Stdout contains one JSON result. `--max-runtime-seconds` is an
optional limit for the whole call; timeout/cancel closes the native process tree
on Windows. Stop the same command through the host or terminal, then confirm
completion before another executor. Windows Job Objects also close children if
the host terminates the wrapper. There is a small OS scheduling window between
native spawn and assignment to the Job Object; hosts with destructive
parent-only cancellation need their own tree cancellation during that window.

## Combining launch, wait, and a check in one host call

At the calling agent's discretion, combine launch, waiting, and an independent
check already defined before launch into one host tool call when this saves a
turn. Wait for the same worker to exit before running the check, then return
both the worker result and the check's actual output and exit code together. The
calling agent still evaluates acceptance. Keep a separate turn when the result
must be examined to choose the check, or when host limits prevent combining
them. This is an optional calling pattern, not an additional delegator command
or a replacement for substantive verification.
