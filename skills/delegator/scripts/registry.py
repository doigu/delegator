"""Configuration and selection helpers for the delegator model registry."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class ConfigError(ValueError):
    """Invalid registry configuration or ambiguous address."""


_EFFORT = re.compile(r"^[A-Za-z0-9_.-]+$")
_RFC3339 = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$")


def _empty() -> dict:
    return {
        "version": 1,
        "harnesses": {},
        "execution": {"status_interval_seconds": 1800, "max_runtime_seconds": None,
                      "capacity_retries": 3, "capacity_retry_seconds": 30,
                      "route_retries": 1},
        "hidden": [],
        "aliases": {},
    }


def resolve_path(cli=None) -> Path:
    if cli is not None:
        return Path(cli).expanduser()
    configured = os.environ.get("DELEGATOR_CONFIG")
    return Path(configured).expanduser() if configured else Path.home() / ".agents" / "delegator.json"


def parse_address(address: str) -> tuple[str, ...]:
    if not isinstance(address, str) or not address:
        raise ConfigError("address must be a non-empty string")
    parts = address.split("/", 2)
    if any(not part.strip() for part in parts):
        raise ConfigError(f"address contains an empty segment: {address!r}")
    if len(parts) == 3 and any(not segment.strip() for segment in parts[2].split("/")):
        raise ConfigError(f"address contains an empty model segment: {address!r}")
    return tuple(parts)


def canonical_address(address: str) -> tuple[str, ...]:
    """Parse an address, dropping exactly one leading slash."""
    if not isinstance(address, str):
        raise ConfigError("address must be a string")
    return parse_address(address[1:] if address.startswith("/") else address)


def _object(value, where, allowed, required=()):
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be an object")
    unknown = set(value) - set(allowed)
    missing = set(required) - set(value)
    if unknown:
        raise ConfigError(f"{where} has unknown key(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ConfigError(f"{where} is missing key(s): {', '.join(sorted(missing))}")


def _bool(value, where):
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be a boolean")


def _effort(value, where):
    if value is not None and (not isinstance(value, str) or not _EFFORT.fullmatch(value)):
        raise ConfigError(f"{where} must be null or a single effort name")


def _validate(config):
    _object(config, "config", {"version", "harnesses", "execution", "hidden", "aliases"},
            {"version", "harnesses", "execution"})
    if type(config["version"]) is not int or config["version"] != 1:
        raise ConfigError("version must be 1")
    if not isinstance(config["harnesses"], dict):
        raise ConfigError("harnesses must be an object")
    execution = config["execution"]
    _object(execution, "execution",
            {"status_interval_seconds", "max_runtime_seconds",
             "capacity_retries", "capacity_retry_seconds", "route_retries"},
            {"status_interval_seconds", "max_runtime_seconds"})
    interval, runtime = execution["status_interval_seconds"], execution["max_runtime_seconds"]
    if type(interval) not in (int, float) or not 0 <= interval < float('inf'):
        raise ConfigError("execution.status_interval_seconds must be a finite nonnegative number")
    if runtime is not None and (type(runtime) not in (int, float) or not 0 < runtime < float('inf')):
        raise ConfigError("execution.max_runtime_seconds must be null or a finite positive number")
    retries = execution.get("capacity_retries", 3)
    if type(retries) is not int or retries < 0:
        raise ConfigError("execution.capacity_retries must be a nonnegative integer")
    retry_seconds = execution.get("capacity_retry_seconds", 30)
    if type(retry_seconds) not in (int, float) or not 0 < retry_seconds < float('inf'):
        raise ConfigError("execution.capacity_retry_seconds must be a finite positive number")
    route_retries = execution.get("route_retries", 1)
    if type(route_retries) is not int or route_retries < 0:
        raise ConfigError("execution.route_retries must be a nonnegative integer")
    for harness, hnode in config["harnesses"].items():
        _valid_segment(harness, "harness")
        _object(hnode, f"harnesses.{harness}", {"enabled", "providers"}, {"enabled", "providers"})
        _bool(hnode["enabled"], f"harnesses.{harness}.enabled")
        if not isinstance(hnode["providers"], dict):
            raise ConfigError(f"harnesses.{harness}.providers must be an object")
        for provider, pnode in hnode["providers"].items():
            _valid_segment(provider, "provider")
            where = f"harnesses.{harness}.providers.{provider}"
            _object(pnode, where, {"enabled", "quota", "models"}, {"enabled", "quota", "models"})
            _bool(pnode["enabled"], f"{where}.enabled")
            quota = pnode["quota"]
            _object(quota, f"{where}.quota", {"blocked", "reset_at", "reason"}, {"blocked", "reset_at"})
            _bool(quota["blocked"], f"{where}.quota.blocked")
            reset = quota["reset_at"]
            if reset is not None:
                if not isinstance(reset, str) or not _RFC3339.fullmatch(reset):
                    raise ConfigError(f"{where}.quota.reset_at must be null or RFC3339")
                try:
                    datetime.fromisoformat(reset[:-1] + "+00:00" if reset.endswith("Z") else reset)
                except ValueError as exc:
                    raise ConfigError(f"{where}.quota.reset_at must be a valid RFC3339 timestamp") from exc
            if quota.get("reason") is not None and not isinstance(quota["reason"], str):
                raise ConfigError(f"{where}.quota.reason must be null or a string")
            if not isinstance(pnode["models"], dict):
                raise ConfigError(f"{where}.models must be an object")
            for model, mnode in pnode["models"].items():
                _valid_model(model)
                mwhere = f"{where}.models.{model}"
                _object(mnode, mwhere, {"enabled", "efforts", "default_effort", "override"},
                        {"enabled", "efforts", "default_effort"})
                _bool(mnode["enabled"], f"{mwhere}.enabled")
                if "override" in mnode:
                    _bool(mnode["override"], f"{mwhere}.override")
                efforts = mnode["efforts"]
                if not isinstance(efforts, list) or any(not isinstance(e, str) or not _EFFORT.fullmatch(e) for e in efforts):
                    raise ConfigError(f"{mwhere}.efforts must contain supported effort strings")
                if len(set(efforts)) != len(efforts):
                    raise ConfigError(f"{mwhere}.efforts must not contain duplicates")
                _effort(mnode["default_effort"], f"{mwhere}.default_effort")
                if mnode["default_effort"] is not None and mnode["default_effort"] not in efforts:
                    raise ConfigError(f"{mwhere}.default_effort must be listed in efforts")
    _validate_hidden(config)
    _validate_aliases(config)


def _validate_hidden(config):
    hidden = config.get("hidden")
    if hidden is None:
        return
    if not isinstance(hidden, list):
        raise ConfigError("hidden must be a list")
    seen = set()
    for index, entry in enumerate(hidden):
        if not isinstance(entry, str):
            raise ConfigError(f"hidden[{index}] must be a string")
        if entry.startswith("/"):
            raise ConfigError(f"hidden[{index}] must not start with '/': {entry!r}")
        parts = parse_address(entry)
        if len(parts) < 2:
            raise ConfigError(f"hidden[{index}] must be harness/provider or a model address: {entry!r}")
        if entry in seen:
            raise ConfigError(f"hidden contains duplicate address: {entry!r}")
        seen.add(entry)
        for address, level, _ in nodes(config):
            if level == "harness":
                continue
            node = parse_address(address)
            if node[:len(parts)] == parts:
                raise ConfigError(f"hidden address {entry!r} covers the registry entry {address!r}; "
                                  "delete it first")


def _validate_aliases(config):
    aliases = config.get("aliases")
    if aliases is None:
        return
    if not isinstance(aliases, dict):
        raise ConfigError("aliases must be an object")
    for name, entry in aliases.items():
        if "/" in name or not name.strip():
            raise ConfigError(f"alias name must be a non-empty name without '/': {name!r}")
        _object(entry, f"aliases.{name}", {"routes"}, {"routes"})
        routes = entry["routes"]
        if not isinstance(routes, list) or not routes:
            raise ConfigError(f"aliases.{name}.routes must be a nonempty list")
        for index, route in enumerate(routes):
            if not isinstance(route, str):
                raise ConfigError(f"aliases.{name}.routes[{index}] must be a string")
            if len(parse_address(route)) < 3:
                raise ConfigError(f"aliases.{name}.routes[{index}] must be a full "
                                  f"harness/provider/model address: {route!r}")
        if len(set(routes)) != len(routes):
            raise ConfigError(f"aliases.{name}.routes must not contain duplicates")


def _valid_segment(value, kind):
    if not isinstance(value, str) or not value.strip() or "/" in value:
        raise ConfigError(f"{kind} key must be a non-empty segment without '/'")


def _valid_model(value):
    if not isinstance(value, str) or not value.strip() or any(not p.strip() for p in value.split("/")):
        raise ConfigError("model key must be non-empty and contain no empty slash-separated segments")


def load(path) -> dict:
    path = Path(path).expanduser()
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream, object_pairs_hook=_unique_keys)
    except FileNotFoundError:
        return _empty()
    except (json.JSONDecodeError, UnicodeDecodeError, ConfigError) as exc:
        raise ConfigError(f"invalid config {path}: {exc}") from exc
    try:
        _validate(data)
    except ConfigError as exc:
        raise ConfigError(f"invalid config {path}: {exc}") from exc
    data.setdefault("hidden", [])
    data.setdefault("aliases", {})
    return data


def _unique_keys(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ConfigError(f"duplicate config key: {key}")
        data[key] = value
    return data


def save(path, data):
    _validate(data)
    payload = dict(data)
    if not payload.get("hidden"):
        payload.pop("hidden", None)
    if not payload.get("aliases"):
        payload.pop("aliases", None)
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def nodes(data) -> list:
    result = []
    for harness, hnode in data["harnesses"].items():
        result.append((harness, "harness", hnode))
        for provider, pnode in hnode["providers"].items():
            address = f"{harness}/{provider}"
            result.append((address, "provider", pnode))
            for model, mnode in pnode["models"].items():
                result.append((f"{address}/{model}", "model", mnode))
    return result


def hidden_list(data) -> list:
    return list(data.get("hidden", []))


def hidden_covering(data, address):
    """Return the stored hidden entry covering an address, if any."""
    parts = canonical_address(address)
    for entry in data.get("hidden", []):
        stored = parse_address(entry)
        if len(stored) <= len(parts) and parts[:len(stored)] == stored:
            return entry
    return None


def hidden_add(data, address) -> list:
    parts = canonical_address(address)
    if "*" in "/".join(parts):
        # Hidden entries are literal prefixes; a silent wildcard store would
        # hide nothing while looking like it did.
        raise ConfigError("hidden entries are literal prefixes; '*' has no glob meaning: "
                          + "/".join(parts))
    if len(parts) < 2:
        raise ConfigError("hidden address must be harness/provider or a model address")
    if hidden_covering(data, address) is not None:
        return []
    entry = "/".join(parts)
    hidden = data.setdefault("hidden", [])
    hidden.append(entry)
    try:
        _validate(data)
    except ConfigError:
        hidden.pop()
        raise
    return [entry]


def hidden_remove(data, address) -> list:
    parts = canonical_address(address)
    entry = "/".join(parts)
    hidden = data.get("hidden", [])
    if entry in hidden:
        hidden.remove(entry)
        _validate(data)
        return [entry]
    return []


def select(data, selector) -> list:
    if not isinstance(selector, str) or not selector:
        raise ConfigError("selector must be a non-empty string")
    rooted = selector.startswith("/")
    raw = selector[1:] if rooted else selector
    parse_address(raw)
    matches = [(address, level, node) for address, level, node in nodes(data)
               if address == raw or (not rooted and address.endswith("/" + raw))]
    if not matches:
        raise ConfigError(f"no registry node matches {selector!r}")
    levels = {item[1] for item in matches}
    if len(levels) > 1:
        candidates = ", ".join(item[0] for item in matches)
        raise ConfigError(f"selector {selector!r} matches multiple levels; use a rooted or qualified address: {candidates}")
    return matches


def _reset_expired(quota, now):
    reset = quota["reset_at"]
    if not quota["blocked"] or reset is None:
        return False
    expires = datetime.fromisoformat(reset[:-1] + "+00:00" if reset.endswith("Z") else reset)
    return expires <= now


def available(data, now=None) -> list:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    result = []
    for address, level, node in nodes(data):
        if level != "model":
            continue
        harness, provider, *_ = parse_address(address)
        hnode = data["harnesses"][harness]
        pnode = hnode["providers"][provider]
        quota = pnode["quota"]
        expired = _reset_expired(quota, now)
        reason = None
        ok = True
        if not hnode["enabled"]:
            ok, reason = False, f"harness {harness} is disabled"
        elif not pnode["enabled"]:
            ok, reason = False, f"provider {harness}/{provider} is disabled"
        elif quota["blocked"] and not expired:
            ok, reason = False, quota.get("reason") or "provider quota is blocked"
        elif not node["enabled"]:
            ok, reason = False, f"model {address} is disabled"
        result.append({"address": address, "enabled": node["enabled"], "available": ok,
                       "reason": reason, "efforts": list(node["efforts"]),
                       "default_effort": node["default_effort"],
                       "override": bool(node.get("override"))})
    return result


def add(data, address, efforts=None, effort=None, override=False) -> list:
    parts = parse_address(address)
    if len(parts) < 3 and (efforts is not None or effort is not None):
        raise ConfigError("efforts and effort can be set only for a model")
    if len(parts) < 3 and override:
        raise ConfigError("override can be set only for a model")
    if efforts is not None:
        if not isinstance(efforts, list) or any(not isinstance(e, str) or not _EFFORT.fullmatch(e) for e in efforts):
            raise ConfigError("efforts must be a list of effort names")
        if len(set(efforts)) != len(efforts):
            raise ConfigError("efforts must not contain duplicates")
    _effort(effort, "effort")
    hidden = data.get("hidden", [])
    exact = next((entry for entry in hidden if parse_address(entry) == parts), None)
    wider = next((entry for entry in hidden
                  if len(parse_address(entry)) < len(parts)
                  and parts[:len(parse_address(entry))] == parse_address(entry)), None)
    if wider is not None:
        raise ConfigError(f"{address} is hidden by {wider!r}; unhide it first")
    removed = None
    if exact is not None:
        if len(parts) != 3:
            raise ConfigError(f"{address} matches the hidden prefix {exact!r}; unhide it first")
        removed = exact
    harness = data["harnesses"].setdefault(parts[0], {"enabled": True, "providers": {}})
    if len(parts) == 1:
        return [parts[0]]
    provider = harness["providers"].setdefault(parts[1], {
        "enabled": True, "quota": {"blocked": False, "reset_at": None, "reason": None}, "models": {}})
    if len(parts) == 2:
        return ["/".join(parts)]
    model = provider["models"].setdefault(parts[2], {"enabled": True, "efforts": [], "default_effort": None})
    if efforts is not None:
        model["efforts"] = list(efforts)
    if effort is not None:
        model["default_effort"] = effort
        if efforts is None and effort not in model["efforts"]:
            model["efforts"].append(effort)
    # Stored only when true: a native catalog model needs no override mark.
    if override:
        model["override"] = True
    if removed is not None:
        hidden.remove(removed)
    try:
        _validate(data)
    except ConfigError:
        if removed is not None:
            hidden.append(removed)
        raise
    return ["/".join(parts)]


def quota_providers(data, selector) -> list:
    """Providers whose quota a selector affects: itself, its subtree, or its ancestor."""
    result, seen = [], set()
    for address, level, node in select(data, selector):
        if level == "provider":
            candidates = [(address, node)]
        elif level == "harness":
            candidates = [(f"{address}/{provider}", pnode)
                          for provider, pnode in node["providers"].items()]
        else:
            harness, provider, _ = parse_address(address)
            candidates = [(f"{harness}/{provider}",
                           data["harnesses"][harness]["providers"][provider])]
        for candidate_address, provider_node in candidates:
            if candidate_address not in seen:
                seen.add(candidate_address)
                result.append((candidate_address, provider_node))
    return result


def toggle(data, selector, enabled) -> list:
    _bool(enabled, "enabled")
    selected = select(data, selector)
    for _, _, node in selected:
        node["enabled"] = enabled
    if enabled:
        for _, provider in quota_providers(data, selector):
            provider["quota"] = {"blocked": False, "reset_at": None, "reason": None}
    return [address for address, _, _ in selected]


def delete(data, selector, recursive=False, yes=False, dry_run=False) -> list:
    selected = select(data, selector)
    targets = []
    for address, level, node in selected:
        children = bool(node.get("providers") or node.get("models"))
        if children and not recursive:
            raise ConfigError(f"{address} has children; recursive deletion is required")
        targets.append(address)
        if recursive and level == "harness":
            targets.extend(a for a, _, _ in nodes(data) if a.startswith(address + "/"))
        elif recursive and level == "provider":
            targets.extend(a for a, _, _ in nodes(data) if a.startswith(address + "/"))
    targets = list(dict.fromkeys(targets))
    if not dry_run and (len(targets) > 1 or recursive) and not yes:
        raise ConfigError("multiple or recursive deletion requires yes=True")
    if dry_run:
        return targets
    for address, level, _ in sorted(selected, key=lambda item: item[0].count("/"), reverse=True):
        parts = parse_address(address)
        if level == "harness":
            del data["harnesses"][parts[0]]
        elif level == "provider":
            del data["harnesses"][parts[0]]["providers"][parts[1]]
        else:
            del data["harnesses"][parts[0]]["providers"][parts[1]]["models"][parts[2]]
    return targets


def block_quota(path, harness, provider, reason, reset_at=None):
    data = load(path)
    _valid_segment(harness, "harness")
    _valid_segment(provider, "provider")
    if not isinstance(reason, str) or not reason.strip():
        raise ConfigError("quota reason must be a non-empty string")
    quota = data["harnesses"][harness]["providers"][provider]["quota"]
    quota.update(blocked=True, reset_at=reset_at, reason=reason)
    _validate(data)
    save(path, data)


def alias_names(data) -> list:
    return sorted(data.get("aliases", {}))


def alias_routes(data, name) -> list:
    return list(data.get("aliases", {}).get(name, {}).get("routes", []))


def alias_set(data, name, routes) -> list:
    if not isinstance(name, str) or "/" in name or not name.strip():
        raise ConfigError("alias name must be a non-empty name without '/'")
    if not isinstance(routes, list) or not routes:
        raise ConfigError("alias routes must be a nonempty list")
    registered = {address for address, level, _ in nodes(data) if level == "model"}
    parsed = []
    for route in routes:
        if not isinstance(route, str):
            raise ConfigError("alias routes must be strings")
        parts = parse_address(route)
        if len(parts) < 3:
            raise ConfigError(f"alias route must be a full harness/provider/model address: {route!r}")
        address = "/".join(parts)
        if address not in registered:
            raise ConfigError(f"alias route is not a registered model: {address}")
        parsed.append(address)
    if len(set(parsed)) != len(parsed):
        raise ConfigError("alias routes must not contain duplicates")
    previous = data.get("aliases", {}).get(name)
    data.setdefault("aliases", {})[name] = {"routes": parsed}
    try:
        _validate(data)
    except ConfigError:
        if previous is None:
            data["aliases"].pop(name, None)
        else:
            data["aliases"][name] = previous
        raise
    return parsed


def alias_remove(data, name) -> bool:
    if name in data.get("aliases", {}):
        del data["aliases"][name]
        _validate(data)
        return True
    return False


def alias_references(data, address) -> list:
    """Aliases whose routes equal the address or lie under it as a prefix."""
    parts = parse_address(address)
    result = []
    for name in alias_names(data):
        for route in alias_routes(data, name):
            route_parts = parse_address(route)
            if route_parts[:len(parts)] == parts:
                result.append(name)
                break
    return result
