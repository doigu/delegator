import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "registry.py"
SPEC = importlib.util.spec_from_file_location("delegator_registry", SCRIPT)
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)


def sample():
    data = registry._empty()
    registry.add(data, "opencode/openrouter/minimax/minimax-m3.1-flash:free",
                 efforts=["low", "medium"], effort="medium")
    registry.add(data, "codex/openai/gpt-6.1-sol", efforts=["high"])
    return data


class RegistryTests(unittest.TestCase):
    def test_address_preserves_model_slashes_and_suffix(self):
        address = "opencode/openrouter/minimax/minimax-m3.1-flash:free"
        self.assertEqual(registry.parse_address(address),
                         ("opencode", "openrouter", "minimax/minimax-m3.1-flash:free"))
        self.assertEqual(registry.parse_address("codex/openai/model:free"),
                         ("codex", "openai", "model:free"))
        for bad in ("", "/", "a//b", "a/b/", "a/b/model//free"):
            with self.subTest(bad=bad), self.assertRaises(registry.ConfigError):
                registry.parse_address(bad)

    def test_select_exact_suffix_root_and_ambiguity(self):
        data = sample()
        self.assertEqual([x[0] for x in registry.select(data, "openrouter")], ["opencode/openrouter"])
        self.assertEqual([x[0] for x in registry.select(data, "/opencode")], ["opencode"])
        data["harnesses"]["opencode"]["providers"]["opencode"] = {
            "enabled": True, "quota": {"blocked": False, "reset_at": None, "reason": None}, "models": {}}
        with self.assertRaisesRegex(registry.ConfigError, "opencode, opencode/opencode"):
            registry.select(data, "opencode")
        self.assertEqual([x[0] for x in registry.select(data, "/opencode")], ["opencode"])
        with self.assertRaises(registry.ConfigError):
            registry.select(data, "missing")

    def test_add_sets_new_ancestors_and_preserves_existing_nodes(self):
        data = registry._empty()
        address = "opencode/openrouter/minimax/minimax-m3.1-flash:free"
        self.assertEqual(registry.add(data, address), [address])
        provider = data["harnesses"]["opencode"]["providers"]["openrouter"]
        self.assertTrue(provider["enabled"])
        registry.toggle(data, "openrouter", False)
        registry.add(data, address, efforts=["high"], effort="high")
        self.assertFalse(provider["enabled"])
        self.assertEqual(provider["models"]["minimax/minimax-m3.1-flash:free"]["efforts"], ["high"])
        with self.assertRaises(registry.ConfigError):
            registry.add(data, "other/provider/model", effort="invalid value")

    def test_override_flag_stores_roundtrips_and_stays_model_only(self):
        data = registry._empty()
        address = "zcode/account:zai-start-plan/GLM-5.3"
        registry.add(data, address, efforts=["low"], effort="low", override=True)
        model = data["harnesses"]["zcode"]["providers"]["account:zai-start-plan"]["models"]["GLM-5.3"]
        self.assertTrue(model["override"])
        # Native models carry no override key at all.
        registry.add(data, "zcode/account:zai-start-plan/GLM-5.3-Flash")
        flash = data["harnesses"]["zcode"]["providers"]["account:zai-start-plan"]["models"]["GLM-5.3-Flash"]
        self.assertNotIn("override", flash)
        # Re-adding with override marks an existing node; idempotent.
        registry.add(data, address, override=True)
        self.assertTrue(model["override"])
        for bad in ("zcode/account:zai-start-plan", "zcode"):
            with self.subTest(address=bad):
                with self.assertRaises(registry.ConfigError):
                    registry.add(data, bad, override=True)
        model["override"] = "yes"
        with self.assertRaises(registry.ConfigError):
            registry._validate(data)
        model["override"] = True
        # The flag dies with its node.
        registry.delete(data, address)
        self.assertNotIn("GLM-5.3",
                         data["harnesses"]["zcode"]["providers"]["account:zai-start-plan"]["models"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            registry.add(data, address, efforts=["low"], effort="low", override=True)
            registry.save(path, data)
            self.assertEqual(registry.load(path), data)

    def test_available_reports_override(self):
        data = registry._empty()
        registry.add(data, "zcode/account:zai-start-plan/GLM-5.3", override=True)
        registry.add(data, "zcode/account:zai-start-plan/GLM-5.3-Flash")
        rows = {row["address"]: row for row in registry.available(data)}
        self.assertTrue(rows["zcode/account:zai-start-plan/GLM-5.3"]["override"])
        self.assertFalse(rows["zcode/account:zai-start-plan/GLM-5.3-Flash"]["override"])

    def test_available_inherits_flags_and_quota_reset_without_mutating_flags(self):
        data = sample()
        address = "opencode/openrouter/minimax/minimax-m3.1-flash:free"
        provider = data["harnesses"]["opencode"]["providers"]["openrouter"]
        model = provider["models"]["minimax/minimax-m3.1-flash:free"]
        registry.toggle(data, "/opencode", False)
        status = registry.available(data)[0]
        self.assertTrue(status["enabled"])
        self.assertFalse(status["available"])
        self.assertIn("harness", status["reason"])
        registry.toggle(data, "/opencode", True)
        provider["quota"].update(blocked=True, reset_at="2026-10-22T10:00:00Z", reason="rate limit")
        model["enabled"] = False
        status = next(item for item in registry.available(data, datetime(2026, 10, 22, 10, tzinfo=timezone.utc))
                      if item["address"] == address)
        self.assertFalse(status["enabled"])
        self.assertFalse(status["available"])
        self.assertIn("disabled", status["reason"])
        self.assertTrue(provider["quota"]["blocked"])
        registry.toggle(data, "openrouter", False)
        registry.toggle(data, "openrouter", True)
        self.assertFalse(provider["quota"]["blocked"])
        self.assertFalse(model["enabled"])

    def test_on_clears_scoped_quota_without_changing_manual_flags(self):
        data = sample()
        provider = data["harnesses"]["opencode"]["providers"]["openrouter"]
        model = provider["models"]["minimax/minimax-m3.1-flash:free"]
        model["enabled"] = False
        provider["quota"].update(blocked=True, reset_at="2026-10-22T10:00:00Z", reason="quota")
        registry.toggle(data, "/opencode", True)
        self.assertFalse(model["enabled"])
        self.assertEqual(provider["quota"], {"blocked": False, "reset_at": None, "reason": None})
        provider["quota"].update(blocked=True, reset_at="2026-10-22T10:00:00Z", reason="quota")
        registry.toggle(data, "opencode/openrouter/minimax/minimax-m3.1-flash:free", True)
        self.assertTrue(model["enabled"])
        self.assertFalse(provider["quota"]["blocked"])
        provider["quota"].update(blocked=True, reason="quota")
        registry.toggle(data, "/opencode", False)
        self.assertTrue(provider["quota"]["blocked"])

    def test_delete_requires_confirmation_and_dry_run_is_read_only(self):
        data = sample()
        before = json.dumps(data, sort_keys=True)
        with self.assertRaises(registry.ConfigError):
            registry.delete(data, "opencode")
        registry.add(data, "codex/openrouter/minimax/minimax-m3.1-flash:free")
        before = json.dumps(data, sort_keys=True)
        with self.assertRaises(registry.ConfigError):
            registry.delete(data, "minimax/minimax-m3.1-flash:free")
        plan = registry.delete(data, "opencode", recursive=True, dry_run=True)
        self.assertEqual(plan, ["opencode", "opencode/openrouter",
                                "opencode/openrouter/minimax/minimax-m3.1-flash:free"])
        self.assertEqual(json.dumps(data, sort_keys=True), before)
        with self.assertRaises(registry.ConfigError):
            registry.delete(data, "opencode", recursive=True)
        registry.delete(data, "opencode", recursive=True, yes=True)
        self.assertNotIn("opencode", data["harnesses"])

    def test_strict_schema_and_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            self.assertEqual(registry.load(path)["execution"],
                             {"status_interval_seconds": 1800, "max_runtime_seconds": None,
                              "capacity_retries": 3, "capacity_retry_seconds": 30,
                              "route_retries": 1})
            data = sample()
            registry.save(path, data)
            self.assertEqual(registry.load(path), data)
            self.assertEqual(list(Path(folder).iterdir()), [path])
            for mutate in (
                lambda d: d.update(extra=True),
                lambda d: d["harnesses"]["opencode"].update(enabled="yes"),
                lambda d: d["harnesses"]["opencode"]["providers"]["openrouter"]["models"][
                    "minimax/minimax-m3.1-flash:free"].update(default_effort="bogus"),
                lambda d: d["harnesses"]["opencode"]["providers"]["openrouter"]["quota"].update(
                    reset_at="tomorrow"),
            ):
                invalid = sample()
                mutate(invalid)
                with self.subTest(invalid=invalid), self.assertRaises(registry.ConfigError):
                    registry.save(path, invalid)
            path.write_text('{', encoding="utf-8")
            with self.assertRaises(registry.ConfigError):
                registry.load(path)

    def test_block_quota_preserves_manual_disable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            data = sample()
            registry.toggle(data, "openrouter", False)
            registry.save(path, data)
            registry.block_quota(path, "opencode", "openrouter", "quota exhausted",
                                 "2026-10-22T10:00:00+00:00")
            result = registry.load(path)
            provider = result["harnesses"]["opencode"]["providers"]["openrouter"]
            self.assertFalse(provider["enabled"])
            self.assertTrue(provider["quota"]["blocked"])
            self.assertEqual(provider["quota"]["reason"], "quota exhausted")


    def test_hidden_roundtrip_and_omitted_when_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            data = registry.load(path)
            self.assertEqual(registry.hidden_list(data), [])
            self.assertEqual(registry.hidden_add(data, "/codex/openai/gpt-5.6-terra"),
                             ["codex/openai/gpt-5.6-terra"])
            registry.save(path, data)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["hidden"], ["codex/openai/gpt-5.6-terra"])
            self.assertEqual(registry.load(path)["hidden"], ["codex/openai/gpt-5.6-terra"])
            self.assertEqual(registry.hidden_remove(data, "codex/openai/gpt-5.6-terra"),
                             ["codex/openai/gpt-5.6-terra"])
            registry.save(path, data)
            self.assertNotIn("hidden", json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(registry.load(path)["hidden"], [])

    def test_hidden_rejects_malformed_stored_entries(self):
        base = registry._empty()
        for hidden in ("opencode/openrouter", ["opencode"], ["/opencode/openrouter"],
                       ["opencode/openrouter/"], ["opencode/openrouter", "opencode/openrouter"],
                       [1], [""]):
            config = json.loads(json.dumps(base))
            config["hidden"] = hidden
            with self.subTest(hidden=hidden), self.assertRaises(registry.ConfigError):
                registry.save(tempfile.gettempdir() + "/never-written.json", config)
        config = json.loads(json.dumps(base))
        config.update(hidden=["opencode/openrouter"], extra=1)
        with self.assertRaises(registry.ConfigError):
            registry.save(tempfile.gettempdir() + "/never-written.json", config)

    def test_hidden_overlap_boundary_and_idempotency(self):
        data = sample()
        with self.assertRaisesRegex(registry.ConfigError, "covers the registry entry"):
            registry.hidden_add(data, "opencode/openrouter")
        with self.assertRaises(registry.ConfigError):
            registry.hidden_add(data, "codex/openai/gpt-6.1-sol")
        self.assertEqual(registry.hidden_add(data, "opencode/openrouter-extra/model"),
                         ["opencode/openrouter-extra/model"])
        self.assertEqual(registry.hidden_add(data, "opencode/openrouter-extra/model"), [])
        self.assertIsNone(registry.hidden_covering(data, "opencode/openrouter-extra-other/model"))
        self.assertEqual(registry.hidden_covering(data, "opencode/openrouter-extra/model"),
                         "opencode/openrouter-extra/model")
        self.assertEqual(registry.hidden_remove(data, "opencode/openrouter-extra/model"),
                         ["opencode/openrouter-extra/model"])
        self.assertEqual(registry.hidden_remove(data, "opencode/openrouter-extra/model"), [])

    def test_add_interacts_with_hidden(self):
        data = registry._empty()
        registry.hidden_add(data, "codex/openai/gpt-5.6-terra")
        registry.add(data, "codex/openai/gpt-5.6-terra")
        self.assertEqual(data["hidden"], [])
        data = registry._empty()
        registry.hidden_add(data, "opencode/openrouter")
        with self.assertRaises(registry.ConfigError):
            registry.add(data, "opencode/openrouter/model")
        with self.assertRaises(registry.ConfigError):
            registry.add(data, "opencode/openrouter")
        data = registry._empty()
        registry.hidden_add(data, "opencode/openrouter/model")
        self.assertEqual(registry.add(data, "opencode/openrouter"), ["opencode/openrouter"])
        self.assertEqual(data["hidden"], ["opencode/openrouter/model"])

    def test_delete_and_toggle_keep_hidden_list(self):
        data = sample()
        registry.hidden_add(data, "codex/openai/gpt-5.6-terra")
        registry.delete(data, "openrouter", recursive=True, yes=True)
        registry.toggle(data, "codex", False)
        registry.toggle(data, "codex", True)
        self.assertEqual(data["hidden"], ["codex/openai/gpt-5.6-terra"])

    def test_manual_intersection_fails_closed_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            data = sample()
            registry.save(path, data)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["hidden"] = ["codex/openai/gpt-6.1-sol"]
            path.write_text(json.dumps(raw), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(registry.ConfigError, "covers the registry entry"):
                registry.load(path)
            self.assertEqual(path.read_bytes(), before)

    def test_alias_crud_requires_registered_full_routes(self):
        data = sample()
        address = "opencode/openrouter/minimax/minimax-m3.1-flash:free"
        registry.alias_set(data, "mini", [address, "codex/openai/gpt-6.1-sol"])
        self.assertEqual(registry.alias_routes(data, "mini"),
                         [address, "codex/openai/gpt-6.1-sol"])
        self.assertEqual(registry.alias_names(data), ["mini"])
        for name, routes in (("with/slash", [address]),
                             ("mini", [address, address]),
                             ("mini", ["opencode/openrouter/unregistered"]),
                             ("mini", ["codex/openai"]),
                             ("mini", []),
                             ("mini", ["opencode/openrouter/minimax/minimax-m3.1-flash:free", 5])):
            with self.subTest(name=name, routes=routes), self.assertRaises(registry.ConfigError):
                registry.alias_set(data, name, routes)
        self.assertEqual(registry.alias_routes(data, "mini"),
                         [address, "codex/openai/gpt-6.1-sol"])
        registry.alias_set(data, "mini", [address])
        self.assertEqual(registry.alias_routes(data, "mini"), [address])
        self.assertEqual(registry.alias_references(data, "opencode/openrouter"), ["mini"])
        self.assertEqual(registry.alias_references(data, "opencode/openrouter/minimax/minimax-m3.1-flash:free"),
                         ["mini"])
        self.assertEqual(registry.alias_references(data, "codex/openai/gpt-6.1-sol"), [])
        self.assertTrue(registry.alias_remove(data, "mini"))
        self.assertFalse(registry.alias_remove(data, "mini"))
        self.assertEqual(registry.alias_references(data, "opencode/openrouter"), [])

    def test_alias_roundtrip_omitted_when_empty_and_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            data = sample()
            registry.alias_set(data, "mini", ["codex/openai/gpt-6.1-sol"])
            registry.save(path, data)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["aliases"], {"mini": {"routes": ["codex/openai/gpt-6.1-sol"]}})
            self.assertEqual(registry.load(path)["aliases"],
                             {"mini": {"routes": ["codex/openai/gpt-6.1-sol"]}})
            registry.alias_remove(data, "mini")
            registry.save(path, data)
            self.assertNotIn("aliases", json.loads(path.read_text(encoding="utf-8")))
            for bad in ("not-an-object", {"m": {}}, {"m": {"routes": []}},
                        {"m": {"routes": ["codex/openai"]}}, {"m": {"routes": [1]}},
                        {"m/x": {"routes": ["codex/openai/gpt-6.1-sol"]}},
                        {"m": {"routes": ["codex/openai/gpt-6.1-sol", "codex/openai/gpt-6.1-sol"]}},
                        {"m": {"routes": ["codex/openai/gpt-6.1-sol"], "extra": 1}}):
                config = sample()
                config["aliases"] = bad
                with self.subTest(bad=bad), self.assertRaises(registry.ConfigError):
                    registry.save(tempfile.gettempdir() + "/never-written.json", config)


if __name__ == "__main__":
    unittest.main()
