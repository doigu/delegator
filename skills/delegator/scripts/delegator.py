#!/usr/bin/env python3
"""Explicit, single-worker delegation and offline model registry CLI."""

import argparse
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import registry
import sources
import sync as sync_module
from native import redact
from runner import CodedError, _inside_git_repo, run, status

MODULES = {'codex', 'opencode', 'zcode', 'agy', 'claude'}
# Registry provider slug each single-connection harness can execute; for Codex
# the adapter passes it to the CLI as model_provider.
RUNNABLE_PROVIDER = {'codex': 'openai', 'agy': 'gemini', 'claude': 'anthropic'}

# Codex, Claude, and Antigravity resume take a UUID; OpenCode resume takes its
# native session id, ZCode a sess_<UUID>. --continue is banned: it can pick up
# a foreign session.
UUID_SESSION = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                         r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
RESUME_PATTERNS = {
    'codex': UUID_SESSION,
    'claude': UUID_SESSION,
    'agy': UUID_SESSION,
    'opencode': re.compile(r'^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$'),
    'zcode': re.compile(r'^sess_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
                        r'[0-9a-f]{4}-[0-9a-f]{12}$'),
}
# Documented exception: headless agy denies every action that needs a
# permission, so the agy default shell level is full for both modes.
AGY_DEFAULT_FULL_WARNING = ('agy runs with the default shell full '
                            '(--dangerously-skip-permissions); pass --shell none '
                            'for a prompt-only task')


def model_rows(data):
    rows = registry.available(data)
    for row in rows:
        harness, provider, _ = registry.parse_address(row['address'])
        if harness not in MODULES or (harness in RUNNABLE_PROVIDER
                                      and RUNNABLE_PROVIDER[harness] != provider):
            row.update(available=False, reason='No executable module for this connection')
    return rows


def active_models(data):
    return [{'address': r['address'], 'efforts': r['efforts'], 'default_effort': r['default_effort']}
            for r in model_rows(data) if r['available']]


def check_catalog(harness, provider, model, efforts):
    """Use native catalog when available; never import its contents or infer a default."""
    if harness == 'codex':
        path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'models_cache.json'
        if not path.is_file():
            return 'Codex local catalog unavailable; entry recorded without catalog verification'
        try:
            catalog = json.loads(path.read_text(encoding='utf-8'))['models']
        except (OSError, ValueError, KeyError):
            return 'Codex local catalog unreadable; entry recorded without catalog verification'
        matches = [r for r in catalog if r.get('slug') == model]
        if not matches:
            raise registry.ConfigError(f'Model absent from the local Codex catalog: {model}')
        supported = {r['effort'] for r in matches[0].get('supported_reasoning_levels', [])}
        if supported and any(e not in supported for e in efforts):
            raise registry.ConfigError('Effort absent from native model catalog; supported: ' + ', '.join(sorted(supported)))
    elif harness == 'opencode' and shutil.which('opencode'):
        try:
            result = subprocess.run(['opencode', 'models', provider], capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return 'OpenCode catalog unavailable; entry recorded without catalog verification'
        if result.returncode:
            return 'OpenCode catalog failed; entry recorded without catalog verification'
        if provider + '/' + model not in result.stdout.splitlines():
            raise registry.ConfigError(f'Model absent from the native OpenCode catalog: {provider}/{model}')
    elif harness == 'agy' and shutil.which('agy'):
        try:
            result = subprocess.run(['agy', 'models'], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return 'Antigravity catalog unavailable; entry recorded without catalog verification'
        if result.returncode:
            return 'Antigravity catalog failed; entry recorded without catalog verification'
        catalog = set(sources.agy_model_names(result.stdout))
        if model not in catalog:
            raise registry.ConfigError(f'Model absent from the native Antigravity catalog: {model}')
    elif harness == 'zcode':
        return sources.zcode_catalog_check(provider, model, efforts)
    return None


def management(args, path):
    data = registry.load(path)
    if args.command == 'list':
        all_nodes = registry.nodes(data)
        if args.address:
            selected = registry.select(data, args.address)
            prefixes = [a for a, _, _ in selected]
            all_nodes = [n for n in all_nodes if any(n[0] == p or n[0].startswith(p + '/') for p in prefixes)]
        models = {r['address']: r for r in model_rows(data)}
        rows = []
        for address, level, node in all_nodes:
            if level == 'model':
                row = dict(models[address], level=level)
            else:
                parts = registry.parse_address(address)
                enabled = data['harnesses'][parts[0]]['enabled'] and node['enabled']
                row = dict(address=address, level=level, enabled=node['enabled'], available=enabled,
                           reason=None if enabled else 'disabled node or ancestor')
                if level == 'provider':
                    quota = node['quota']
                    from datetime import datetime, timezone
                    blocked = quota['blocked'] and not registry._reset_expired(quota, datetime.now(timezone.utc))
                    if blocked:
                        row.update(available=False, reason=quota.get('reason') or 'provider quota blocked')
                    row['quota'] = quota
            if not args.active or row['available']:
                rows.append(row)
        return {'config': str(path), 'nodes': rows}
    warnings, reset_providers = [], []
    if args.command == 'add':
        parts = registry.parse_address(args.address)
        override = bool(getattr(args, 'override', False))
        if override:
            if len(parts) != 3:
                raise registry.ConfigError('--override applies to a model address')
            if parts[0] != 'zcode':
                raise registry.ConfigError('--override applies to zcode models only; '
                                           'other harnesses have no builtin-table gate')
            warnings.append('added without native catalog verification (override)')
            if args.efforts is None and args.effort is None:
                derived = sources.zcode_model_efforts(parts[2])
                if derived:
                    args.efforts = derived
                else:
                    warnings.append('override: the model is absent from the builtin table entirely; '
                                    'pass --efforts or exec cannot select a reasoning level')
        elif len(parts) == 3:
            warning = check_catalog(*parts, args.efforts or ([args.effort] if args.effort else []))
            if warning:
                warnings.append(warning)
        addresses = registry.add(data, args.address, args.efforts, args.effort, override=override)
    elif args.command in ('on', 'off'):
        if args.command == 'on':
            reset_providers = [a for a, n in registry.quota_providers(data, args.address)
                               if n['quota']['blocked']]
        addresses = registry.toggle(data, args.address, args.command == 'on')
    else:
        # Show the entire plan even when confirmation is missing.
        planned = registry.delete(data, args.address, recursive=args.recursive, dry_run=True)
        if not args.dry_run and (len(planned) > 1 or args.recursive) and not args.yes:
            raise registry.ConfigError('Deletion requires --yes; planned nodes: ' + ', '.join(planned))
        addresses = registry.delete(data, args.address, args.recursive, args.yes, args.dry_run)
        aliases = sorted({name for address in addresses
                          for name in registry.alias_references(data, address)})
        for name in aliases:
            warnings.append(f'alias {name} still references a deleted address; '
                            'fix it with alias add or alias remove')
    if not getattr(args, 'dry_run', False):
        registry.save(path, data)
    return {'command': args.command, 'addresses': addresses, 'dry_run': getattr(args, 'dry_run', False),
            'quota_cleared': reset_providers, 'warnings': warnings}


def add_targets(report, data, spec):
    if all(token.isdigit() for token in sync_module.parse_spec(spec)):
        return sync_module.pick_entries(report['new'], spec, 'NEW')
    picked = []
    registered = {address for address, level, _ in registry.nodes(data) if level == 'model'}
    for address in sync_module.parse_addresses(spec):
        entry = next((item for item in report['new'] if item['address'] == address), None)
        if entry is not None:
            picked.append(entry)
            continue
        harness = registry.parse_address(address)[0]
        candidate = report['_candidates'].get(harness, {}).get(address)
        if candidate is not None and (address in registered
                                      or registry.hidden_covering(data, address) == address):
            item = dict(candidate)
            item['harness'] = harness
            picked.append(item)
            continue
        raise registry.ConfigError(f'{address} is not in NEW; use add for arbitrary models')
    return picked


def hide_targets(report, spec):
    """Addresses to hide: NEW indices, NEW model addresses, or known provider prefixes."""
    tokens = sync_module.parse_spec(spec)
    if all(token.isdigit() for token in tokens):
        return [item['address'] for item in sync_module.pick_entries(report['new'], spec, 'NEW')]
    new_addresses = {item['address'] for item in report['new']}
    addresses = []
    for token in tokens:
        if token.isdigit():
            raise registry.ConfigError('use either indices or addresses, not both')
        parts = registry.canonical_address(token)
        address = '/'.join(parts)
        if len(parts) == 3:
            if address not in new_addresses:
                raise registry.ConfigError(f'{address} is not in NEW; registered models must be '
                                           'deleted first, other prefixes use hide')
        elif len(parts) == 2:
            state = report['sources'].get(parts[0])
            if state is None or parts[1] not in state['providers']:
                raise registry.ConfigError(f'{address} is not in the sync report')
        else:
            raise registry.ConfigError(f'only model or provider addresses are accepted here: {token}')
        if address not in addresses:
            addresses.append(address)
    return addresses


def sync_command(args, data, path):
    source_map = sources.read_sources()
    report = sync_module.build(data, source_map)
    text = [sync_module.render(report)]
    if args.add is None and args.prune is None and args.hide is None:
        return sync_module.public(report), text

    values = args.add if args.add is not None else (args.prune if args.prune is not None
                                                    else args.hide)
    spec = ','.join(values).strip()
    all_mode = sync_module.parse_spec(spec) == ['all']
    applied, skipped, alias_warnings = [], [], []
    if args.add is not None:
        if all_mode:
            if not args.yes:
                raise registry.ConfigError('all requires --yes')
            picked = list(report['new'])
        else:
            picked = add_targets(report, data, spec)
        planned = [item['address'] for item in picked]
        if planned and not args.json:
            print('planned: ' + ', '.join(planned))
        registered = set().union(*report['_registered'].values())
        for item in picked:
            if item['address'] in registered:
                skipped.append(item['address'])
                continue
            registry.add(data, item['address'], item['efforts'], item['default_effort'])
            applied.append(item['address'])
        label = 'added'
    elif args.hide is not None:
        if all_mode:
            if not args.yes:
                raise registry.ConfigError('all requires --yes')
            picked = [item['address'] for item in report['new']]
        else:
            picked = hide_targets(report, spec)
        if picked and not args.json:
            print('planned: ' + ', '.join(picked))
        for address in picked:
            if registry.hidden_add(data, address):
                applied.append(address)
            else:
                skipped.append(address)
        label = 'hidden'
    else:
        if all_mode:
            if not args.yes:
                raise registry.ConfigError('all requires --yes')
            picked = list(report['stale'])
        else:
            picked = sync_module.pick_entries(report['stale'], spec, 'STALE')
        planned = [item['address'] for item in picked]
        if planned and not args.json:
            print('planned: ' + ', '.join(planned))
        for item in picked:
            registry.delete(data, item['address'])
            applied.append(item['address'])
        label = 'pruned'
        alias_warnings = [f'alias {name} still references a deleted address; '
                          'fix it with alias add or alias remove'
                          for name in sorted({n for address in applied
                                              for n in registry.alias_references(data, address)})]
    if applied:
        registry.save(path, data)
    report = sync_module.build(data, source_map)
    text = [sync_module.render(report)]
    text.append(f'{label}: ' + (', '.join(applied) if applied else 'none'))
    if skipped:
        text.append('skipped: ' + ', '.join(skipped))
    payload = sync_module.public(report)
    payload.update(applied=applied, skipped=skipped, warnings=alias_warnings)
    return payload, text


def hidden_command(args, data, path):
    if args.command == 'hidden':
        entries = registry.hidden_list(data)
        return ({'command': 'hidden', 'hidden': entries},
                [', '.join(entries) if entries else 'hidden: none'])
    added, removed, skipped, warnings = [], [], [], []
    for address in args.address:
        canonical = '/'.join(registry.canonical_address(address))
        if args.command == 'hide':
            entry = registry.hidden_add(data, address)
            if entry:
                added.extend(entry)
            else:
                skipped.append(canonical)
        else:
            gone = registry.hidden_remove(data, address)
            if gone:
                removed.extend(gone)
                continue
            covering = registry.hidden_covering(data, address)
            if covering is not None:
                warnings.append(f'{canonical} is covered by hidden prefix {covering}; unhide {covering}')
            skipped.append(canonical)
    if added or removed:
        registry.save(path, data)
    changed = added if args.command == 'hide' else removed
    text = [f"{args.command}: " + (', '.join(changed) if changed else 'none')]
    if skipped:
        text.append('no-op: ' + ', '.join(skipped))
    payload = {'command': args.command, 'added': added, 'removed': removed,
               'skipped': skipped, 'warnings': warnings}
    return payload, text


def registry_command(args, path):
    data = registry.load(path)
    if args.command == 'sync':
        return sync_command(args, data, path)
    return hidden_command(args, data, path)


def alias_command(args, data, path):
    if args.alias_command == 'list':
        aliases = {name: registry.alias_routes(data, name) for name in registry.alias_names(data)}
        text = [name + ': ' + ', '.join(routes) for name, routes in aliases.items()]
        if not text:
            text = ['aliases: none']
        return {'command': 'alias', 'aliases': aliases}, text
    if args.alias_command == 'remove':
        if registry.alias_remove(data, args.name):
            registry.save(path, data)
            return {'command': 'alias', 'removed': [args.name]}, ['alias removed: ' + args.name]
        return {'command': 'alias', 'removed': []}, ['alias remove: none (no alias named ' + args.name + ')']
    # PowerShell passes an unquoted `a,b` as an argument array; the tokens are
    # joined back into one comma-separated spec.
    routes = [token for token in (part.strip() for part in ','.join(args.routes).split(',')) if token]
    saved = registry.alias_set(data, args.name, routes)
    registry.save(path, data)
    return ({'command': 'alias', 'name': args.name, 'routes': saved},
            ['alias ' + args.name + ': ' + ', '.join(saved)])


def retry_wait(seconds):
    time.sleep(seconds)


class RoutePreparationError(ValueError):
    """Launch preparation inside the harness adapter failed."""


def run_with_capacity_retries(adapter, executable, repo, provider, model, effort, mode, shell,
                              prompt, target, interval, runtime, log_dir, resume_session,
                              retries, retry_seconds, track=True, git_check=True, override=False):
    """Run the worker, retrying a classified capacity failure.

    A retry resumes the failed attempt's observed native session when there is
    one, otherwise it starts fresh with the same prompt. runtime stays a
    whole-call budget: a retry gets only the unspent remainder. The shell
    level is rebuilt per attempt like any other launch argument. A launch
    preparation failure inside the adapter raises RoutePreparationError: a
    full-address call reports it as a configuration error, an alias chain
    skips to the next route.
    """
    started = time.monotonic()
    results, notes = [], []
    attempt = 0
    while True:
        attempt += 1
        resume = resume_session if attempt == 1 else (results[-1].get('session_id') or resume_session)
        try:
            prepared = adapter.command(executable, repo, provider, model, effort, mode, shell,
                                       resume, git_check=git_check, override=override)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            raise RoutePreparationError(str(exc)) from exc
        if len(prepared) == 3:
            command, env, session = prepared
        else:
            command, env, session = *prepared, None
        attempt_runtime = runtime
        if runtime is not None and attempt > 1:
            attempt_runtime = max(runtime - (time.monotonic() - started), 0.001)
        results.append(run(adapter, command, env, repo, prompt, target, interval, attempt_runtime,
                           log_dir, resume, session, track))
        result = results[-1]
        if result['stop_reason'] != 'capacity' or attempt > retries:
            break
        delay = retry_seconds * (2 ** (attempt - 1))
        if runtime is not None and runtime - (time.monotonic() - started) <= 0:
            notes.append('Capacity retry skipped: the whole-call runtime budget is spent')
            break
        next_resume = result.get('session_id') or resume_session
        notes.append(f'Attempt {attempt} failed (capacity); retrying after {int(round(delay))}s '
                     f'({"resume" if next_resume else "fresh"})')
        status(sys.stderr, f'capacity | attempt {attempt} failed | retrying in '
                           f'{int(round(delay))}s | {"resume" if next_resume else "fresh"}')
        try:
            retry_wait(delay)
        except KeyboardInterrupt:
            notes.append('Capacity retry interrupted; returning the last failed result')
            break
    final, first = results[-1], results[0]
    final['attempts'] = len(results)
    final['log_paths'] = [r['log_path'] for r in results]
    if len(results) > 1:
        final['preexisting_changed_paths'] = first['preexisting_changed_paths']
        usage = {}
        for r in results:
            for key, value in (r.get('usage') or {}).items():
                if type(value) in (int, float):
                    usage[key] = usage.get(key, 0) + value
        final['usage'] = usage or None
        final['elapsed_seconds'] = round(time.monotonic() - started, 3)
        final['resumed'] = resume_session is not None
    final['warnings'] = notes + list(final.get('warnings') or [])
    return final


ENVIRONMENT_REASON = ('The calling environment blocks writes to the user profile: {home}; '
                      'native CLI state lives there and no route can run; '
                      'invoke outside a restricted sandbox')
SKIPPED_ENVIRONMENT_REASON = ('The calling environment blocks writes to the native CLI state; '
                              'every route was skipped before launch; '
                              'invoke outside a restricted sandbox')
SKIPPED_ENVIRONMENT_MARKER = 'not writable'


def profile_home():
    return Path.home()


def profile_is_writable(home=None):
    """One canary for exec/ask: native CLIs keep their state in the profile.

    A restricted sandbox (Codex workspace-write/read-only and the like) blocks
    writes there, so every route would fail before launch; fail fast with a
    reason the calling agent can act on instead of per-route skips (issue #112).
    """
    home = home or profile_home()
    probe = home / f'.delegator-write-probe-{os.getpid()}'
    try:
        probe.write_text('', encoding='utf-8')
        probe.unlink()
        return True
    except OSError:
        return False


def result_exit_code(result):
    """0 completed, 3 a worker question, 1 every other non-success outcome."""
    return {'completed': 0, 'needs_input': 3}.get(result.get('status'), 1)


def execute(args, path, repo=None, track=True, git_check=True):
    home = profile_home()
    if not profile_is_writable(home):
        return {'status': 'configuration_error', 'code': 'profile_not_writable',
                'reason': ENVIRONMENT_REASON.format(home=home)}, 2
    if getattr(args, 'result_file', None) is not None:
        target = Path(args.result_file).expanduser()
        if _inside_git_repo(target):
            raise CodedError('result_file_invalid',
                             f'--result-file must be outside every Git repository: {target}')
        if target.exists():
            raise CodedError('result_file_invalid', f'--result-file must not exist: {target}')
    data = registry.load(path)
    if args.model is None:
        return {'status': 'configuration_error', 'code': 'model_not_selected',
                'reason': 'Select an available full model address',
                'available_models': active_models(data)}, 2
    alias_name = None
    route_retries = None
    if '/' not in args.model:
        alias_name = args.model
        if getattr(args, 'resume_session', None) is not None:
            raise registry.ConfigError('--resume-session continues one native session; '
                                       'resume with the full address reported by the attempt')
        if args.capacity_retries is not None or args.capacity_retry_seconds is not None:
            raise registry.ConfigError('--capacity-retries/--capacity-retry-seconds apply to a full '
                                       'model address; for an alias use --route-retries')
        routes = registry.alias_routes(data, alias_name)
        if not routes:
            return {'status': 'configuration_error', 'code': 'unknown_alias',
                    'reason': f'Unknown alias: {alias_name!r}',
                    'aliases': registry.alias_names(data)}, 2
        route_retries = args.route_retries if args.route_retries is not None \
            else data['execution'].get('route_retries', 1)
    elif args.route_retries is not None:
        raise registry.ConfigError('--route-retries applies to an alias; a full model address '
                                   'uses --capacity-retries')
    if route_retries is not None and (type(route_retries) is not int or route_retries < 0):
        raise registry.ConfigError('route retries must be a nonnegative integer')
    if repo is None:
        repo = Path(args.repo).expanduser().resolve()
        root = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
        if root.returncode or Path(root.stdout.strip()).resolve() != repo:
            raise registry.ConfigError('--repo must be the root of a Git working copy')
    prompt = Path(args.prompt_file).read_text(encoding='utf-8-sig') if args.prompt_file else args.prompt
    if not prompt or not prompt.strip():
        raise registry.ConfigError('A nonempty --prompt or --prompt-file is required')
    shell = args.shell
    if shell is None:
        shell = 'none' if args.mode == 'review' else 'auto'
    interval = data['execution']['status_interval_seconds'] if args.status_interval_seconds is None else args.status_interval_seconds
    runtime = data['execution']['max_runtime_seconds'] if args.max_runtime_seconds is None else args.max_runtime_seconds
    retry_seconds = args.capacity_retry_seconds if args.capacity_retry_seconds is not None else data['execution'].get('capacity_retry_seconds', 30)
    if not math.isfinite(interval) or interval < 0 or (runtime is not None and (not math.isfinite(runtime) or runtime <= 0)):
        raise registry.ConfigError('Status interval must be finite and >= 0; runtime must be finite and > 0')
    if not math.isfinite(retry_seconds) or retry_seconds <= 0:
        raise registry.ConfigError('capacity retry seconds must be finite and > 0')
    if alias_name is not None:
        rows = {r['address']: r for r in model_rows(data)}
        route_payload, candidates = [], []
        for address in routes:
            row = rows.get(address)
            if row is None:
                route_payload.append({'address': address, 'available': False, 'reason': 'not registered'})
            elif not row['available']:
                route_payload.append({'address': address, 'available': False, 'reason': row['reason']})
            else:
                effort = args.effort if args.effort is not None else row['default_effort']
                if args.effort is not None and args.effort not in row['efforts']:
                    route_payload.append({'address': address, 'available': False,
                                          'reason': 'effort ' + args.effort + ' is not configured; configured: '
                                                    + ', '.join(row['efforts'])})
                else:
                    route_payload.append({'address': address, 'available': True, 'reason': None})
                    harness, provider, model = registry.parse_address(address)
                    candidates.append({'address': address, 'harness': harness, 'provider': provider,
                                       'model': model, 'effort': effort,
                                       'override': bool(row.get('override'))})
        if not candidates:
            return {'status': 'configuration_error', 'code': 'no_available_route',
                    'reason': 'No available route for the alias',
                    'alias': alias_name, 'routes': route_payload}, 2
        return run_route_chain(args, path, alias_name, candidates, shell, repo, prompt,
                               interval, runtime, route_retries, retry_seconds, track=track,
                               git_check=git_check)
    available = active_models(data)
    selection = next((r for r in model_rows(data) if r['address'] == args.model), None)
    if not selection or not selection['available']:
        return {'status': 'configuration_error', 'code': 'model_not_selected',
                'reason': 'Select an available full model address',
                'available_models': available}, 2
    harness, provider, model = registry.parse_address(args.model)
    effort = args.effort if args.effort is not None else selection['default_effort']
    if effort is not None and effort not in selection['efforts']:
        raise registry.ConfigError('Unsupported effort; configured values: ' + ', '.join(selection['efforts']))
    adapter = importlib.import_module(harness)
    resolver = getattr(adapter, 'resolve', None)
    executable = resolver() if resolver else shutil.which(harness)
    if not executable:
        raise registry.ConfigError(f'Native CLI is not installed: {harness}')
    resume_session = getattr(args, 'resume_session', None)
    if resume_session is not None:
        pattern = RESUME_PATTERNS.get(harness)
        if pattern is None:
            raise registry.ConfigError(f'--resume-session is not supported for {harness}')
        if pattern.fullmatch(resume_session) is None:
            raise registry.ConfigError(f'--resume-session must use the exact native {harness} session ID format')
    agy_default_full = False
    if harness == 'agy':
        if args.shell == 'auto':
            raise registry.ConfigError('--shell auto is unavailable for agy (no native support); '
                                       'use none or full')
        if args.shell is None:
            shell, agy_default_full = 'full', True
    retries = args.capacity_retries if args.capacity_retries is not None else data['execution'].get('capacity_retries', 3)
    if retries < 0:
        raise registry.ConfigError('capacity retries must be >= 0')
    result = run_with_capacity_retries(adapter, executable, repo, provider, model, effort, args.mode,
                                       shell, prompt, args.model, interval, runtime, args.log_dir,
                                       resume_session, retries, retry_seconds, track=track,
                                       git_check=git_check, override=bool(selection.get('override')))
    result['requested'] = dict(harness=harness, provider=provider, model=model, effort=effort,
                               shell=shell)
    if agy_default_full:
        result['warnings'].append(AGY_DEFAULT_FULL_WARNING)
    reported = result['reported']
    if any(reported[k] is not None and reported[k] != result['requested'][k]
           for k in ('harness', 'provider', 'model', 'effort')):
        result.update(status='failed', stop_reason='reported_selection_mismatch')
    if result['stop_reason'] == 'quota':
        try:
            registry.block_quota(path, harness, provider, result['reason'] or 'Quota exhausted', result['quota_reset_at'])
        except (OSError, ValueError, KeyError) as exc:
            result['warnings'].append('Provider quota was not saved: ' + redact(str(exc)))
    return result, result_exit_code(result)


ASK_TEST_PROMPT = 'Reply with exactly the word: Hi'


def ask_execute(args, path):
    """One prompt, one native turn, no repository: a throwaway plain directory.

    Reuses execute() end to end with fixed semantics: review mode, shell none,
    and zero retries (a full address runs exactly once; every alias route runs
    once, capacity/quota handing off to the next route without a wait). The
    Codex native Git working-copy check is waived for the throwaway directory.
    """
    alias = args.model is not None and '/' not in args.model
    args.mode = 'review'
    # Pinned explicitly so the agy default-full exception does not apply to ask.
    args.shell = 'none'
    args.prompt_file = None
    args.status_interval_seconds = None
    args.max_runtime_seconds = None
    args.resume_session = None
    args.capacity_retries = None if alias else 0
    args.capacity_retry_seconds = None
    args.route_retries = 0 if alias else None
    repo = Path(tempfile.mkdtemp(prefix='delegator-ask-'))
    try:
        return execute(args, path, repo=repo, track=False, git_check=False)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_one(args, path, rows, address):
    """Offline preflight then one live ask turn; a query failure is a fail."""
    row = rows.get(address)
    if row is None:
        return {'address': address, 'phase': 'offline', 'ok': False, 'status': None,
                'stop_reason': None, 'reason': 'not registered', 'answer': None,
                'log_path': None, 'elapsed_seconds': None}
    if not row['available']:
        return {'address': address, 'phase': 'offline', 'ok': False, 'status': None,
                'stop_reason': None, 'reason': row['reason'], 'answer': None,
                'log_path': None, 'elapsed_seconds': None}
    effort = args.effort if args.effort is not None else row['default_effort']
    if args.effort is not None and args.effort not in row['efforts']:
        return {'address': address, 'phase': 'offline', 'ok': False, 'status': None,
                'stop_reason': None,
                'reason': 'effort ' + args.effort + ' is not configured; configured: '
                          + ', '.join(row['efforts']), 'answer': None,
                'log_path': None, 'elapsed_seconds': None}
    harness, provider, model = registry.parse_address(address)
    repo = Path(tempfile.mkdtemp(prefix='delegator-test-'))
    try:
        try:
            adapter = importlib.import_module(harness)
            resolver = getattr(adapter, 'resolve', None)
            executable = resolver() if resolver else shutil.which(harness)
            if not executable:
                raise OSError(f'native CLI is not installed: {harness}')
            adapter.command(executable, repo, provider, model, effort, 'review', 'none',
                            git_check=False, override=bool(row.get('override')))
        except (ImportError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            return {'address': address, 'phase': 'offline', 'ok': False, 'status': None,
                    'stop_reason': None, 'reason': redact(str(exc)), 'answer': None,
                    'log_path': None, 'elapsed_seconds': None}
        single = argparse.Namespace(model=address, effort=effort, prompt=ASK_TEST_PROMPT,
                                    prompt_file=None, mode='review', shell=None,
                                    log_dir=args.log_dir, status_interval_seconds=None,
                                    max_runtime_seconds=None, capacity_retries=0,
                                    capacity_retry_seconds=None, route_retries=None,
                                    resume_session=None)
        result, _ = execute(single, path, repo=repo, track=False, git_check=False)
    finally:
        shutil.rmtree(repo, ignore_errors=True)
    return {'address': address, 'phase': 'live', 'ok': result['status'] == 'completed',
            'status': result['status'], 'stop_reason': result.get('stop_reason'),
            'reason': result.get('reason'), 'answer': result.get('answer'),
            'log_path': result.get('log_path'), 'elapsed_seconds': result.get('elapsed_seconds')}


def test_execute(args, path):
    data = registry.load(path)
    rows = {r['address']: r for r in model_rows(data)}
    if args.model is None:
        addresses = sorted(address for address, row in rows.items() if row['available'])
    elif '/' not in args.model:
        addresses = registry.alias_routes(data, args.model)
        if not addresses:
            payload = {'status': 'configuration_error', 'code': 'unknown_alias',
                       'reason': f'Unknown alias: {args.model!r}',
                       'aliases': registry.alias_names(data)}
            print(json.dumps(payload, ensure_ascii=False))
            return 2
    else:
        addresses = [args.model]
    results = []
    for address in addresses:
        entry = test_one(args, path, rows, address)
        results.append(entry)
        if args.json:
            print(json.dumps(entry, ensure_ascii=False))
            continue
        detail = ' '.join((entry['reason'] or entry['answer'] or entry['stop_reason'] or 'completed').split())
        print(f"{address} | {'ok' if entry['ok'] else 'fail'} | {detail[:120]}")
    passed = sum(1 for entry in results if entry['ok'])
    if args.json:
        print(json.dumps({'command': 'test', 'passed': passed, 'total': len(results)}))
    else:
        print(f'passed {passed}/{len(results)}')
    return 0 if passed == len(results) else 1


def changed_paths_note(result):
    fresh = [p for p in result.get('changed_paths', [])
             if p not in result.get('preexisting_changed_paths', [])]
    return ' changed paths: ' + ', '.join(fresh) if fresh else ''


def run_route_chain(args, path, alias_name, candidates, shell, repo, prompt,
                    interval, runtime, route_retries, retry_seconds, track=True, git_check=True):
    """Run alias candidates in order; capacity/quota hand off to the next route.

    Each candidate gets its own per-route preparation (adapter, executable,
    shell level). A capacity failure after the candidate's route retries and a
    confirmed quota failure move to the next candidate without a wait; any
    other stop reason ends the chain. A launch preparation failure inside the
    adapter skips to the next route like the chain-level preparation failures.
    The runtime budget spans the whole call.
    """
    started = time.monotonic()
    results, routes_log, warnings = [], [], []
    blocked_providers = set()
    last = None
    index = 0
    while index < len(candidates):
        candidate = candidates[index]
        address = candidate['address']
        index += 1
        if (candidate['harness'], candidate['provider']) in blocked_providers:
            warnings.append(f'Route {address} skipped: provider quota is blocked')
            continue
        if runtime is not None and runtime - (time.monotonic() - started) <= 0:
            warnings.append(f'Route {address} skipped: the whole-call runtime budget is spent')
            break
        harness, provider, model = registry.parse_address(address)
        if harness == 'agy' and args.shell == 'auto':
            raise registry.ConfigError('--shell auto is unavailable for agy (no native support); '
                                       'use none or full')
        try:
            adapter = importlib.import_module(harness)
            resolver = getattr(adapter, 'resolve', None)
            executable = resolver() if resolver else shutil.which(harness)
            if not executable:
                raise OSError(f'native CLI is not installed: {harness}')
            effective_shell, agy_default_full = shell, False
            if harness == 'agy' and args.shell is None:
                effective_shell, agy_default_full = 'full', True
            if harness == 'zcode' and candidate['effort'] is None:
                raise ValueError('zcode requires an explicit effort; the model has no default effort')
        except (ImportError, OSError, ValueError) as exc:
            warnings.append(f'Route {address} skipped: {exc}')
            continue
        last = dict(harness=harness, provider=provider, model=model,
                    effort=candidate['effort'], shell=effective_shell)
        attempt_runtime = runtime
        if runtime is not None:
            attempt_runtime = max(runtime - (time.monotonic() - started), 0.001)
        try:
            result = run_with_capacity_retries(adapter, executable, repo, provider, model,
                                               candidate['effort'], args.mode, effective_shell,
                                               prompt, address, interval, attempt_runtime,
                                               args.log_dir, None, route_retries, retry_seconds,
                                               track=track, git_check=git_check,
                                               override=bool(candidate.get('override')))
        except RoutePreparationError as exc:
            warnings.append(f'Route {address} skipped: {redact(str(exc))}')
            continue
        results.append(result)
        routes_log.extend([address] * result.get('attempts', 1))
        if agy_default_full:
            warnings.append(AGY_DEFAULT_FULL_WARNING)
        stop = result['stop_reason']
        if stop not in ('capacity', 'quota'):
            break
        label = 'capacity' if stop == 'capacity' else 'quota'
        if index < len(candidates):
            status(sys.stderr, f'route | {address} failed ({label}) | next: {candidates[index]["address"]}')
            warnings.append(f'Route {address} failed ({label}); continuing with the next route'
                            + changed_paths_note(result))
        if stop == 'quota':
            blocked_providers.add((harness, provider))
            try:
                registry.block_quota(path, harness, provider, result['reason'] or 'Quota exhausted',
                                     result.get('quota_reset_at'))
            except (OSError, ValueError, KeyError) as exc:
                warnings.append('Provider quota was not saved: ' + redact(str(exc)))
    if not results:
        if any(SKIPPED_ENVIRONMENT_MARKER in w.lower() for w in warnings):
            reason, code = SKIPPED_ENVIRONMENT_REASON, 'environment_blocked'
        else:
            reason, code = 'No route of the alias was run', 'no_route_ran'
        payload = {'status': 'configuration_error', 'code': code, 'reason': reason,
                   'alias': alias_name, 'warnings': warnings}
        return payload, 2
    final, first = results[-1], results[0]
    if len(results) > 1:
        final['attempts'] = sum(r.get('attempts', 1) for r in results)
        final['log_paths'] = [p for r in results for p in r.get('log_paths', [])]
        final['preexisting_changed_paths'] = first['preexisting_changed_paths']
        usage = {}
        for r in results:
            for key, value in (r.get('usage') or {}).items():
                if type(value) in (int, float):
                    usage[key] = usage.get(key, 0) + value
        final['usage'] = usage or None
        final['elapsed_seconds'] = round(time.monotonic() - started, 3)
        final['resumed'] = False
    final['attempt_routes'] = routes_log
    final['alias'] = alias_name
    final['requested'] = dict(last)
    warnings.extend(final.get('warnings') or [])
    final['warnings'] = warnings
    reported = final['reported']
    if any(reported[k] is not None and reported[k] != final['requested'][k]
           for k in ('harness', 'provider', 'model', 'effort')):
        final.update(status='failed', stop_reason='reported_selection_mismatch')
    return final, result_exit_code(final)


def parser():
    command_help = {
        'list': ('List registry nodes',
                 'List registry nodes with enabled/available state, quota, efforts, and default effort.',
                 'examples:\n'
                 '  delegator list\n'
                 '  delegator list --active\n'
                 '  delegator list opencode/openrouter --json'),
        'add': ('Add a harness/provider/model entry',
                'Add an explicit harness/provider/model entry, creating enabled missing parents. '
                '--override registers a zcode model outside the installed builtin table: the '
                'native catalog check is skipped, the entry is marked, and exec pushes the model '
                'into the plan provider list.',
                'examples:\n'
                '  delegator add codex/openai/gpt-6-luna --efforts low medium high --effort low\n'
                '  delegator add opencode/openrouter/deepseek/deepseek-v4.1-flash\n'
                '  delegator add zcode/account:zai-start-plan/GLM-5.3 --override'),
        'on': ('Enable a node',
               'Enable a node and clear quota for the affected provider(s), preserving descendants.',
               'examples:\n'
               '  delegator on opencode/openrouter\n'
               '  delegator on opencode/openrouter/deepseek/deepseek-v4.1-flash'),
        'off': ('Disable a node',
                'Disable a node, preserving its descendants.',
                'examples:\n'
                '  delegator off openrouter'),
        'delete': ('Delete registry records',
                   'Delete registry records for a node; nonempty nodes require --recursive.',
                   'examples:\n'
                   '  delegator delete opencode/openrouter --recursive --dry-run\n'
                   '  delegator delete opencode/openrouter --recursive --yes'),
        'sync': ('Compare the registry with harness catalogs',
                 'Show NEW, STALE, and HIDDEN entries from local harness catalogs; '
                 'apply an explicit diff with --add, --prune, or --hide. OpenCode '
                 'candidates come from the desktop app state when it exists '
                 '(models with visibility "show"; TUI favorites otherwise), and '
                 'providers that cannot load (disabled, outside enabled_providers, '
                 'or built-in without a key) are skipped and marked unavailable.',
                 'examples:\n'
                 '  delegator sync\n'
                 '  delegator sync --add 1,3\n'
                 '  delegator sync --prune 2 --yes\n'
                 '  delegator sync --hide 4,zcode/plan\n'
                 '  delegator sync --add all --yes\n'
                 '  delegator sync --hide all --yes'),
        'hide': ('Hide models or providers from sync',
                 'Hide models or providers from sync; registered addresses must be deleted first. '
                 'Addresses are literal prefixes; no glob patterns.',
                 'examples:\n'
                 '  delegator hide codex/openai/gpt-5.6-terra\n'
                 '  delegator hide opencode/openrouter'),
        'unhide': ('Stop hiding an address',
                   'Stop hiding an address: remove an exact hidden entry; a broader prefix must be unhidden itself.',
                   'examples:\n'
                   '  delegator unhide codex/openai/gpt-5.6-terra\n'
                   '  delegator unhide opencode/openrouter'),
        'hidden': ('List hidden addresses',
                   'List hidden addresses stored in the registry.',
                   'examples:\n'
                   '  delegator hidden\n'
                   '  delegator hidden --json'),
        'alias': ('Manage model aliases with ordered routes',
                  'Aliases map a short name to ordered full model routes; exec tries them '
                  'in order on capacity/quota failures. Routes must be registered models.',
                  'examples:\n'
                  '  delegator alias add glm-flash zcode/account:zai-start-plan/GLM-5.3-Flash,'
                  'zcode/account:zai-individual-coding-plan/GLM-5.3-Flash\n'
                  '  delegator alias add deepseek-flash opencode/opencode/deepseek/deepseek-v4.1-flash '
                  'opencode/commandcode/deepseek/deepseek-v4.1-flash\n'
                  '  delegator alias list\n'
                  '  delegator alias remove deepseek-flash'),
        'exec': ('Run one native worker',
                 'Start exactly one native worker for an available full model address; '
                 'a classified capacity failure is retried with progressive backoff. '
                 'Shell commands follow --shell none|auto|full: none by default for review, '
                 'auto by default for implementation. Antigravity defaults to full '
                 '(--dangerously-skip-permissions) in both modes: a headless agy run is '
                 'meaningful only at full.',
                 'examples:\n'
                 '  delegator exec -m codex/openai/gpt-6-luna -e low --mode implementation '
                 '--repo D:\\prj\\example --prompt-file .tmp/issue-42/task.md\n'
                 '  delegator exec -m codex/openai/gpt-6-luna -e low --mode implementation '
                 '--shell full --repo D:\\prj\\example -p \'Run the full cleanup as assigned.\'\n'
                 '  delegator exec -m opencode/commandcode/deepseek/deepseek-v4.1-flash -e high '
                 "--mode review --repo D:\\prj\\example -p 'Review the specified diff; "
                 "report concrete defects only.'\n"
                 '  delegator exec -m zcode/account:zai-individual-coding-plan/GLM-5.3 -e high '
                 "--mode review --repo D:\\prj\\example -p 'Review the specified diff.'\n"
                 '  delegator exec -m agy/gemini/gemini-3.8-flash-high -e high '
                 "--mode review --repo D:\\prj\\example -p 'Review the specified diff; "
                 "report concrete defects only.'\n"
                 '  delegator exec -m claude/anthropic/claude-sonnet-5-5 -e high '
                 "--mode review --repo D:\\prj\\example -p 'Review the specified diff; "
                 "report concrete defects only.'\n"
                  '  delegator exec -m deepseek-flash -e high '
                  "--mode review --repo D:\\prj\\example -p 'Review the specified diff; "
                  "report concrete defects only.'"),
        'ask': ('Ask one prompt without a repository',
                'One native turn for a single prompt in a fresh temporary directory '
                'outside Git, removed after the run. Review-only with shell none; no '
                'capacity retries: a full address runs exactly once, an alias runs each '
                'route once and hands off to the next route on capacity/quota without a '
                'wait. Prints the answer as plain text (a failure note goes to stderr); '
                '--json prints the full exec JSON result with empty changed paths.',
                'examples:\n'
                '  delegator ask -m zcode/account:zai-individual-coding-plan/GLM-5.3-Flash -p \'Hi\'\n'
                '  delegator ask -m glm-flash -e high -p \'Summarize this in one line: ...\'\n'
                '  delegator ask -m deepseek-flash -p \'Hi\' --json'),
        'test': ('Smoke-test configured models',
                 'Offline validation of each model (registry availability, native CLI '
                 'resolution, launch preparation — the zcode plan key and efforts are '
                 'checked here) followed by one live ask turn with a fixed short prompt '
                 'and the model default effort. Without -m tests every available model; '
                 'an alias expands to its routes; an explicitly named unavailable model '
                 'fails offline with its reason. A live quota/capacity/auth failure is a '
                 'fail, not a configuration error.',
                 'examples:\n'
                 '  delegator test\n'
                 '  delegator test -m glm-flash\n'
                 '  delegator test -m zcode/account:zai-individual-coding-plan/GLM-5.3-Flash\n'
                 '  delegator test --json'),
    }
    root = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='examples:\n'
               '  delegator add codex/openai/gpt-6-luna --efforts low medium high --effort low\n'
               '  delegator list --active\n'
               '  delegator on opencode/openrouter\n'
               '  delegator off openrouter\n'
               '  delegator delete opencode/openrouter --recursive --dry-run\n'
               '  delegator sync --add 1,3\n'
               '  delegator hide codex/openai/gpt-5.6-terra\n'
               '  delegator alias add glm-flash zcode/account:zai-start-plan/GLM-5.3-Flash,'
               'zcode/account:zai-individual-coding-plan/GLM-5.3-Flash\n'
               '  delegator quota --format html --out D:\\tools\\quota.html\n'
               '  delegator exec -m codex/openai/gpt-6-luna -e low --mode implementation '
               '--repo D:\\prj\\example --prompt-file .tmp/issue-42/task.md\n'
               "  delegator ask -m glm-flash -p 'Hi'\n"
               '  delegator test -m glm-flash')
    root.add_argument('--config', help='Registry path (overrides DELEGATOR_CONFIG)')
    root.add_argument('--json', action='store_true', help='Machine-readable management output')
    commands = root.add_subparsers(dest='command', required=True)
    for name in ('list', 'add', 'on', 'off', 'delete', 'sync', 'hide', 'unhide', 'hidden',
                 'alias', 'exec', 'ask', 'test'):
        short, description, epilog = command_help[name]
        sub = commands.add_parser(name, help=short, description=description, epilog=epilog,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
        sub.add_argument('--config', default=argparse.SUPPRESS,
                         help='Registry path (overrides DELEGATOR_CONFIG)')
        sub.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                         help='Machine-readable management output')
        if name == 'alias':
            operation = sub.add_subparsers(dest='alias_command', required=True)
            alias_add = operation.add_parser(
                'add', help='Create or replace an alias',
                description='Create or replace an alias with ordered full model routes; '
                            'every route must be a registered model.',
                epilog='examples:\n'
                       '  delegator alias add glm-flash zcode/account:zai-start-plan/GLM-5.3-Flash,'
                       'zcode/account:zai-individual-coding-plan/GLM-5.3-Flash\n'
                       '  delegator alias add deepseek-flash opencode/opencode/deepseek/deepseek-v4.1-flash '
                       'opencode/commandcode/deepseek/deepseek-v4.1-flash',
                formatter_class=argparse.RawDescriptionHelpFormatter)
            alias_add.add_argument('name', help='Alias name without "/"')
            # nargs='+': PowerShell turns an unquoted `a,b` into an argument
            # array; the tokens are joined back into one comma-separated spec.
            alias_add.add_argument('routes', nargs='+',
                                   help='Ordered full model addresses (comma- or space-separated)')
            alias_remove = operation.add_parser('remove', help='Delete an alias')
            alias_remove.add_argument('name', help='Alias name to delete')
            alias_list = operation.add_parser('list', help='List aliases with their ordered routes')
            for alias_sub in (alias_add, alias_remove, alias_list):
                alias_sub.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                                       help='Machine-readable management output')
        elif name == 'exec':
            sub.add_argument('-m', '--model',
                             help='Exact harness/provider/model address, or an alias name without "/"')
            sub.add_argument('-e', '--effort',
                             help='Effort name; defaults to the model entry default effort')
            prompt = sub.add_mutually_exclusive_group()
            prompt.add_argument('-p', '--prompt', help='Assignment text')
            prompt.add_argument('--prompt-file', help='Read the assignment from a UTF-8 file')
            sub.add_argument('--repo', default='.',
                             help='Git working-copy root (default: current directory)')
            sub.add_argument('--mode', choices=['review', 'implementation'], default='review',
                             help='review (default; read-only where the harness supports it) '
                                  'or implementation (native edits)')
            sub.add_argument('--shell', choices=['none', 'auto', 'full'],
                             help='Shell command level: none (no shell commands), auto (native '
                                  'guard: safe commands run, destructive ones are denied), or '
                                  'full (no protection). Default: none for review, auto for '
                                  'implementation; agy defaults to full '
                                  '(--dangerously-skip-permissions) in both modes and an '
                                  'explicit auto fails')
            sub.add_argument('--status-interval-seconds', type=float,
                             help='Seconds between status messages on stderr (0 disables)')
            sub.add_argument('--max-runtime-seconds', type=float,
                             help='Optional limit for the whole call in seconds')
            sub.add_argument('--capacity-retries', type=int,
                             help='Retries after a classified capacity failure of a full model '
                                  'address (default 3; config execution.capacity_retries); '
                                  'rejected for an alias')
            sub.add_argument('--capacity-retry-seconds', type=float,
                             help='First capacity retry wait in seconds; doubles each retry '
                                  '(default 30; config execution.capacity_retry_seconds)')
            sub.add_argument('--route-retries', type=int,
                             help='For an alias: retries per route after a classified capacity '
                                  'failure before handing off to the next route (default 1; '
                                  'config execution.route_retries); rejected for a full address')
            sub.add_argument('--log-dir', help='Native log directory outside every Git repository; '
                             'when the system TEMP/TMP falls inside Git, a permanent user directory '
                             'is used (%%LOCALAPPDATA%%\\delegator or the XDG state directory)')
            sub.add_argument('--result-file',
                             help='Write the final JSON result to this file atomically before '
                                  'stdout; the path must be outside every Git repository and '
                                  'must not exist')
            sub.add_argument('--resume-session',
                             help='Continue the exact native session ID with a delta prompt')
        elif name in ('ask', 'test'):
            sub.add_argument('-m', '--model',
                             help='Exact harness/provider/model address, or an alias name '
                                  'without "/" (test without -m tests every available model)')
            sub.add_argument('-e', '--effort',
                             help='Effort name; defaults to the model entry default effort')
            if name == 'ask':
                sub.add_argument('-p', '--prompt', help='Prompt text')
            sub.add_argument('--log-dir', help='Native log directory outside every Git repository; '
                             'when the system TEMP/TMP falls inside Git, a permanent user directory '
                             'is used (%%LOCALAPPDATA%%\\delegator or the XDG state directory)')
        elif name == 'sync':
            operation = sub.add_mutually_exclusive_group()
            # nargs='+': PowerShell turns an unquoted `1,3` into an argument
            # array; the tokens are joined back into one comma-separated spec.
            operation.add_argument('--add', metavar='SPEC', nargs='+',
                                   help='NEW indices, model addresses, or all')
            operation.add_argument('--prune', metavar='SPEC', nargs='+',
                                   help='STALE indices, stale addresses, or all')
            operation.add_argument('--hide', metavar='SPEC', nargs='+',
                                   help='NEW indices, NEW model or provider addresses, or all')
            sub.add_argument('--yes', action='store_true', help='Confirm all')
        elif name in ('hide', 'unhide'):
            sub.add_argument('address', nargs='+',
                             help='Harness/provider or model address to '
                                  + ('hide' if name == 'hide' else 'unhide'))
        elif name == 'hidden':
            pass
        else:
            if name == 'list':
                help_text = ('Optional full address or whole-segment suffix; limits the listing '
                             'to matching nodes')
            elif name == 'add':
                help_text = 'Full harness/provider/model address to add'
            elif name in ('on', 'off'):
                help_text = ('Harness, provider, or model address to '
                             + ('enable' if name == 'on' else 'disable'))
            else:
                help_text = 'Harness, provider, or model address to delete'
            sub.add_argument('address', nargs='?' if name == 'list' else None, help=help_text)
            if name == 'list':
                sub.add_argument('--active', action='store_true', help='Show only available nodes')
            if name == 'add':
                sub.add_argument('--efforts', nargs='*',
                                 help='Explicit effort names for this model (space separated)')
                sub.add_argument('--effort', help='Default effort; must be one of --efforts')
                sub.add_argument('--override', action='store_true',
                                 help='Register a zcode model outside the installed builtin table: '
                                      'skips the native catalog check, marks the entry, and exec '
                                      'pushes the model into the plan provider list')
            if name == 'delete':
                sub.add_argument('--recursive', action='store_true',
                                 help='Delete the node and all descendants')
                sub.add_argument('--yes', action='store_true',
                                 help='Confirm multiple or recursive deletion')
                sub.add_argument('--dry-run', action='store_true',
                                 help='Show planned deletions without writing the registry')
    quota = commands.add_parser(
        'quota', help='Show harness account quota windows',
        description='Collect read-only plan-quota windows for codex, zcode, claude, agy, '
                    'openrouter, commandcode, and opencode-go; render text, a JSON snapshot, or a '
                    'static HTML dashboard. Sources without local credentials are skipped '
                    'automatically. No LLM calls; secrets never enter the output.',
        epilog='snapshot: {version, generated_at, sources: [{source, status: ok | error, '
               'fetched_at, error, windows: [{label, used_percent, used, limit, unit, '
               'window_minutes, resets_at}], extras}]}\n'
               'unit is usd | credits | tokens | requests | null; windows of different sources are not '
               'comparable; error is a classified reason (auth_expired, timeout, network, '
               'parse_error, config, unexpected_error) and never contains secrets\n'
               'without --only, sources without local credentials (missing CLI, key, or token '
               'file) are skipped automatically; an explicit --only always collects the named '
               'sources\n'
               'exit codes: 0 collected (source errors are data), 2 CLI/configuration error\n'
               'examples:\n'
               '  delegator quota\n'
               '  delegator quota --format json\n'
               '  delegator quota --format html --out D:\\tools\\quota.html\n'
               '  delegator quota --only codex,zcode --timeout 20',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    quota.add_argument('--format', choices=['text', 'json', 'html'], default='text',
                       help='text (default), json snapshot on stdout, or html dashboard file')
    quota.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                       help='Alias of --format json')
    quota.add_argument('--out', help='html target path (default: ~/.agents/quota-dashboard.html)')
    quota.add_argument('--only', nargs='+', help='comma-separated subset of sources: '
                                                + ', '.join(('codex', 'zcode', 'claude', 'agy',
                                                             'openrouter', 'commandcode',
                                                             'opencode-go'))
                                                + '; skipped-source auto-detection is bypassed '
                                                  'for named sources (a quoted spec and an '
                                                  'unquoted PowerShell array are equivalent)')
    quota.add_argument('--timeout', type=float, default=30,
                       help='wall-clock budget per source in seconds (default 30)')
    return root


def write_result_file(args, result):
    """Deliver the final exec result to --result-file before the stdout print.

    The path was validated before launch; a path that exists by now or lies
    inside a Git repository is never written or overwritten. A failed write
    is a warning without a status change: a missing result file means the
    outcome is unknown, not that a repeat is allowed.
    """
    if args.command != 'exec' or getattr(args, 'result_file', None) is None:
        return
    target = Path(args.result_file).expanduser()
    if target.exists() or _inside_git_repo(target):
        return
    result['result_file'] = str(target)
    tmp = target.with_name(target.name + '.tmp')
    try:
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, target)
    except OSError as exc:
        result.setdefault('warnings', []).append('Result file write failed: ' + str(exc))


def main(argv=None):
    args_list = sys.argv[1:] if argv is None else list(argv)
    if not args_list:
        parser().print_help()
        return 2
    args = parser().parse_args(args_list)
    if args.command == 'quota':
        import quota
        return quota.run_from_namespace(args)
    path = registry.resolve_path(args.config)
    try:
        if args.command in ('sync', 'hide', 'unhide', 'hidden'):
            payload, text = registry_command(args, path)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                for line in text:
                    print(line)
            for warning in payload.get('warnings', []):
                print(warning, file=sys.stderr)
            return 0
        if args.command == 'alias':
            data = registry.load(path)
            payload, text = alias_command(args, data, path)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                for line in text:
                    print(line)
            return 0
        if args.command in ('exec', 'ask'):
            result, code = execute(args, path) if args.command == 'exec' else ask_execute(args, path)
        elif args.command == 'test':
            return test_execute(args, path)
        else:
            result, code = management(args, path), 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result, code = {'status': 'configuration_error', 'config': str(path),
                        'reason': redact(str(exc))}, 2
        if getattr(exc, 'code', None) is not None:
            result['code'] = exc.code
    write_result_file(args, result)
    if args.command == 'ask' and not args.json:
        # Human-readable ask: the full answer as plain text, a one-line
        # failure note on stderr; --json keeps the full exec result contract.
        answer = result.get('answer')
        if result.get('answer_truncated') and result.get('answer_path'):
            try:
                answer = Path(result['answer_path']).read_text(encoding='utf-8')
            except OSError:
                print('Full answer file unreadable: ' + str(result['answer_path']), file=sys.stderr)
        if answer:
            print(answer)
        if result.get('status') not in ('completed', 'needs_input'):
            parts = ['ask failed']
            if result.get('stop_reason'):
                parts.append(result['stop_reason'])
            if result.get('reason'):
                parts.append(result['reason'])
            print(': '.join(parts), file=sys.stderr)
            for row in result.get('available_models', []):
                print('  available: ' + row['address'], file=sys.stderr)
        return code
    if args.command in ('exec', 'ask') or args.json or code:
        print(json.dumps(result, ensure_ascii=False))
    elif 'nodes' in result:
        for row in result['nodes']:
            override = ' | override' if row.get('override') else ''
            print(f"{row['address']} | enabled={str(row['enabled']).lower()} | available={str(row['available']).lower()}"
                  f" | {row.get('reason') or '-'} | efforts={','.join(row.get('efforts', [])) or '-'}"
                  f" | default_effort={row.get('default_effort') or '-'}{override}")
        if not result['nodes']:
            print('Registry is empty. Add an explicit harness/provider/model address.')
    else:
        print(args.command + ': ' + ', '.join(result['addresses']))
        if result['quota_cleared']:
            print('Quota cleared: ' + ', '.join(result['quota_cleared']))
        for warning in result['warnings']:
            print(warning, file=sys.stderr)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
