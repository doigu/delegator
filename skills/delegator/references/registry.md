# Registry and configuration

Model registry commands, the config file, provider agreements, installation of
the PowerShell launcher, and `sync`. The everyday launch/acceptance cycle lives
in [SKILL.md](../SKILL.md); harness addressing and mechanics in
[harnesses.md](harnesses.md).

## Config file

The owner maintains `~/.agents/delegator.json` (override: `--config`, then
`DELEGATOR_CONFIG`). It survives skill replacement. Start with
`delegator.example.json`, then explicitly `add` chosen connections/models. Do
not import all native models or change credentials. Codex uses provider `openai`
for its native OpenAI connection and passes `model_provider="openai"` explicitly
to the CLI; Antigravity uses provider `gemini` as the delegator connection label
with no native provider flag; Claude Code uses provider `anthropic` as the
delegator label for its native Anthropic connection; OpenCode uses its native
`provider/model` selection. Unknown harness records are storable but cannot
execute. Model effort names and default belong to each model entry. The optional
`aliases` key maps a short name to ordered full model routes (`alias add <name>
<routes…>`; every route must be a registered model); `exec -m <alias>` tries the
routes in order on capacity/quota failures (see the result contract), while `-m`
with a full address runs exactly that one worker. Registries created before the
rename keep `codex/default/…` / `agy/default/…` addresses that no longer execute;
migrate once with `python -X utf8 -B <skill-dir>/scripts/migrate_default_providers.py`
(override: `--config`): it backs up the registry, renames only these provider
nodes and matching `hidden` entries, and refuses conflicting configs without
writing.

`available` means eligible under registry flags, quota state, and
executable-module support; it does not confirm CLI installation, account access,
server capacity, or the actual served model.

## Launcher installation

The optional PowerShell launcher is installed with
`& <skill-dir>/scripts/install.ps1 -BinDir <directory-in-PATH>`; it points to
this installed skill and contains no second implementation. The same installer
also writes `delegator-quota.cmd` next to the launcher: a copyable double-click
shortcut that refreshes the quota dashboard and opens it in the default browser
(only when the run succeeds; on failure it prints the exit code and pauses).

## Commands

Registry commands work without a skill/LLM:

```powershell
delegator add codex/openai/gpt-6-luna --efforts low medium high --effort low
delegator add zcode/account:zai-start-plan/GLM-5.3 --override
delegator list --active
delegator off openrouter
delegator on opencode/openrouter
delegator delete opencode/openrouter --recursive --dry-run
delegator delete opencode/openrouter --recursive --yes
delegator sync
delegator sync --add 1,3
delegator sync --prune 1 --yes
delegator hide codex/openai/gpt-5.6-terra
delegator unhide opencode/openrouter
delegator hidden
delegator alias add deepseek-flash opencode/opencode/deepseek/deepseek-v4.1-flash opencode/commandcode/deepseek/deepseek-v4.1-flash
delegator alias list
delegator alias remove deepseek-flash
```

Management selectors match a full address or its literal whole-segment suffix,
case sensitively. `/opencode` is an exact root address. Multiple matches on the
same level are allowed; matches across levels are rejected with candidates.
`on/off` changes only selected flags, preserving descendants; `on` also clears
quota on the affected provider(s): the selected provider, every provider under a
selected harness, or the provider of a selected model. Delete affects registry
records only; nonempty nodes require `--recursive`, multiple/recursive deletion
requires `--yes`, and `--dry-run` writes nothing; when an alias still references
a deleted address, the command warns instead of failing. `add` checks the native
catalog when available: Codex uses its local cache, OpenCode invokes
`opencode models <provider>`, Antigravity invokes `agy models`, and ZCode is
checked against the installed builtin table. Claude Code has no model catalog:
`add` records the full model id without verification. `add --override` (zcode
models only) registers a model the installed builtin table does not list: the
catalog check is skipped, the entry is marked (`list` shows `override`), efforts
come from `--efforts`/`--effort` or, when the model id is known to the table's
reasoning-level rules, from those rules; a model absent from the table entirely
needs explicit `--efforts` or exec cannot select a reasoning level. Deleting the
node removes the mark. `alias add` creates or replaces an alias with ordered
routes, every route must be a registered model at that moment; routes are
accepted comma- or space-separated, and an unquoted PowerShell array is
equivalent; `alias remove` deletes, `alias list` prints names with their ordered
routes, and deleting a route's model leaves the alias in place (exec then
reports that route as unavailable). `list/on/off/delete/alias` are offline.
`--json` supports management scripting.

## `sync`

`sync` reads local catalogs: Codex `$CODEX_HOME/models_cache.json`
(`visibility == "list"`), OpenCode models shown in the desktop app
(`%APPDATA%\ai.opencode.desktop\opencode.global.dat` on Windows and the sibling
app-data layouts, `model.user` entries with `visibility == "show"`; the desktop
never updates the TUI favorite file, so that stale snapshot is only the fallback
when no desktop state exists — `$XDG_STATE_HOME/opencode/model.json`, then
`~/.local/state/opencode/model.json`), and ZCode plan providers
(`access.type == "zhipu-account"`) with their models and reasoning levels from
the installed builtin table (`ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`, else the
desktop install paths). Antigravity is the one exception to the local-files
rule: its source invokes `agy models`, the same call the `add` check makes,
because no local cache exists. Claude Code has no model catalog and is not a
sync source. Apart from that call, sync starts no native CLI and imports nothing
wholesale. With a desktop state, OpenCode is expected to match the shown models
minus `hidden` (scope `desktop-visible`, STALE reason "not visible in the
OpenCode desktop"); in the favorite fallback it is expected to match `favorite`
minus `hidden`. A hand-added non-shown model shows up as STALE. In the favorite
fallback, a user-defined provider that declares a nonempty `models` block in the
config is a closed catalog (the TUI offers exactly those): favorites missing
from it are stale marks on models the provider no longer offers and are dropped.
The desktop path is never checked against the config: the desktop shows each
provider's own model list, which is wider than the config block, so a shown
model outside it is legitimate. OpenCode providers that OpenCode itself cannot
load never become NEW and are marked `unavailable` in the report:
`disabled_providers` members, providers outside `enabled_providers`, and
built-in providers (not user-defined in the config) without a key in
`~/.local/share/opencode/auth.json` (`XDG_CONFIG_HOME`/`XDG_DATA_HOME` are
honored; the Zen provider `opencode` serves free models without a key and stays
available; providers user-defined in the config are never filtered by keys).
Their STALE entries are suppressed like a broken source's, and a missing or
unreadable OpenCode config disables this filtering entirely. ZCode keeps
offering every plan of the installed builtin table — active plans live on the
account server and leave no local machine-readable state, so trim them with
`hide`; a provider whose every offered model is hidden and that holds no
registry entries is omitted from the text table (it stays in `--json` and the
HIDDEN list). ZCode entries are expected to match the installed table; a table
update after an app upgrade marks removed models STALE and new ones NEW —
except `add --override` entries, which are never stale (their count is shown
per provider as `override` in the report). `--add`/`--prune`/`--hide` accept
NEW/STALE indices, exact model addresses, or `all` across all available sources
(`--hide` also accepts provider prefixes known to the report and hides NEW
models or whole providers); a quoted spec (`'1,3'`) and an unquoted PowerShell
array (`1,3`, which the shell passes as separate tokens) are equivalent; `all`
requires `--yes`, and indices resolve against the same invocation, so the
printed `planned:` line is the confirmation. An unavailable, malformed, or empty
source yields a warning and leaves that harness's NEW/STALE empty, so a broken
catalog never prunes the registry. `hide` refuses addresses that are still
registered (delete first) and prefixes covering registered models; hidden
entries are literal prefixes, so `*` is refused rather than silently hiding
nothing; a broader `hide` leaves narrower entries as redundant. `hidden` is
stored in the same config. `--prune` removes the node together with its manual
efforts; restore with `add`, which for Codex requires the model to be in the
catalog again. A concurrent `block_quota` and `sync` are last-writer-wins in
both directions: a block may be lost (one more native failure) or a just-synced
node may be dropped (repeat `sync`). `sync/hide/unhide/hidden` make no LLM turns and use the general exit codes
(`sync` may still invoke `agy models` — its one native catalog call — as
described above).
