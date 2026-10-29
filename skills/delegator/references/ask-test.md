# One-shot commands: `ask` and `test`

`ask` and `test` are local one-shot commands without a repository; they never
touch any Git worktree. Full result semantics (plain-text output contract, JSON
shape, exit codes) are in [the result contract](result.md), section "One-shot
commands".

```powershell
delegator ask -m glm-flash -p 'Hi'
delegator ask -m zcode/account:zai-individual-coding-plan/GLM-5.3-Flash -e high -p 'Summarize this in one line: ...'
delegator test
delegator test -m glm-flash
```

`ask` runs one prompt as one native turn in a fresh temporary directory outside
Git, removed after the run (also on failure). Review-only with shell `none` and
no capacity retries — a full address runs exactly once, an alias runs each route
once and hands off to the next route on capacity/quota without a wait; the Codex
native Git working-copy check is waived for the throwaway directory
(`--skip-git-repo-check`), the sandbox level is not. Without `--json` the answer
is printed as plain text and a failure note (status, reason, and the available
model addresses on a selection error) goes to stderr; a worker question is
printed as the answer and exits 3 without an `ask failed` note; `--json` prints
the full exec result with empty `changed_paths`/`preexisting_changed_paths`.
Exit codes are the same as `exec` (a worker question exits 3). Use it for quick
local prompts and configuration checks; project work still goes through `exec`
with `--repo`.

`test` smoke-tests configured models: for each address — every available model
without `-m`, one explicit address, or an alias expanded to its routes — it
first validates offline (registry availability, native CLI resolution, launch
preparation; a missing ZCode plan key or an unconfigured effort surfaces here
without a request), then sends one live `ask` turn with the fixed prompt
`Reply with exactly the word: Hi` at the model's default effort (or `-e`). An
explicitly named unavailable model fails offline with its reason and no request.
Output is one `address | ok|fail | detail` line per model plus a `passed X/Y`
summary (`--json`: one entry object per line, then `{command, passed, total}`).
Exit `0` only when every tested model passed; a live quota/capacity/auth failure
is that model's `fail`, not a configuration error. `test` spends one real model
request per tested model; use `-m` to bound the cost.
