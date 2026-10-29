# Harness specifics

Addressing, native shell-level mapping, protocol mechanics, and per-harness
limits for the five harnesses. The launch/acceptance cycle and the resume rules
live in [SKILL.md](../SKILL.md); the model registry and its provider agreements
in [registry.md](registry.md); status, stop-reason, and exit-code semantics in
[the result contract](result.md).

## Addressing

| Harness | Full address | Session id | Resume mechanics |
|---|---|---|---|
| Codex | `codex/openai/<model>` | UUID | native session id passed back by the adapter; the desktop `exec_command` handle is a process handle, not the conversation id |
| OpenCode | `opencode/<provider>/<model…>` (native selection) | `ses_…` | native session id passed back by the adapter |
| ZCode | `zcode/<plan-provider>/<model>` | `sess_<UUID>` | `session/resume` + `setMode` + `setModel` (below) |
| Claude Code | `claude/anthropic/<full-model-id>` | UUID | explicit `--resume` with a delta prompt (below) |
| Antigravity | `agy/gemini/<model>` | conversation UUID | `--conversation` (below) |

## Native shell-level mapping

The `none` column keeps the review/implementation split; `auto` and `full` are
mode-independent:

| Harness | `none` | `auto` | `full` |
|---|---|---|---|
| Codex | sandbox `read-only` | sandbox `workspace-write` (writes scoped to the working copy; actions outside it are rejected by policy) | `--approve-for-me`, automatic review (resume passes its `-c` equivalent, since `exec resume` rejects the flag; the sandbox bypass flag and `danger-full-access` still leave the headless approval gate in place on Windows, live check 2026-10-24) |
| Claude Code | `--permission-mode plan` (review) / `acceptEdits` (implementation) | `--permission-mode auto` | `--permission-mode bypassPermissions` |
| ZCode | session mode `plan` (review) / `edit` (implementation) | session mode `guarded` | session mode `yolo` |
| OpenCode | read/search only (review) or edits without shell (implementation); unlisted tools denied | `bash` allow with `external_directory` deny and a short deny list (`git push*`, `git clean*`, `format *`, `format.com*`, `Format-Volume*`, `shutdown*`); unlisted tools denied | every permission `allow` |
| Antigravity | `--mode plan` (review) / `--mode accept-edits` (implementation) | unavailable: an explicit `--shell auto` is a configuration error; the agy default level is `full` with a result warning (headless agy denies permissioned actions at any lower level) | `--dangerously-skip-permissions` |

## Calling-environment profile writes

Every harness's native CLI keeps its state in the user profile: `~/.codex`,
`~/.claude`, `~/.zcode`, `~/.local/share/opencode` or `$XDG_DATA_HOME`, and the
Windows Credential Manager for agy. A restricted sandbox (for example Codex
`workspace-write`) blocks those writes, so no route can run from it: `exec` and
`ask` probe profile writability before any launch and return a
`configuration_error` whose reason names the calling environment — stop and
request an owner decision instead of trying other models. Harness-level
redirects (for example `ZCODE_HOME` to a writable directory) do not lift this
early stop. ZCode additionally probes its own home per route, because its
app-server stalls instead of failing when that write is blocked.

## Headless operation details

Every level stays headless: nothing waits for a human. Claude Code pins
`--permission-prompts none` only at `--shell none`; at `auto`/`full` the flag is
dropped and the native classifier decides — plainly safe commands run without a
prompt, risky shapes (an outside `rm -rf`, `python -c`) are denied headless and
land in `denied_actions` (live check 2026-10-24; pinning the flag at `auto`
denies even safe commands). A ZCode permission request is declined; Codex policy
rejections and OpenCode rule denials are reported. OpenCode `none`/`auto` use
only `allow`/`deny`: a headless `ask` is auto-rejected and ends the turn, so an
unlisted tool is denied and hidden from the model instead of being offered;
`todowrite` is allowed in implementation because it only stores the session's
todo list (live check 2026-10-27). A worker that hits an explicit rule denial
may continue with other tools. OpenCode subagents stay denied at `none`/`auto`.
Claude Code `--continue`, `--fallback-model`, and `--strict-mcp-config` are
never passed, and no allow/deny tool lists are added beyond the OpenCode `auto`
deny list above.

Native harness configuration, including MCP configuration, is inherited.
Delegator adds no MCP isolation; native permissions govern tool access.

## ZCode

ZCode uses its app-server protocol rather than a turn-event CLI: the adapter
starts `zcode app-server --stdio`, pushes the plan entitlement, creates a
session, pins the selected model with `setModel`, confirms it with
`session/read` before and after the turn, subscribes, sends the assignment, and
reads the final messages. The model address is
`zcode/<plan-provider>/<model>` (for example
`zcode/account:zai-individual-coding-plan/GLM-5.3`); `--effort` is required —
the explicit flag or the model entry's configured default — and is sent as
`reasoningLevel`. The provider must be a plan in the installed ZCode
builtin table, and the model/effort are validated before launch; an
`add --override` entry (a model the installed table does not list yet — the
account server may offer it before the published table does) skips the model
check and is pushed into the plan provider's model list, so the headless
provider registry accepts it. `add` itself records without reading the
account-server catalog (the installed builtin table is still checked, as for
every zcode `add`). Node is required for an encrypted Coding Plan credential (decrypted in
memory) and for a desktop `zcode.cjs` backend; the backend is discovered from
`ZCODE_BIN`, PATH, or the desktop bundle. The session ID is `sess_<UUID>` and
persists across backend processes: explicit `--resume-session` calls
`session/resume`, re-pins the session mode with `setMode` (the resumed call's
`--shell` level; raised levels apply to the resumed turn), then pins the model
with `setModel` and confirms it before and after the turn like a fresh session.
A foreign or dead id fails closed (`resume_session_unconfirmed`), a mismatched
native echo is `resume_session_mismatch`, and the answer is read only from the
new turn's terminal payload and streaming deltas, never from the resumed
transcript, which still holds prior answers. The session mode follows `--shell`
(`plan`/`edit` for `none`, `guarded` for `auto`, `yolo` for `full`): only `yolo`
is unprotected, and in `plan` even edits are impossible; in every other mode the
boundary is the native mode plus the assignment's `SCOPE`, and after the run
nonempty `changed_paths` means the review was not accepted. ZCode has no native
output-length `limited` signal; `output_limited` is not inferred.

## Claude Code

Claude Code runs `claude --print --verbose --output-format stream-json` with the
prompt on stdin. The model address is `claude/anthropic/<full-model-id>` (for
example `claude/anthropic/claude-sonnet-5-5`); the CLI has no local model
catalog, so `add` records the full id without verification, and a native alias
(`sonnet`) can report a different id and fail closed. Claude Code has no native
output-length `limited` signal; `output_limited` is not inferred. Native
settings, MCP configuration, and hooks are inherited without isolation: only the
`--shell none` levels are bounded by the native mode, and the read boundary of a
review is the assignment's `SCOPE` plus the native `plan` mode. The session ID
is a UUID; resume is explicit `--resume` with a delta prompt only, and the
resumed call's `--permission-mode` carries the new `--shell` level.

Claude Code reports its selection in the `system/init` event, so an alias
request can end in `reported_selection_mismatch` while a mid-turn message model
is ignored; its terminal `result` event finishes only on `subtype == success`
without `is_error`, and `error_max_turns` is an ordinary native failure.

## Codex

Codex desktop `exec_command` yields a session handle; use `write_stdin` to await
it (see [long-runs.md](long-runs.md)). Codex policy rejections surface as
`Rejected(... rejected: <reason>)` router lines on stderr and land in
`denied_actions`.

## Antigravity (agy)

Antigravity reports a terminal `result` event with `status`, `usage`, and a
conversation ID; a non-`SUCCESS` result is a native failure. Its session ID is
reported, and explicit resume passes it back as `--conversation`; a foreign or
dead id makes agy warn on stderr and silently start a new conversation with exit
0, so the reported id is confirmed after the run (`resume_session_mismatch`).

Shell levels (live checks 2026-10-29): `plan` gates every tool behind a
confirmation the headless run cannot get — the turn ends `SUCCESS` asking for
confirmation even with `--dangerously-skip-permissions`, and a requested file
edit is refused. `accept-edits` + `full` executes commands and edits headless.
Under `full` a model attempt is approved instead of denied, but the typical
plan-and-ask review is a `completed` result whose answer asks for
confirmation, so a reliable agy review is prompt-only and a review that needs
tools belongs to another harness. The default-mode fallback (never launched by
delegator, which always passes an explicit `--mode`) prints a
`jetski: … auto-denied` hint on stderr; the adapter surfaces it in `warnings`.

## OpenCode

OpenCode uses its native `provider/model` selection. Denials, subagent blocks,
and the `auto` deny list are described above under "Headless operation details".
