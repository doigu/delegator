---
name: delegator
description: Delegate a bounded implementation or read-only review through an explicitly selected native Codex, OpenCode, ZCode, Antigravity (agy), or Claude Code model. Use for ordinary delegation when permitted by the project.
---

# Delegator

Delegate when a compact assignment plus verification costs less than doing the work yourself. Keep authority, acceptance, and external actions with the calling agent. Do not delegate trivial work. Follow the target project's worktree and handoff rules; this tool creates no worktrees.

Resolve `<skill-dir>` to this folder. Python and the chosen authenticated native CLI must already be installed. Run `python -X utf8 -B <skill-dir>/scripts/delegator.py --help` when syntax is unclear; the examples below assume the optional PowerShell launcher in PATH (see [registry.md](references/registry.md) for installation; without it call the script path directly). The owner maintains `~/.agents/delegator.json` (override: `--config`, then `DELEGATOR_CONFIG`): start with `delegator.example.json`, explicitly `add` chosen connections/models, and never import all native models or change credentials. The registry, launcher installation, and every management command are described in [registry.md](references/registry.md).

This file carries the ordinary cycle — assign, launch, wait, accept, resume. The map at the end routes rarer cases to reference files; an ordinary launch needs none of them.

## Assignment and launch

Give the worker a short assignment: result, boundaries, source paths, and one observable verification criterion. Explicitly include any decision requiring the lead or owner; a required decision ends work. One call runs at most one native worker at a time (capacity retries or an alias chain may launch successive attempts):

```powershell
delegator exec -m codex/openai/gpt-6-luna -e low --mode implementation --repo D:\prj\example --prompt-file .tmp\issue-42\task.md
delegator exec -m claude/anthropic/claude-sonnet-5-5 -e high --mode review --repo D:\prj\example -p 'Review the specified diff; report concrete defects only.'
delegator exec -m deepseek-flash -e high --mode review --repo D:\prj\example -p 'Review the specified diff; report concrete defects only.'
```

`--repo` is the Git working-copy root the worker runs in; the default is the current directory, and the path must be that root (`git rev-parse --show-toplevel`). Only one `delegator exec` is active per working copy: a second concurrent launch into the same copy fails with `configuration_error` (`Another delegator worker is active in this working copy`), while separate worktrees/clones have independent locks. Point `--repo` at the copy that holds the assigned work — for a review, the clone containing the changes — and give concurrent workers separate copies; when the intended copy is busy or shared, prepare its own clone/worktree for the delegated task and pass its root.

These are examples, not defaults or promises of account access. Select the model explicitly. Missing/disabled/unknown selection returns available full addresses and their efforts without launching a worker (an unknown alias returns the defined alias names). Review is the default mode. Shell commands follow `--shell none|auto|full`: `none` runs the worker in the harness's most restrictive native mode — no routine shell commands, though implementation edits are still possible, `auto` lets routine commands (tests, builds, assigned cleanup) run behind the native guard while unintended destructive actions are denied, and `full` requests the most permissive native mode (use it only for trusted work; some native gates, like the Codex Windows approval gate, may remain). The default level is `none` for review and `auto` for implementation; an explicit level overrides the default for both modes. The guard targets errors and unintended damage — paths outside the working copy and a short list of never-legitimate commands — not a model deliberately working around it; for example, an allowed OpenCode `bash` tool can still run `python -c` touching outside paths. The levels bound shell commands only: native tools, MCP configuration, and hooks are inherited without isolation, so external actions must be bounded by the assignment and the harness's own permissions. Antigravity is the exception: a headless agy run is meaningful only at `full`, so agy defaults to `full` (`--dangerously-skip-permissions`) in both modes (the result records a warning), an explicit `--shell auto` is a configuration error, and `--shell none` is for prompt-only tasks whose answer needs no tools. Even at `full`, a plan-mode review still does not execute — agy ends the turn asking for confirmation instead of running a command — so a review that needs tools belongs to another harness. The full per-harness native mapping of the three levels is in [harnesses.md](references/harnesses.md).

The calling environment must allow writes to the user profile: every harness's native CLI keeps its state there. A restricted sandbox blocks those writes, so no route can run from it: `exec` and `ask` probe profile writability before any launch and return a `configuration_error` whose reason names the calling environment — stop and request an owner decision instead of trying other models.

## Waiting and reading the result

The wrapper remains a foreground process. Stderr shows a short start message and periodic native-event status; stdout contains one JSON result. A runtime limit `--max-runtime-seconds` is optional for the whole call. Stop the same command through the host or terminal, then confirm completion before another executor. For tasks that can exceed the host's ordinary command timeout — background execution, wake cadence, log-growth checks — read [long-runs.md](references/long-runs.md).

## Acceptance

Accept by reading the compact result, actual Git diff, and the assignment's substantive checks. `completed` means the native worker finished, not that the task is accepted. `changed_paths` is Git status after execution and may contain preexisting edits; compare `preexisting_changed_paths`. Requested and reported selection are distinct; absent native metadata/usage remains unknown — read `reported` per field: `harness` is never reported today, and a null `model`, `provider`, or `effort` is unknown, not confirmed. The answer is capped at 2000 characters with an explicit truncation flag, and when truncated the full redacted answer is saved to `answer_path` next to the attempt's log; full native output is in a redacted log outside Git, request it only when needed, and keep logs local. A failed log/status write must not stop useful work. `output_limited` means the native output hit its length limit; narrow the assignment before another attempt and do not treat it as quota.

Permission denials are reported in `denied_actions` (tool plus redacted detail) and do not fail a run that still reaches the native terminal with a nonempty answer (`completed`); a denial without a terminal answer is `failed`/`needs_user_input`. Read `denied_actions` before accepting: the worker may have skipped a requested step, such as a check. A worker that starts its final answer with `NEEDS_USER_INPUT` is asking, not failing: the result is `needs_input`/`worker_question` with exit code 3, and the `session_id` is kept so you can answer the question and continue with `--resume-session`.

For unusual statuses, unknown result fields, or CLI integration, read [the result contract](references/result.md) for the full field reference; the essentials for ordinary acceptance are above.

## Failures, retries, exit codes

Exit codes: `0` completed/management success, `1` native failure, quota, capacity, native required input, cancellation, or runtime limit; `2` configuration/launch failure; `3` worker question (`needs_input`/`worker_question`), which keeps `session_id` for `--resume-session`. Partial edits remain for inspection. A classified `capacity` failure is retried automatically: up to `--capacity-retries` extra attempts (default 3; config `execution.capacity_retries`) with progressive backoff starting at `--capacity-retry-seconds` (default 30; config `execution.capacity_retry_seconds`), doubling each retry; a retry resumes the failed attempt's observed native session when there is one, otherwise starts fresh with the same prompt, and `--max-runtime-seconds` stays a whole-call budget across attempts. Capacity covers transient provider-side failures — Codex `Selected model is at capacity`, ZCode `The system is busy …`, `Provider returned a server error.`, `Network connection failed for the provider request.`, and other transient provider-side wordings (`internal server error`, `service unavailable`, `bad gateway`, `gateway timeout`, `server_error`, `model is overloaded`, `network connection failed`); generic `429`/rate-limit text stays `native_error`. For a full model address there are no other wrapper retries, fallback, automatic resume, automatic rollback, or model substitution. An alias call is the exception: unavailable routes and route-preparation failures are skipped with warnings, and a `capacity` failure after the route's `--route-retries` retries (per route, instead of `--capacity-retries`) or a confirmed `quota` (which blocks the provider and every model under it) hands off to the next route immediately; every other stop reason, including a worker question, ends the chain. See the result contract for the chain semantics. Native CLI internal network retries may still occur.

## Resume

Resume is explicit only, never automatic: cross-call resume requires `--resume-session` (an in-call capacity retry may still reuse the observed native session). Continue the same session with a delta prompt when `OUTCOME`, `SCOPE`, and `ORACLE` are unchanged and the terminal result carries a `session_id`:

```powershell
delegator exec -m codex/openai/gpt-6-luna -e low --mode implementation --repo D:\prj\example --resume-session <session-id> -p 'Continue: <delta only, no replay>.'
```

After an alias call, resume with the full address of the actual final attempt (from `requested`), not with the alias: `--resume-session` together with an alias is a configuration error. Rules: the session ID must use the exact native format of the selected harness (Codex, Claude Code, and Antigravity UUIDs, OpenCode `ses_…`, ZCode `sess_<UUID>`); a malformed ID fails closed before launch, while a well-formed but foreign ID can only be detected after the run — either way the call fails closed and never continues a silent new dialogue. `--continue` is forbidden: it can pick up a foreign session. Model, effort, mode, and `--repo` stay the same — repeat them on the resume call (an absent `-e` falls back to the model entry's default, an absent `--mode` to review); `--shell` may change between calls — deferred escalation: read `denied_actions`, then either perform the denied action yourself or resume the same session with a higher level. A new outcome, scope, or oracle starts a new session. The result reports `resumed: true`; an unconfirmed or mismatched native session is `failed` (`resume_session_unconfirmed` / `resume_session_mismatch`), never a silent new dialogue.

## Case map

Ordinary launches need only this file. Open a reference when its case applies:

| When | Read |
|---|---|
| Choosing/interpreting `--shell` per harness; harness addressing and protocols (ZCode app-server, Claude stream-json, agy conversations); headless permission details, inherited MCP/hooks limits | [references/harnesses.md](references/harnesses.md) |
| The whole call may exceed the host command timeout — long tasks, but also capacity backoff (30+60+120 s) or an alias chain; background waits, wake cadence, log-growth checks; combining launch+wait+check into one host call | [references/long-runs.md](references/long-runs.md) |
| Unusual statuses, unknown result fields, answer truncation and the native log, retry and alias-chain semantics in full | [references/result.md](references/result.md) |
| Continuing a session of a specific harness (ZCode `setMode`, Claude `--resume`, agy `--conversation`) | [references/harnesses.md](references/harnesses.md) |
| Quick local prompt or model smoke-test without a repository (`ask`, `test`) | [references/ask-test.md](references/ask-test.md) |
| Registry setup and management: `add`/`list`/`on`/`off`/`delete`/`sync`/`hide`/`alias`, config file, launcher installation, `available` semantics, migration of pre-rename addresses | [references/registry.md](references/registry.md) |
| Account quota windows, dashboard formats, scheduled refresh | [references/quota.md](references/quota.md) |
