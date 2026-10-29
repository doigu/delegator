import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    'delegator_migrate', SCRIPTS / 'migrate_default_providers.py')
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)
registry = migrate.registry


def build_registry(path):
    data = registry.load(path)
    registry.add(data, 'codex/default/gpt-x', ['low'], 'low')
    registry.add(data, 'codex/default/neighbor')
    provider = data['harnesses']['codex']['providers']['default']
    provider['quota'].update(blocked=True, reset_at='2026-10-22T10:00:00Z',
                             reason='quota exceeded')
    provider['models']['neighbor']['enabled'] = False
    registry.add(data, 'agy/default/gemini-m', ['high'], 'high')
    registry.add(data, 'opencode/p/m')
    registry.hidden_add(data, 'codex/default/hidden-m')
    registry.save(path, data)
    return data


def test_migration_renames_providers_and_preserves_state(tmp_path):
    path = tmp_path / 'registry.json'
    before = build_registry(path)
    assert migrate.main(['--config', str(path)]) == 0
    after = registry.load(path)
    codex = after['harnesses']['codex']['providers']['openai']
    assert codex['quota'] == {'blocked': True, 'reset_at': '2026-10-22T10:00:00Z',
                              'reason': 'quota exceeded'}
    assert codex['models']['gpt-x']['efforts'] == ['low']
    assert codex['models']['gpt-x']['default_effort'] == 'low'
    assert codex['models']['neighbor']['enabled'] is False
    agy = after['harnesses']['agy']['providers']['gemini']
    assert agy['models']['gemini-m']['efforts'] == ['high']
    assert 'default' not in after['harnesses']['codex']['providers']
    assert 'default' not in after['harnesses']['agy']['providers']
    assert 'p' in after['harnesses']['opencode']['providers']
    assert after['hidden'] == ['codex/openai/hidden-m']
    assert after['execution'] == before['execution']
    backups = list(tmp_path.glob('*.backup-*'))
    assert len(backups) == 1
    assert registry.load(backups[0]) == before
    assert migrate.main(['--config', str(path)]) == 0
    assert list(tmp_path.glob('*.backup-*')) == backups


def test_migration_rewrites_only_exact_hidden_prefixes(tmp_path):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    data['hidden'] = ['agy/default', 'codex/defaults/x', 'opencode/default/m',
                      'codex/default/deep/model']
    registry.save(path, data)
    assert migrate.main(['--config', str(path)]) == 0
    assert registry.load(path)['hidden'] == ['agy/gemini', 'codex/defaults/x',
                                             'opencode/default/m',
                                             'codex/openai/deep/model']


def test_conflicting_old_and_new_provider_refuses_writes(tmp_path):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'codex/default/legacy')
    registry.add(data, 'codex/openai/fresh')
    registry.save(path, data)
    before = path.read_bytes()
    assert migrate.main(['--config', str(path)]) == 1
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.backup-*'))


def test_conflicting_hidden_entry_refuses_writes(tmp_path):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    data['hidden'] = ['codex/default/a', 'codex/openai/a']
    registry.save(path, data)
    before = path.read_bytes()
    assert migrate.main(['--config', str(path)]) == 1
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.backup-*'))


def test_missing_registry_and_empty_registry_are_noops(tmp_path):
    absent = tmp_path / 'absent.json'
    assert migrate.main(['--config', str(absent)]) == 0
    assert not absent.exists()
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'opencode/p/m')
    registry.save(path, data)
    before = path.read_bytes()
    assert migrate.main(['--config', str(path)]) == 0
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.backup-*'))
