"""Compare the registry with local harness catalogs."""

from __future__ import annotations

import registry
import sources

SCOPE = {"codex": "catalog", "opencode": "favorite", "zcode": "builtin-table", "agy": "model-list"}
REASON = {"codex": "not in Codex catalog", "opencode": "not in OpenCode favorites",
          "zcode": "not in ZCode builtin table", "agy": "not in Antigravity model list"}
OPENCODE_SCOPE_REASON = {"favorite": "not in OpenCode favorites",
                         "desktop-visible": "not visible in the OpenCode desktop"}
TITLE = {"codex": "Codex", "opencode": "OpenCode", "zcode": "ZCode", "agy": "Antigravity"}
COUNT_LABEL = {"codex": "offered", "opencode": "favorite", "zcode": "offered", "agy": "offered"}
OPENCODE_SCOPE_COUNT_LABEL = {"favorite": "favorite", "desktop-visible": "visible"}


def _parts(address):
    return registry.parse_address(address)


def _covers(parts, entry) -> bool:
    stored = _parts(entry)
    return len(stored) <= len(parts) and parts[:len(stored)] == stored


def _hidden(entries, address) -> bool:
    parts = _parts(address)
    return any(_covers(parts, entry) for entry in entries)


def registry_models(data, harness) -> list:
    return sorted(address for address, level, _ in registry.nodes(data)
                  if level == "model" and _parts(address)[0] == harness)


def override_models(data, harness) -> set:
    """Registered model addresses explicitly added outside the harness catalog
    (`add --override`): not catalog evidence, so never stale."""
    return {address for address, level, node in registry.nodes(data)
            if level == "model" and _parts(address)[0] == harness
            and isinstance(node, dict) and node.get("override")}


def hidden_entries(data, harness) -> list:
    return [entry for entry in data.get("hidden", []) if _parts(entry)[0] == harness]


def build(data, source_map) -> dict:
    report = {"sources": {}, "new": [], "stale": [], "hidden": registry.hidden_list(data),
              "_candidates": {}, "_registered": {}, "_new": set(), "_stale": set()}
    for harness in sources.HARNESSES:
        source = source_map[harness]
        entries = hidden_entries(data, harness)
        offered = list(source.candidates)
        offered_map = {item["address"]: item for item in offered}
        unavailable = getattr(source, "unavailable", None) or {}
        candidates = [item for item in offered if not _hidden(entries, item["address"])
                      and _parts(item["address"])[1] not in unavailable]
        registered = registry_models(data, harness)
        registered_set = set(registered)
        overrides = override_models(data, harness)
        candidate_addresses = {item["address"] for item in candidates}
        new_addresses = sorted(candidate_addresses - registered_set)
        suppressed = (not source.ok) or not offered
        # A provider the harness cannot load is not evidence against registry
        # entries: their staleness stays suppressed like a broken source's.
        # An override entry was added outside the catalog on purpose: the
        # catalog's silence is not evidence against it either.
        stale_addresses = [] if suppressed else sorted(
            address for address in registered_set - candidate_addresses
            if address not in overrides and _parts(address)[1] not in unavailable)

        warning = source.warning
        if source.ok and not offered:
            warning = "no candidate models; stale list suppressed"

        scope = getattr(source, "scope", None) or SCOPE[harness]
        stale_reason = (OPENCODE_SCOPE_REASON.get(scope, REASON[harness])
                        if harness == "opencode" else REASON[harness])
        state = {"harness": harness, "ok": source.ok, "warning": warning,
                 "candidate_scope": scope, "stale_scope": scope,
                 "providers": {}}
        report["_candidates"][harness] = offered_map
        report["_registered"][harness] = registered

        providers = set()
        for address in [item["address"] for item in offered] + registered + entries:
            providers.add(_parts(address)[1])
        for provider in sorted(providers):
            prefix = (harness, provider)
            offered_here = [item["address"] for item in offered
                            if _parts(item["address"])[1] == provider]
            state["providers"][provider] = {
                "favorite": len(offered_here),
                "registry": sum(1 for address in registered if _parts(address)[1] == provider),
                "hidden": sum(1 for entry in entries if _parts(entry)[:2] == prefix),
                "new": sum(1 for address in new_addresses if _parts(address)[1] == provider),
                "stale": sum(1 for address in stale_addresses if _parts(address)[1] == provider),
                "override": sum(1 for address in overrides if _parts(address)[1] == provider),
                "unavailable": unavailable.get(provider),
                # Every offered model is covered by a manual hidden entry:
                # text render keeps the line out of the provider table.
                "hidden_all": bool(offered_here)
                and all(_hidden(entries, address) for address in offered_here),
            }

        for address in new_addresses:
            item = dict(offered_map[address])
            item["harness"] = harness
            report["new"].append(item)
            report["_new"].add(address)
        for address in stale_addresses:
            report["stale"].append({"address": address, "reason": stale_reason})
            report["_stale"].add(address)
        report["sources"][harness] = state

    report["new"].sort(key=lambda item: item["address"])
    report["stale"].sort(key=lambda item: item["address"])
    for index, item in enumerate(report["new"], start=1):
        item["index"] = index
    for index, item in enumerate(report["stale"], start=1):
        item["index"] = index
    return report


def public(report) -> dict:
    return {
        "sources": report["sources"],
        "new": [{"index": item["index"], "address": item["address"], "favorite": item["favorite"]}
                for item in report["new"]],
        "stale": [{"index": item["index"], "address": item["address"], "reason": item["reason"]}
                  for item in report["stale"]],
        "hidden": list(report["hidden"]),
    }


def parse_spec(spec: str) -> list:
    tokens = [token.strip() for token in spec.split(",") if token.strip()]
    if not tokens:
        raise registry.ConfigError("an index list, an address list, or all is required")
    if "all" in tokens and len(tokens) > 1:
        raise registry.ConfigError("all cannot be combined with indices or addresses")
    return tokens


def parse_addresses(spec: str) -> list:
    tokens = parse_spec(spec)
    addresses = []
    for token in tokens:
        if token.isdigit():
            raise registry.ConfigError("use either indices or addresses, not both")
        parts = registry.canonical_address(token)
        if len(parts) != 3:
            raise registry.ConfigError(f"only model addresses are accepted here: {token}")
        address = "/".join(parts)
        if address not in addresses:
            addresses.append(address)
    return addresses


def pick_entries(entries, spec: str, label: str) -> list:
    tokens = parse_spec(spec)
    if all(token.isdigit() for token in tokens):
        picked = []
        for token in tokens:
            number = int(token)
            if not 1 <= number <= len(entries):
                raise registry.ConfigError(f"index {number} is outside 1..{len(entries)} of {label}")
            entry = entries[number - 1]
            if entry not in picked:
                picked.append(entry)
        return picked
    addresses = parse_addresses(spec)
    picked = []
    for address in addresses:
        entry = next((item for item in entries if item["address"] == address), None)
        if entry is None:
            raise registry.ConfigError(f"{address} is not in {label}")
        picked.append(entry)
    return picked


def render(report) -> str:
    lines = []
    for harness in sources.HARNESSES:
        state = report["sources"][harness]
        scope = state["candidate_scope"]
        count_label = (OPENCODE_SCOPE_COUNT_LABEL.get(scope, COUNT_LABEL[harness])
                       if harness == "opencode" else COUNT_LABEL[harness])
        lines.append(f"{TITLE[harness]} — источник: {scope}")
        if state["warning"]:
            lines.append(f"  warning: {state['warning']}")
        for provider, counts in state["providers"].items():
            # A provider whose every offered model is manually hidden and that
            # holds no registry activity is noise: it stays in --json and the
            # HIDDEN list, not in the text table.
            if (counts.get("hidden_all") and not counts["registry"] and not counts["new"]
                    and not counts["stale"] and not counts.get("unavailable")):
                continue
            extra = f" · {counts['override']} override" if counts.get("override") else ""
            lines.append(f"  {provider:<18} {counts['favorite']} {count_label} · "
                         f"{counts['registry']} in registry · {counts['hidden']} hidden → "
                         f"{counts['new']} new · {counts['stale']} stale{extra}")
            if counts.get("unavailable"):
                lines.append(f"    unavailable: {counts['unavailable']}")

    lines.append("")
    lines.append(f"NEW ({len(report['new'])})")
    group = None
    for item in report["new"]:
        harness, provider = _parts(item["address"])[:2]
        if (harness, provider) != group:
            group = (harness, provider)
            lines.append(f"  {harness}/{provider}")
        star = "★" if item["favorite"] else " "
        lines.append(f"    {star} {item['index']:>2}  {item['address']}")

    lines.append("")
    lines.append(f"STALE ({len(report['stale'])})")
    for item in report["stale"]:
        lines.append(f"     {item['index']:>2}  {item['address']}  ({item['reason']})")

    hidden = sorted(report["hidden"])
    lines.append("")
    lines.append(f"HIDDEN ({len(hidden)})" + ("  " + ", ".join(hidden) if hidden else ""))
    lines.append("")
    lines.append("★ favorite   --add 1,3 · --prune 1 · --json")
    return "\n".join(lines)
