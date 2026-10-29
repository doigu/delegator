#!/usr/bin/env python3
"""One-off migration of delegator registry provider slugs.

Renames the provider nodes codex/default -> codex/openai and agy/default ->
agy/gemini, including matching `hidden` entries, and preserves everything else
(models, efforts, enabled flags, quota, execution settings, other harnesses).
Writes a backup next to the registry first; conflicting configs are refused
without writing. Safe to rerun: a registry without these providers is a no-op.

Usage:
  python -X utf8 -B migrate_default_providers.py [--config <registry.json>]

The registry path follows the usual resolution: --config, then
DELEGATOR_CONFIG, then ~/.agents/delegator.json. Run it while no delegator
exec is in progress; a concurrent run could lose its quota write.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry

RENAMES = {'codex': 'openai', 'agy': 'gemini'}


class MigrationError(Exception):
    """Ambiguous registry state; nothing is written."""


def _renamed_hidden(entries):
    result = []
    for entry in entries:
        updated = entry
        for harness, provider in RENAMES.items():
            prefix = harness + '/default'
            if entry == prefix or entry.startswith(prefix + '/'):
                updated = harness + '/' + provider + entry[len(prefix):]
                break
        if updated in result:
            raise MigrationError(f'hidden entry {entry!r} would duplicate {updated!r}; '
                                 'resolve the hidden list manually')
        result.append(updated)
    return result


def migrate(data):
    """Return (migrated data, renamed provider addresses, rewritten hidden entries)."""
    renamed_nodes, rewritten_hidden = [], []
    for harness, provider in RENAMES.items():
        providers = data.get('harnesses', {}).get(harness, {}).get('providers', {})
        if 'default' not in providers:
            continue
        if provider in providers:
            raise MigrationError(f'harnesses.{harness}.providers has both default and {provider}; '
                                 'resolve the conflict manually')
        # Rebuild the mapping in place to preserve key order.
        data['harnesses'][harness]['providers'] = {
            provider if key == 'default' else key: value for key, value in providers.items()
        }
        renamed_nodes.append(f'{harness}/default')
    if data.get('hidden'):
        updated = _renamed_hidden(data['hidden'])
        rewritten_hidden = [new for old, new in zip(data['hidden'], updated) if old != new]
        if rewritten_hidden:
            data['hidden'] = updated
    return data, renamed_nodes, rewritten_hidden


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Rename delegator registry providers codex/default -> codex/openai and '
                    'agy/default -> agy/gemini; backs up the registry first and refuses '
                    'conflicts without writing.')
    parser.add_argument('--config', help='Registry path (overrides DELEGATOR_CONFIG)')
    args = parser.parse_args(argv)
    path = registry.resolve_path(args.config)
    if not path.is_file():
        print(f'nothing to migrate: {path} does not exist')
        return 0
    try:
        data = registry.load(path)
        data, renamed_nodes, rewritten_hidden = migrate(data)
        if not renamed_nodes and not rewritten_hidden:
            print(f'nothing to migrate in {path}')
            return 0
        registry._validate(data)
    except (registry.ConfigError, MigrationError) as exc:
        print(f'migration refused, no changes written: {exc}', file=sys.stderr)
        return 1
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = path.with_name(path.name + '.backup-' + stamp)
    if backup.exists():
        print(f'migration refused: backup {backup} already exists', file=sys.stderr)
        return 1
    try:
        backup.write_bytes(path.read_bytes())
    except OSError as exc:
        print(f'migration failed before writing: {exc}', file=sys.stderr)
        return 1
    try:
        registry.save(path, data)
    except (OSError, registry.ConfigError) as exc:
        print(f'migration failed after backup {backup}: {exc}', file=sys.stderr)
        return 1
    print(f'backup: {backup}')
    if renamed_nodes:
        print('renamed providers: ' + ', '.join(renamed_nodes))
    if rewritten_hidden:
        print('rewritten hidden: ' + ', '.join(rewritten_hidden))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
