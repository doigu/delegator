import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
registry = importlib.import_module("registry")
sources = importlib.import_module("sources")
sync = importlib.import_module("sync")


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def fake_agy_result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["agy"], returncode, stdout=stdout, stderr=stderr)


def fake_subprocess_module(run):
    return SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired,
                           CompletedProcess=subprocess.CompletedProcess)


@pytest.fixture
def state(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    # Isolate provider-availability probes from any real OpenCode install.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    table = tmp_path / "zcode" / "zcode-builtin.json"
    write(table, {"config": {
        "providerConfigRules": {"providerRules": [
            {"providerId": "plan", "config": {"access": {"type": "zhipu-account"},
                                              "builtinModelIds": ["GLM-A", "GLM-B"]}},
            {"providerId": "apikey", "config": {"access": {"type": "api-key"},
                                                "builtinModelIds": ["M2"]}},
        ]},
        "modelConfigRules": {"modelRules": [
            {"modelMatch": "GLM-.*", "config": {"optionSpecs": {"reasoningLevel": {"values": ["high", "low"]}}}},
        ]},
    }})
    monkeypatch.setenv("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", str(table))
    monkeypatch.setattr(sources, "subprocess", fake_subprocess_module(
        lambda *args, **kwargs: fake_agy_result(stdout="Fetching models…\n\t-\ngem-a\tdesc\n")))
    # APPDATA isolation covers the OpenCode desktop state.
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    write(codex_home / "models_cache.json", {"models": [
        {"slug": "gpt-a", "visibility": "list", "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "medium"}]},
        {"slug": "gpt-hidden", "visibility": "hide"},
    ]})
    write(xdg / "opencode" / "model.json", {"favorite": [
        {"providerID": "p", "modelID": "m1"},
        {"providerID": "p", "modelID": "sub/m2"},
        {"providerID": "q", "modelID": "m3"},
        {"providerID": "q", "modelID": "m3"},
    ], "recent": [{"providerID": "p", "modelID": "m1"}]})
    return codex_home, xdg


def test_sources_read_only_files_and_deduplicate(state):
    result = sources.read_sources()
    assert result["codex"].ok
    assert [item["address"] for item in result["codex"].candidates] == ["codex/openai/gpt-a"]
    assert result["codex"].candidates[0]["efforts"] == ["low", "medium"]
    assert result["codex"].candidates[0]["default_effort"] == "medium"
    assert result["opencode"].ok
    assert [item["address"] for item in result["opencode"].candidates] == [
        "opencode/p/m1", "opencode/p/sub/m2", "opencode/q/m3"]
    assert result["zcode"].ok
    assert [item["address"] for item in result["zcode"].candidates] == [
        "zcode/plan/GLM-A", "zcode/plan/GLM-B"]
    assert result["zcode"].candidates[0]["efforts"] == ["high", "low"]
    assert result["zcode"].candidates[0]["default_effort"] is None
    assert result["agy"].ok
    assert [item["address"] for item in result["agy"].candidates] == ["agy/gemini/gem-a"]


def test_report_new_stale_and_boundary(state):
    source_map = sources.read_sources()
    data = registry._empty()
    registry.add(data, "opencode/p/m1")
    registry.add(data, "opencode/q/ghost")
    registry.add(data, "other/prov/model")
    report = sync.build(data, source_map)
    assert [item["address"] for item in report["new"]] == [
        "agy/gemini/gem-a", "codex/openai/gpt-a", "opencode/p/sub/m2", "opencode/q/m3",
        "zcode/plan/GLM-A", "zcode/plan/GLM-B"]
    assert [item["index"] for item in report["new"]] == [1, 2, 3, 4, 5, 6]
    assert [item["address"] for item in report["stale"]] == ["opencode/q/ghost"]
    assert "other/prov/model" not in [item["address"] for item in report["stale"]]
    assert "other/prov/model" not in [item["address"] for item in report["new"]]


def test_zcode_table_missing_or_unreadable_disables_source(tmp_path, monkeypatch):
    monkeypatch.setenv("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", str(tmp_path / "absent.json"))
    result = sources.zcode_source()
    assert result.ok is False and result.candidates == [] and result.warning
    table = tmp_path / "broken.json"
    table.write_text("{", encoding="utf-8")
    monkeypatch.setenv("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", str(table))
    result = sources.zcode_source()
    assert result.ok is False and result.candidates == [] and result.warning


def test_zcode_fallback_install_layout(tmp_path, monkeypatch):
    # The env override is authoritative; without it the install layout is used.
    monkeypatch.delenv("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(os, "name", "nt")
    table = (tmp_path / "Programs" / "ZCode" / "resources"
             / "config" / "provider" / "zcode-builtin.json")
    write(table, {})
    assert sources.builtin_table_path() == table
    assert sources.zcode_source().ok is True


@pytest.mark.parametrize("table", [
    {"config": {"providerConfigRules": {"providerRules": [
        {"config": {"access": {"type": "zhipu-account"}, "builtinModelIds": ["m"]}}]}}},
    {"config": {"providerConfigRules": {"providerRules": [
        {"providerId": "p/", "config": {"access": {"type": "zhipu-account"},
                                        "builtinModelIds": ["m"]}}]}}},
    {"config": {"providerConfigRules": {"providerRules": [
        {"providerId": "p", "config": {"access": {"type": "zhipu-account"}}}]}}},
    {"config": {"providerConfigRules": {"providerRules": [
        {"providerId": "p", "config": {"access": {"type": "zhipu-account"},
                                       "builtinModelIds": ["m//x"]}}]}}},
])
def test_malformed_zcode_table_disables_source(state, monkeypatch, tmp_path, table):
    path = tmp_path / "table.json"
    write(path, table)
    monkeypatch.setenv("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", str(path))
    result = sources.zcode_source()
    assert result.ok is False and result.candidates == [] and result.warning


def test_agy_source_parses_dedups_and_fails(state, monkeypatch):
    monkeypatch.setattr(sources, "subprocess", fake_subprocess_module(
        lambda *args, **kwargs: fake_agy_result(
            stdout="Fetching models…\n\nm1\tdesc\nm1\tdesc again\nm2 with space\tdesc")))
    result = sources.agy_source()
    assert result.ok
    assert [item["address"] for item in result.candidates] == [
        "agy/gemini/m1", "agy/gemini/m2 with space"]
    monkeypatch.setattr(sources, "subprocess", fake_subprocess_module(
        lambda *args, **kwargs: fake_agy_result(returncode=3, stdout="m1\tdesc")))
    result = sources.agy_source()
    assert result.ok is False and result.candidates == [] and result.warning

    def boom(*args, **kwargs):
        raise FileNotFoundError("agy")
    monkeypatch.setattr(sources, "subprocess", fake_subprocess_module(boom))
    result = sources.agy_source()
    assert result.ok is False and result.candidates == [] and result.warning


def test_zcode_catalog_check(state, monkeypatch):
    assert sources.zcode_catalog_check("plan", "GLM-A", ["high"]) is None
    with pytest.raises(registry.ConfigError):
        sources.zcode_catalog_check("other", "GLM-A", [])
    with pytest.raises(registry.ConfigError):
        sources.zcode_catalog_check("plan", "nope", [])
    with pytest.raises(registry.ConfigError):
        sources.zcode_catalog_check("plan", "GLM-A", ["max"])
    monkeypatch.setenv("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", str(state[0] / "absent.json"))
    assert "without catalog verification" in sources.zcode_catalog_check("plan", "GLM-A", [])


def test_hidden_filters_new_and_zero_candidates_suppresses_stale(state):
    codex_home, xdg = state
    data = registry._empty()
    data["hidden"] = ["codex/openai/gpt-a"]
    write(xdg / "opencode" / "model.json", {"favorite": []})
    report = sync.build(data, sources.read_sources())
    assert "codex/openai/gpt-a" not in [item["address"] for item in report["new"]]
    opencode = report["sources"]["opencode"]
    assert opencode["ok"] is True and opencode["warning"]
    assert [item["address"] for item in report["stale"]] == []


def test_override_registered_model_is_never_stale(state):
    data = registry._empty()
    # GLM-C is not in the fixture builtin table: a plain add would be stale.
    registry.add(data, "zcode/plan/GLM-C", override=True)
    report = sync.build(data, sources.read_sources())
    assert [item["address"] for item in report["stale"]] == []
    assert "zcode/plan/GLM-C" not in [item["address"] for item in report["new"]]
    provider = report["sources"]["zcode"]["providers"]["plan"]
    assert provider["override"] == 1 and provider["registry"] == 1
    assert "· 1 override" in sync.render(report)


def test_override_model_turning_native_is_deduplicated(state):
    data = registry._empty()
    # GLM-A is native in the fixture table: the override mark is inert, the
    # registered model is neither NEW nor stale and never counted twice.
    registry.add(data, "zcode/plan/GLM-A", override=True)
    report = sync.build(data, sources.read_sources())
    assert "zcode/plan/GLM-A" not in [item["address"] for item in report["new"]]
    assert [item["address"] for item in report["stale"]] == []
    provider = report["sources"]["zcode"]["providers"]["plan"]
    assert provider["registry"] == 1 and provider["override"] == 1


def test_source_failure_is_isolated_per_harness(state):
    codex_home, xdg = state
    (codex_home / "models_cache.json").write_text("{", encoding="utf-8")
    data = registry._empty()
    report = sync.build(data, sources.read_sources())
    assert report["sources"]["codex"]["ok"] is False
    assert report["sources"]["opencode"]["ok"] is True
    assert all(not item["address"].startswith("codex/") for item in report["new"])


@pytest.mark.parametrize("payload", [
    {"favorite": None},
    {"favorite": "opencode/p/m"},
    {"favorite": [{"providerID": "p"}]},
    {"favorite": [{"providerID": "", "modelID": "m"}]},
    {"favorite": [{"providerID": "p", "modelID": ""}]},
    {"favorite": [{"providerID": "p", "modelID": "m//x"}]},
])
def test_malformed_favorite_disables_opencode_source(state, payload):
    _, xdg = state
    write(xdg / "opencode" / "model.json", payload)
    result = sources.opencode_source()
    assert result.ok is False and result.candidates == [] and result.warning


def test_malformed_codex_entry_disables_source(state):
    codex_home, _ = state
    write(codex_home / "models_cache.json", {"models": [
        {"slug": "gpt-a", "visibility": "list"},
        {"visibility": "list"},
    ]})
    result = sources.codex_source()
    assert result.ok is False and result.candidates == []


def test_codex_effort_defaults_and_missing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    write(tmp_path / "models_cache.json", {"models": [
        {"slug": "a", "visibility": "list", "default_reasoning_level": "max",
         "supported_reasoning_levels": [{"effort": "low"}]},
        {"slug": "b", "visibility": "list"},
    ]})
    result = sources.codex_source()
    assert result.ok
    by_address = {item["address"]: item for item in result.candidates}
    assert by_address["codex/openai/a"]["default_effort"] is None
    assert by_address["codex/openai/a"]["efforts"] == ["low"]
    assert by_address["codex/openai/b"]["efforts"] == []
    assert by_address["codex/openai/b"]["default_effort"] is None


def test_opencode_falls_back_to_local_state(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "no-data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "no-appdata"))
    write(tmp_path / ".local" / "state" / "opencode" / "model.json",
          {"favorite": [{"providerID": "p", "modelID": "m"}]})
    result = sources.opencode_source()
    assert result.ok
    assert [item["address"] for item in result.candidates] == ["opencode/p/m"]


def opencode_env(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    return config_home, data_home


def write_opencode_config(config_home, payload, name="opencode.json"):
    path = config_home / "opencode" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        write(path, payload)


def test_strip_jsonc_keeps_strings_and_drops_comments_and_trailing_commas():
    text = ('{\n'
            '  // user providers\n'
            '  "url": "http://x//y", /* block */\n'
            '  "list": [1, 2,],\n'
            '  "esc": "quote\\"//still a string",\n'
            '}')
    assert json.loads(sources._strip_jsonc(text)) == {
        "url": "http://x//y", "list": [1, 2], "esc": 'quote"//still a string'}


def test_opencode_unavailable_providers_rules(tmp_path, monkeypatch):
    config_home, data_home = opencode_env(tmp_path, monkeypatch)
    write_opencode_config(config_home, {
        "provider": {"custom": {"name": "Custom"}},
        "disabled_providers": ["gone"],
    })
    write(data_home / "opencode" / "auth.json", {"keyed": {"type": "api", "key": "k"}})
    reasons = sources.opencode_unavailable_providers(
        ["gone", "custom", "keyed", "builtin", "opencode"])
    assert reasons == {"gone": "disabled in OpenCode config",
                       "builtin": "no key in OpenCode auth"}


def test_opencode_enabled_providers_allowlist(tmp_path, monkeypatch):
    config_home, _ = opencode_env(tmp_path, monkeypatch)
    write_opencode_config(config_home, {"enabled_providers": ["custom"],
                                        "provider": {"custom": {}, "other": {}}})
    reasons = sources.opencode_unavailable_providers(["custom", "other", "opencode"])
    assert reasons == {"other": "not in enabled_providers",
                       "opencode": "not in enabled_providers"}


def test_opencode_config_files_merge_and_jsonc_is_parsed(tmp_path, monkeypatch):
    config_home, _ = opencode_env(tmp_path, monkeypatch)
    write_opencode_config(config_home, {"disabled_providers": ["a"]})
    write_opencode_config(config_home,
                          '{\n  // jsonc half\n  "disabled_providers": ["b",],\n}',
                          name="opencode.jsonc")
    reasons = sources.opencode_unavailable_providers(["a", "b"])
    assert set(reasons) == {"a", "b"}


def test_opencode_missing_or_broken_config_disables_filtering(tmp_path, monkeypatch):
    config_home, _ = opencode_env(tmp_path, monkeypatch)
    assert sources.opencode_unavailable_providers(["x"]) is None
    write_opencode_config(config_home, "{ not json", name="opencode.jsonc")
    assert sources.opencode_unavailable_providers(["x"]) is None


def test_opencode_unavailable_providers_removed_from_new_and_stale(state, tmp_path,
                                                                   monkeypatch):
    _, xdg = state
    config_home, data_home = opencode_env(tmp_path, monkeypatch)
    write_opencode_config(config_home, {"disabled_providers": ["q"]})
    write(data_home / "opencode" / "auth.json", {"p": {"type": "api", "key": "k"}})
    write(xdg / "opencode" / "model.json", {"favorite": [
        {"providerID": "p", "modelID": "m1"},
        {"providerID": "q", "modelID": "m2"},
    ]})
    data = registry._empty()
    registry.add(data, "opencode/q/m2")
    report = sync.build(data, sources.read_sources())
    addresses = [item["address"] for item in report["new"]]
    assert "opencode/p/m1" in addresses and "opencode/q/m2" not in addresses
    # Registered but unavailable: staleness stays suppressed, the provider is marked.
    assert report["stale"] == []
    provider = report["sources"]["opencode"]["providers"]["q"]
    assert provider["unavailable"] == "disabled in OpenCode config"
    assert provider["new"] == 0 and provider["stale"] == 0
    assert "unavailable: disabled in OpenCode config" in sync.render(report)
    assert report["sources"]["opencode"]["providers"]["p"]["unavailable"] is None


def test_opencode_builtin_without_key_hidden_zen_and_defined_survive(state, tmp_path,
                                                                     monkeypatch):
    _, xdg = state
    config_home, data_home = opencode_env(tmp_path, monkeypatch)
    write_opencode_config(config_home, {"provider": {"custom-noauth": {}}})
    write(data_home / "opencode" / "auth.json", {"keyed": {"type": "api", "key": "k"}})
    write(xdg / "opencode" / "model.json", {"favorite": [
        {"providerID": "opencode-go", "modelID": "m1"},
        {"providerID": "opencode", "modelID": "m2"},
        {"providerID": "keyed", "modelID": "m3"},
        {"providerID": "custom-noauth", "modelID": "m4"},
    ]})
    report = sync.build(registry._empty(), sources.read_sources())
    addresses = [item["address"] for item in report["new"]]
    assert "opencode/opencode-go/m1" not in addresses
    assert "opencode/opencode/m2" in addresses
    assert "opencode/keyed/m3" in addresses
    assert "opencode/custom-noauth/m4" in addresses
    providers = report["sources"]["opencode"]["providers"]
    assert providers["opencode-go"]["unavailable"] == "no key in OpenCode auth"
    assert providers["opencode"]["unavailable"] is None


# ------------------------------------------------------- OpenCode desktop state


def write_desktop_state(appdata, entries):
    path = Path(appdata) / "ai.opencode.desktop" / "opencode.global.dat"
    write(path, {"model": json.dumps({"user": entries})})


def test_opencode_desktop_state_is_preferred(state, monkeypatch):
    write_desktop_state(os.environ["APPDATA"], [
        {"providerID": "p", "modelID": "m1", "visibility": "show"},
        {"providerID": "p", "modelID": "m2", "visibility": "hide"},
        {"providerID": "opencode-go", "modelID": "m3", "visibility": "show"},
    ])
    write_opencode_config(Path(os.environ["XDG_CONFIG_HOME"]), {})
    write(Path(os.environ["XDG_DATA_HOME"]) / "opencode" / "auth.json",
          {"p": {"type": "api", "key": "k"}})
    result = sources.opencode_source()
    assert result.ok and result.scope == "desktop-visible"
    # Only shown models become candidates; hidden and stale-favorite ones drop.
    assert [item["address"] for item in result.candidates] == [
        "opencode/opencode-go/m3", "opencode/p/m1"]
    assert all(item["favorite"] is False for item in result.candidates)
    # The #98 unavailable rules still apply on top of the desktop catalog.
    assert result.unavailable == {"opencode-go": "no key in OpenCode auth"}


def test_opencode_desktop_report_scope_and_stale_reason(state, monkeypatch):
    write_desktop_state(os.environ["APPDATA"], [
        {"providerID": "p", "modelID": "m1", "visibility": "show"},
        {"providerID": "p", "modelID": "m2", "visibility": "hide"},
    ])
    write_opencode_config(Path(os.environ["XDG_CONFIG_HOME"]), {})
    write(Path(os.environ["XDG_DATA_HOME"]) / "opencode" / "auth.json",
          {"p": {"type": "api", "key": "k"}})
    data = registry._empty()
    registry.add(data, "opencode/p/m2")
    report = sync.build(data, sources.read_sources())
    opencode = report["sources"]["opencode"]
    assert opencode["candidate_scope"] == "desktop-visible" == opencode["stale_scope"]
    addresses = [item["address"] for item in report["new"]
                 if item["address"].startswith("opencode/")]
    assert addresses == ["opencode/p/m1"]
    assert [item["reason"] for item in report["stale"]] == [
        "not visible in the OpenCode desktop"]
    lines = sync.render(report).splitlines()
    assert "OpenCode — источник: desktop-visible" in lines
    assert any(line.startswith("  p ") and " 1 visible " in line for line in lines)


def test_opencode_desktop_broken_or_keyless_falls_back_to_favorites(state, monkeypatch):
    appdata = Path(os.environ["APPDATA"])
    path = appdata / "ai.opencode.desktop" / "opencode.global.dat"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    result = sources.opencode_source()
    assert result.scope is None
    assert [item["address"] for item in result.candidates] == [
        "opencode/p/m1", "opencode/p/sub/m2", "opencode/q/m3"]
    write(path, json.dumps({"other": 1}))
    result = sources.opencode_source()
    assert result.scope is None
    assert len(result.candidates) == 3


def test_opencode_desktop_shown_model_outside_config_catalog_stays(state):
    write_desktop_state(os.environ["APPDATA"], [
        {"providerID": "custom", "modelID": "in-config", "visibility": "show"},
        {"providerID": "custom", "modelID": "beyond-config", "visibility": "show"},
        {"providerID": "custom", "modelID": "hidden", "visibility": "hide"},
    ])
    write_opencode_config(Path(os.environ["XDG_CONFIG_HOME"]),
                          {"provider": {"custom": {"models": {"in-config": {"name": "In"}}}}})
    write(Path(os.environ["XDG_DATA_HOME"]) / "opencode" / "auth.json",
          {"custom": {"type": "api", "key": "k"}})
    result = sources.opencode_source()
    # The desktop shows each provider's own list, wider than the TUI config
    # models block: shown models stay even outside that block; hidden drop.
    assert [item["address"] for item in result.candidates] == [
        "opencode/custom/beyond-config", "opencode/custom/in-config"]


def test_opencode_favorite_drops_models_missing_from_config_catalog(state):
    _, xdg = state
    write(xdg / "opencode" / "model.json", {"favorite": [
        {"providerID": "p", "modelID": "kept"},
        {"providerID": "p", "modelID": "gone"},
        {"providerID": "q", "modelID": "m"},
    ]})
    write_opencode_config(Path(os.environ["XDG_CONFIG_HOME"]),
                          {"provider": {"p": {"models": {"kept": {}}}}})
    result = sources.opencode_source()
    assert [item["address"] for item in result.candidates] == [
        "opencode/p/kept", "opencode/q/m"]


def test_opencode_desktop_malformed_entries_are_skipped(state, monkeypatch):
    write_desktop_state(os.environ["APPDATA"], [
        {"providerID": "p", "modelID": "m1", "visibility": "show"},
        "nonsense",
        {"providerID": "x//bad", "modelID": "m", "visibility": "show"},
        {"providerID": "p", "modelID": "m//x", "visibility": "show"},
    ])
    result = sources.opencode_source()
    assert result.ok and result.scope == "desktop-visible"
    assert [item["address"] for item in result.candidates] == ["opencode/p/m1"]


# ------------------------------------------------------- silent hidden rows


def test_fully_hidden_provider_row_is_silent(state):
    data = registry._empty()
    data["hidden"] = ["zcode/plan"]
    report = sync.build(data, sources.read_sources())
    providers = report["sources"]["zcode"]["providers"]
    assert providers["plan"]["hidden_all"] is True
    assert providers["plan"]["new"] == 0 and providers["plan"]["stale"] == 0
    lines = sync.render(report).splitlines()
    assert not any(line.startswith("  plan") for line in lines)
    # Untouched provider rows of other harnesses stay in the table.
    assert any(line.startswith("  openai") for line in lines)


def test_partially_hidden_provider_row_stays(state):
    data = registry._empty()
    data["hidden"] = ["zcode/plan/GLM-A"]
    report = sync.build(data, sources.read_sources())
    providers = report["sources"]["zcode"]["providers"]
    assert providers["plan"]["hidden_all"] is False
    assert providers["plan"]["new"] == 1
    assert "zcode/plan/GLM-B" in [item["address"] for item in report["new"]]
    assert any(line.startswith("  plan") for line in sync.render(report).splitlines())


def test_fully_hidden_provider_with_registry_entry_stays(state):
    data = registry._empty()
    registry.add(data, "zcode/plan/GLM-A")
    data["hidden"] = ["zcode/plan"]
    report = sync.build(data, sources.read_sources())
    providers = report["sources"]["zcode"]["providers"]
    assert providers["plan"]["hidden_all"] is True
    assert providers["plan"]["registry"] == 1
    assert any(line.startswith("  plan") for line in sync.render(report).splitlines())


def test_pick_entries_indices_addresses_and_errors(state):
    data = registry._empty()
    entries = sync.build(data, sources.read_sources())["new"]
    assert len(entries) >= 2
    assert [item["address"] for item in sync.pick_entries(entries, "1", "NEW")] == [entries[0]["address"]]
    assert [item["address"] for item in sync.pick_entries(entries, "2,2", "NEW")] == [entries[1]["address"]]
    assert sync.pick_entries(entries, entries[1]["address"], "NEW")[0]["address"] == entries[1]["address"]
    for spec in ("0", "99", "all", "all,1", "1," + entries[0]["address"],
                 "opencode/zzz", "/opencode/zzz/missing"):
        with pytest.raises(registry.ConfigError):
            sync.pick_entries(entries, spec, "NEW")


def test_public_payload_schema(state):
    data = registry._empty()
    data["hidden"] = ["opencode/zzz", "codex/openai/aaa"]
    report = sync.build(data, sources.read_sources())
    payload = sync.public(report)
    assert set(payload) == {"sources", "new", "stale", "hidden"}
    assert payload["hidden"] == ["opencode/zzz", "codex/openai/aaa"]
    assert "codex/openai/aaa" in sync.render(report)
    rendered = sync.render(report).splitlines()
    header = next(line for line in rendered if line.startswith("HIDDEN"))
    assert header.index("codex/openai/aaa") < header.index("opencode/zzz")
    assert set(payload["new"][0]) == {"index", "address", "favorite"}
    source = payload["sources"]["opencode"]
    assert set(source) == {"harness", "ok", "warning", "candidate_scope", "stale_scope", "providers"}
    assert source["candidate_scope"] == "favorite"
    assert payload["sources"]["codex"]["candidate_scope"] == "catalog"
    assert payload["sources"]["zcode"]["candidate_scope"] == "builtin-table"
    assert payload["sources"]["agy"]["candidate_scope"] == "model-list"
