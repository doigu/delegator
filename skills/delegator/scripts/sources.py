"""Read local harness catalogs for registry synchronisation.

Local files only, with one documented exception: the Antigravity source invokes
`agy models` because no local cache exists (the same call the `add` catalog
check makes). No model is contacted and no other CLI is started.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import registry
import zcode

HARNESSES = ("codex", "opencode", "zcode", "agy")


@dataclass
class SourceResult:
    harness: str
    ok: bool
    candidates: list = field(default_factory=list)
    warning: str | None = None
    # Provider -> reason for providers the harness itself cannot load
    # (OpenCode only: disabled, not allowlisted, or built-in without a key).
    unavailable: dict = field(default_factory=dict)
    # Candidate-scope label when the source picked a non-default catalog
    # (OpenCode desktop-visible instead of TUI favorites).
    scope: str | None = None


def codex_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / "models_cache.json"


def opencode_paths() -> list:
    """Candidate state files: XDG state first, then the default local state."""
    result = []
    state = os.environ.get("XDG_STATE_HOME")
    if state:
        result.append(Path(state).expanduser() / "opencode" / "model.json")
    default = Path.home() / ".local" / "state" / "opencode" / "model.json"
    if default not in result:
        result.append(default)
    return result


# The built-in Zen provider serves free models without any credential.
OPENCODE_ANON_PROVIDERS = ("opencode",)


def opencode_config_paths() -> list:
    """Config files in one base: XDG_CONFIG_HOME when set, else ~/.config."""
    config = os.environ.get("XDG_CONFIG_HOME")
    base = (Path(config).expanduser() / "opencode" if config
            else Path.home() / ".config" / "opencode")
    return [base / "opencode.json", base / "opencode.jsonc"]


def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas outside string literals."""
    out = []
    index, length = 0, len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            out.append(" ")
            continue
        out.append(char)
        index += 1
    stripped = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", stripped)


def opencode_auth_key_names() -> set:
    """Provider names that hold a nonempty key in the OpenCode auth store."""
    data_home = os.environ.get("XDG_DATA_HOME")
    path = ((Path(data_home).expanduser() if data_home
             else Path.home() / ".local" / "share") / "opencode" / "auth.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, dict):
        return set()
    return {name for name, entry in raw.items()
            if isinstance(entry, dict) and isinstance(entry.get("key"), str)
            and entry["key"].strip()}


def opencode_desktop_state_paths() -> list:
    """Candidate OpenCode desktop state files across per-OS app-data layouts."""
    paths = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "ai.opencode.desktop" / "opencode.global.dat")
    if sys.platform == "darwin":
        paths.append(Path.home() / "Library" / "Application Support"
                     / "ai.opencode.desktop" / "opencode.global.dat")
    config = os.environ.get("XDG_CONFIG_HOME")
    paths.append(((Path(config).expanduser() if config else Path.home() / ".config")
                  / "ai.opencode.desktop" / "opencode.global.dat"))
    unique = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def opencode_desktop_visible() -> list | None:
    """(provider, model) pairs shown in the OpenCode desktop model list, or
    None when the desktop store is absent or has no model visibility state —
    the TUI favorite file is the catalog then. The desktop never updates that
    TUI file, so it alone goes stale for desktop users. The store belongs to
    another app: malformed entries are skipped, never fatal."""
    for path in opencode_desktop_state_paths():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("model"), str):
            return None
        try:
            state = json.loads(raw["model"])
        except ValueError:
            return None
        users = state.get("user") if isinstance(state, dict) else None
        if not isinstance(users, list):
            return None
        visible = set()
        for item in users:
            if not isinstance(item, dict) or item.get("visibility") != "show":
                continue
            try:
                registry._valid_segment(item.get("providerID"), "providerID")
                registry._valid_model(item.get("modelID"))
            except registry.ConfigError:
                continue
            visible.add((item["providerID"], item["modelID"]))
        return sorted(visible)
    return None


def _opencode_configs() -> list | None:
    """Parsed opencode.json/opencode.jsonc objects (merged), or None when the
    config is missing or unreadable and config-derived filtering must not run."""
    configs = []
    for path in opencode_config_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        try:
            loaded = json.loads(_strip_jsonc(text))
        except ValueError:
            return None
        if not isinstance(loaded, dict):
            return None
        configs.append(loaded)
    return configs or None


def opencode_config_catalogs() -> dict:
    """Closed model catalogs from the config: provider -> set(model ids) for
    user-defined providers that declare a nonempty `models` block. Providers
    without the block (and built-in providers) map to nothing: their model
    list is not known locally and no catalog check applies."""
    configs = _opencode_configs() or []
    catalogs = {}
    for config in configs:
        providers_block = config.get("provider")
        if not isinstance(providers_block, dict):
            continue
        for name, provider in providers_block.items():
            if not isinstance(name, str) or not isinstance(provider, dict):
                continue
            models = provider.get("models")
            if not isinstance(models, dict) or not models:
                continue
            known = {model for model in models if isinstance(model, str)}
            catalogs.setdefault(name, set()).update(known)
    return catalogs


def opencode_unavailable_providers(providers) -> dict | None:
    """Reasons why OpenCode itself would not load each provider, or None when
    the config is missing or unreadable and no filtering should happen.

    A provider is unavailable when it is disabled_providers, when it falls
    outside enabled_providers, or when it is not user-defined in the config
    (a built-in provider) and holds no key in the OpenCode auth store.
    User-defined providers are never filtered by keys: they were added
    deliberately and OpenCode reports their auth problems itself.
    """
    configs = _opencode_configs()
    if configs is None:
        return None
    disabled, enabled, defined = set(), set(), set()
    enabled_configured = False
    for config in configs:
        for name in config.get("disabled_providers") or []:
            if isinstance(name, str):
                disabled.add(name)
        allow = config.get("enabled_providers")
        if allow is not None:
            enabled_configured = True
            enabled.update(name for name in allow if isinstance(name, str))
        providers_block = config.get("provider")
        if isinstance(providers_block, dict):
            defined.update(name for name in providers_block if isinstance(name, str))
    keyed = opencode_auth_key_names()
    reasons = {}

    def mark(provider, reason):
        if provider not in reasons:
            reasons[provider] = reason

    for provider in providers:
        if not isinstance(provider, str):
            continue
        if provider in disabled:
            mark(provider, "disabled in OpenCode config")
        elif enabled_configured and provider not in enabled:
            mark(provider, "not in enabled_providers")
        elif provider not in defined and provider not in OPENCODE_ANON_PROVIDERS \
                and provider not in keyed:
            mark(provider, "no key in OpenCode auth")
    return reasons


def builtin_table_path() -> Path | None:
    """First existing candidate of the installed ZCode builtin table."""
    return next((path for path in zcode.builtin_table_paths() if path.is_file()), None)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"not found: {path}"
    except (OSError, ValueError) as exc:
        return None, f"unreadable: {path} ({exc})"


def codex_source() -> SourceResult:
    path = codex_path()
    raw, warning = _read_json(path)
    if warning:
        return SourceResult("codex", False, warning=f"Codex catalog {warning}")
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, list):
        return SourceResult("codex", False, warning=f"Codex catalog has no model list: {path}")
    candidates, seen = [], set()
    for item in models:
        slug = item.get("slug") if isinstance(item, dict) else None
        if not isinstance(slug, str) or any(not part.strip() for part in slug.split("/")):
            return SourceResult("codex", False,
                                warning=f"Codex catalog has a malformed model entry: {path}")
        if item.get("visibility") != "list" or slug in seen:
            continue
        seen.add(slug)
        efforts = []
        levels = item.get("supported_reasoning_levels")
        if isinstance(levels, list):
            for level in levels:
                value = level.get("effort") if isinstance(level, dict) else None
                if isinstance(value, str) and registry._EFFORT.fullmatch(value) and value not in efforts:
                    efforts.append(value)
        default = item.get("default_reasoning_level")
        if not isinstance(default, str) or default not in efforts:
            default = None
        candidates.append({"address": f"codex/openai/{slug}", "favorite": False,
                           "efforts": efforts, "default_effort": default})
    candidates.sort(key=lambda candidate: candidate["address"])
    return SourceResult("codex", True, candidates)


def opencode_source() -> SourceResult:
    visible = opencode_desktop_visible()
    if visible is not None:
        # No config-catalog check here: the desktop shows each provider's own
        # model list, which is wider than the TUI config models block, so a
        # shown model outside that block is still legitimate.
        found = {}
        for provider, model in visible:
            address = f"opencode/{provider}/{model}"
            found[address] = {"address": address, "favorite": False,
                              "efforts": [], "default_effort": None}
        candidates = [found[address] for address in sorted(found)]
        unavailable = opencode_unavailable_providers(sorted({p for p, _ in visible}))
        return SourceResult("opencode", True, candidates, unavailable=unavailable or {},
                            scope="desktop-visible")
    paths = opencode_paths()
    raw = used = None
    problems = []
    for path in paths:
        loaded, warning = _read_json(path)
        if warning is None:
            raw, used = loaded, path
            break
        problems.append(warning)
    if used is None:
        return SourceResult("opencode", False,
                            warning="OpenCode state not found or unreadable: " + "; ".join(problems))
    favorite = raw.get("favorite") if isinstance(raw, dict) else None
    if not isinstance(favorite, list):
        return SourceResult("opencode", False,
                            warning=f"OpenCode state has no favorite list: {used}")
    catalogs = opencode_config_catalogs()
    found = {}
    for item in favorite:
        provider = item.get("providerID") if isinstance(item, dict) else None
        model = item.get("modelID") if isinstance(item, dict) else None
        try:
            registry._valid_segment(provider, "providerID")
            registry._valid_model(model)
        except registry.ConfigError:
            return SourceResult("opencode", False,
                                warning=f"OpenCode favorite has a malformed entry: {used}")
        if provider in catalogs and model not in catalogs[provider]:
            continue
        address = f"opencode/{provider}/{model}"
        found[address] = {"address": address, "favorite": True,
                          "efforts": [], "default_effort": None}
    candidates = [found[address] for address in sorted(found)]
    unavailable = opencode_unavailable_providers(
        sorted({item.get("providerID") for item in favorite
                if isinstance(item, dict) and isinstance(item.get("providerID"), str)}))
    return SourceResult("opencode", True, candidates, unavailable=unavailable or {})


def zcode_source() -> SourceResult:
    path = builtin_table_path()
    if path is None:
        return SourceResult(
            "zcode", False,
            warning="ZCode builtin table not found (checked ZCODE_BUILTIN_PROVIDER_CONFIG_FILE "
                    "and install paths)")
    raw, warning = _read_json(path)
    if warning:
        return SourceResult("zcode", False, warning=f"ZCode builtin table {warning}")
    if not isinstance(raw, dict):
        return SourceResult("zcode", False,
                            warning=f"ZCode builtin table is not an object: {path}")
    candidates, seen = [], set()
    for rule in zcode._rules(raw):
        config = rule.get("config") if isinstance(rule, dict) else None
        if not isinstance(config, dict):
            continue
        if ((config.get("access") or {}).get("type") != zcode.PLAN_ACCESS_TYPE):
            continue
        provider = rule.get("providerId")
        try:
            registry._valid_segment(provider, "providerId")
        except registry.ConfigError:
            return SourceResult("zcode", False,
                                warning=f"ZCode builtin table has a malformed provider rule: {path}")
        models = config.get("builtinModelIds")
        if not isinstance(models, list):
            return SourceResult("zcode", False,
                                warning=f"ZCode plan {provider} has no model list: {path}")
        for model in models:
            model = str(model)
            try:
                registry._valid_model(model)
            except registry.ConfigError:
                return SourceResult(
                    "zcode", False,
                    warning=f"ZCode plan {provider} has a malformed model entry: {path}")
            address = f"zcode/{provider}/{model}"
            if address in seen:
                continue
            seen.add(address)
            levels = sorted(value for value in zcode._reasoning_levels(raw, model)
                            if isinstance(value, str) and registry._EFFORT.fullmatch(value))
            candidates.append({"address": address, "favorite": False,
                               "efforts": levels, "default_effort": None})
    candidates.sort(key=lambda candidate: candidate["address"])
    return SourceResult("zcode", True, candidates)


def agy_model_names(stdout: str) -> list:
    """Model ids from `agy models` output; shared with the `add` catalog check."""
    names = []
    for line in stdout.splitlines():
        columns = line.strip().split("\t", 1)
        if len(columns) == 2 and columns[0].strip() and not columns[0].startswith("Fetching"):
            name = columns[0].strip()
            if name not in names:
                names.append(name)
    return names


def agy_source() -> SourceResult:
    try:
        result = subprocess.run(["agy", "models"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SourceResult("agy", False,
                            warning=f"Antigravity catalog unavailable ({exc.__class__.__name__})")
    if result.returncode:
        return SourceResult("agy", False,
                            warning=f"Antigravity catalog failed with exit code {result.returncode}")
    candidates = [{"address": f"agy/gemini/{name}", "favorite": False,
                   "efforts": [], "default_effort": None}
                  for name in agy_model_names(result.stdout)]
    candidates.sort(key=lambda candidate: candidate["address"])
    return SourceResult("agy", True, candidates)


def zcode_catalog_check(provider, model, efforts):
    """Offline add-verification against the installed builtin table."""
    path = builtin_table_path()
    if path is None:
        return 'ZCode builtin table unavailable; entry recorded without catalog verification'
    raw, warning = _read_json(path)
    if warning or not isinstance(raw, dict):
        return 'ZCode builtin table unreadable; entry recorded without catalog verification'
    providers = {rule.get("providerId") for rule in zcode._rules(raw)
                 if isinstance(rule, dict)
                 and ((rule.get("config") or {}).get("access") or {}).get("type")
                 == zcode.PLAN_ACCESS_TYPE}
    if provider not in providers:
        raise registry.ConfigError(f'ZCode plan provider not found in builtin table: {provider}')
    if model not in zcode._plan_models(raw, provider):
        raise registry.ConfigError(f'Model absent from the ZCode builtin table: {provider}/{model}')
    levels = zcode._reasoning_levels(raw, model)
    if levels and any(effort not in levels for effort in efforts):
        raise registry.ConfigError('Effort absent from ZCode builtin table; supported: '
                                   + ', '.join(sorted(levels)))
    return None


def zcode_model_efforts(model):
    """Effort levels for a model id from the installed builtin table's
    modelConfigRules (provider-independent), or [] when the table is
    unavailable or the model is absent from it entirely. Feeds the effort
    derivation for `add --override`; the same filter as zcode_source."""
    path = builtin_table_path()
    if path is None:
        return []
    raw, warning = _read_json(path)
    if warning or not isinstance(raw, dict):
        return []
    return sorted(value for value in zcode._reasoning_levels(raw, model)
                  if isinstance(value, str) and registry._EFFORT.fullmatch(value))


def read_sources() -> dict:
    return {"codex": codex_source(), "opencode": opencode_source(),
            "zcode": zcode_source(), "agy": agy_source()}
