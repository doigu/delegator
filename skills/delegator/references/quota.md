# Quota dashboard

`quota` reads account quota windows without launching any delegated work or
LLM turn. Two narrow exceptions renew an expired access token by making the
harness CLI rewrite its own stored credential: the claude source (a headless
`claude -p --model haiku` probe, see below) and the agy source (an
`agy models` probe) — no quota request itself goes through a model.

```powershell
delegator quota
delegator quota --format json
delegator quota --format html --out D:\tools\quota.html
delegator quota --only codex,zcode --timeout 20
```

Sources: `codex` (via `codex app-server --stdio`), `zcode` (coding-plan api-key
and/or the desktop login JWT `zcodejwttoken` decrypted from
`~/.zcode/v2/credentials.json`, Node required; the api-key yields the 3h/1w
credit windows, the JWT yields one `tokens` window per start-plan model package
with the earliest package expiry in `extras.start_plan_expires_at`), `claude`
(OAuth usage; an access token expired or expiring within a minute is renewed
first by a headless `claude -p --model haiku` probe that makes the claude CLI
refresh its own stored token — the versionless alias tracks the latest haiku, so
there is no model hardcode; if the stored token is still expired afterwards, the
source errors `auth_expired` with a hint to run claude interactively), `agy`
(Antigravity quota from the internal `daily-cloudcode-pa` endpoints with the
CLI's `x-goog-api-client` header — without it they answer a misleading license
error; the OAuth blob comes from the Windows Credential Manager entry
`gemini:antigravity`, Windows only, written by the `agy` login; an expired
access token is not renewed locally — an `agy models` probe makes the agy CLI
refresh its own stored credential with its own OAuth client material (no OAuth
client material is kept in this repository), and quota re-reads the blob: the
source errors `config` when the CLI is not on PATH, `timeout` when the probe
exceeds the budget, and `auth_expired` when the blob stays expired after the
probe, while a credential without a refresh token keeps erroring `config` with
a re-login hint; windows are the summary's group buckets labeled by `bucketId`
(`3p-*` = the Claude and GPT group) plus `<family>-weekly` windows for per-model
families the summary may omit — the gemini rows, the account's primary model,
always come first and stay visible even at zero usage, since no summary group
covers them — while untouched non-gemini families stay in `extras`),
`openrouter`, `commandcode`, and `opencode-go` (keys from
`~/.local/share/opencode/auth.json`, honoring `XDG_DATA_HOME`); `CODEX_HOME` is
honored. `--format text` (default) prints a compact table on stdout; `--json` is
an alias of `--format json` and prints the snapshot; `--format html` atomically
rewrites a single self-contained dashboard file (default
`~/.agents/quota-dashboard.html`) for browser viewing and scheduled runs.
`--only` selects a comma-separated subset (a quoted spec and an unquoted
PowerShell array are equivalent); an unknown name fails with exit `2`. Without
`--only`, sources without local credentials are skipped entirely instead of
surfacing a `config` error: codex needs the CLI on PATH plus
`$CODEX_HOME/auth.json`, zcode a coding-plan api-key entry or the login JWT in
`~/.zcode/v2/credentials.json`, claude an OAuth token in
`~/.claude/.credentials.json`, agy the `gemini:antigravity` credential with a
refresh token in the Windows Credential Manager, and `openrouter`/`commandcode`/
`opencode-go` their keys in the OpenCode auth store — presence is checked
offline, no decryption and no requests; an explicit `--only` always collects the
named sources for diagnosis. Each source gets its own `--timeout` wall-clock
budget (default 30) covering subprocess spawn, handshake, decryption, and
requests; a failure, timeout, or unexpected error in one source becomes that
source's `status: "error"` entry with a classified, redacted `error` reason
(`auth_expired`, `timeout`, `network`, `parse_error`, `config`,
`unexpected_error`) and never blocks the others. Exit codes: `0` when collection
ran (source errors are data, not failures), `2` for CLI/configuration errors.
The snapshot contract — `{version, generated_at, sources: [{source, status:
ok | error, fetched_at, error, windows: [{label, used_percent, used, limit,
unit, window_minutes, resets_at}], extras}]}`, `unit` being `usd | credits |
tokens | requests | null`, windows of different sources not comparable — is
documented in `delegator quota --help`. Secrets never enter the snapshot,
dashboard, or error text.

For a scheduled dashboard refresh, run it under the same user account with the
profile loaded, for example `powershell.exe -NoProfile -ExecutionPolicy Bypass
-File %USERPROFILE%\.local\bin\delegator.ps1 quota --format html` in Task
Scheduler. For an on-demand refresh, copy the installer-written
`delegator-quota.cmd` (next to the launcher, see [registry.md](registry.md))
anywhere and double-click it: it runs the same refresh with `--out
~/.agents/delegator-quota.html` through the launcher and opens that written file
in the default browser only on exit `0`, so a dashboard is shown exactly when
one was written.
