import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
cli = importlib.import_module('delegator')
registry = cli.registry
native = importlib.import_module('native')
runner = importlib.import_module('runner')
codex = importlib.import_module('codex')
opencode = importlib.import_module('opencode')
agy = importlib.import_module('agy')
zcode = importlib.import_module('zcode')
processes = importlib.import_module('processes')


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    subprocess.run(['git', 'init', str(path)], check=True, capture_output=True)
    return path


@pytest.mark.parametrize('adapter,provider,model', [(codex, 'openai', 'gpt-6-luna'),
                                                 (opencode, 'openrouter', 'minimax/model:free')])
def test_native_argv_and_permissions(adapter, provider, model, tmp_path):
    argv, env = adapter.command('worker.exe', tmp_path, provider, model, 'high', 'review', 'none')
    assert '--auto' not in argv
    assert not any('bypass' in x for x in argv)
    if adapter is codex:
        assert argv == ['worker.exe', 'exec', '--json', '-C', str(tmp_path), '-s', 'read-only',
                        '-m', model, '-c', 'model_provider="openai"',
                        '-c', 'model_reasoning_effort="high"', '-']
        for legacy in ('default', 'ignored-provider'):
            with pytest.raises(ValueError):
                adapter.command('worker.exe', tmp_path, legacy, model, None, 'review', 'none')
    else:
        assert argv[argv.index('--model') + 1] == 'openrouter/minimax/model:free'
        permissions = json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']
        assert permissions['*'] == 'deny' and permissions['read'] == 'allow'
        _, env = adapter.command('worker.exe', tmp_path, provider, model, None,
                                 'implementation', 'none')
        permissions = json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']
        assert (permissions['edit'] == 'allow' and permissions['*'] == 'deny'
                and permissions['todowrite'] == 'allow')


def test_codex_shell_levels_argv(tmp_path):
    argv, _ = codex.command('codex.exe', tmp_path, 'openai', 'gpt-6-luna', None,
                            'implementation', 'auto')
    assert argv[argv.index('-s') + 1] == 'workspace-write'
    assert 'read-only' not in argv
    argv, _ = codex.command('codex.exe', tmp_path, 'openai', 'gpt-6-luna', None,
                            'implementation', 'full')
    assert '--approve-for-me' in argv
    assert '-s' not in argv and '--dangerously-bypass-approvals-and-sandbox' not in argv
    # The sandbox tracks --shell only; the mode does not change it.
    argv, _ = codex.command('codex.exe', tmp_path, 'openai', 'gpt-6-luna', None,
                            'review', 'full')
    assert '--approve-for-me' in argv
    with pytest.raises(ValueError):
        codex.command('codex.exe', tmp_path, 'openai', 'gpt-6-luna', None, 'review', 'chatty')


def test_codex_resume_shell_levels_argv(tmp_path):
    session = '12345678-1234-1234-1234-1234567890ab'
    argv, _ = codex.command('codex.exe', tmp_path, 'openai', 'gpt-6-luna', None,
                            'implementation', 'full', session)
    # exec resume rejects --approve-for-me; full passes its -c equivalent.
    assert argv[-7:] == ['-c', 'sandbox_mode="workspace-write"',
                         '-c', 'approvals_reviewer="auto_review"',
                         '-c', 'approval_policy="on-request"', '-']
    assert '--approve-for-me' not in argv and 'windows.sandbox' not in argv
    argv, _ = codex.command('codex.exe', tmp_path, 'openai', 'gpt-6-luna', None,
                            'implementation', 'auto', session)
    assert 'sandbox_workspace_write.writable_roots=["' + tmp_path.as_posix() + '"]' in argv


def test_opencode_shell_permission_maps(tmp_path):
    for mode, edit in (('review', None), ('implementation', 'allow')):
        _, env = opencode.command('opencode', tmp_path, 'p', 'm', None, mode, 'none')
        permissions = json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']
        assert permissions['*'] == 'deny' and permissions.get('edit') == edit
        assert permissions.get('todowrite') == ('allow' if mode == 'implementation' else None)
        assert 'bash' not in permissions
        _, env = opencode.command('opencode', tmp_path, 'p', 'm', None, mode, 'auto')
        permissions = json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']
        assert permissions['bash'] == {'*': 'allow', 'git push*': 'deny', 'git clean*': 'deny',
                                       'format *': 'deny', 'format.com*': 'deny',
                                       'Format-Volume*': 'deny', 'shutdown*': 'deny'}
        assert permissions['external_directory'] == 'deny'
        assert permissions.get('edit') == edit and permissions['*'] == 'deny'
        # The deny patterns must follow the broad allow: last match wins.
        assert list(permissions['bash']) == ['*'] + list(opencode.SHELL_DENY_PATTERNS)
        _, env = opencode.command('opencode', tmp_path, 'p', 'm', None, mode, 'full')
        assert json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission'] == {'*': 'allow'}


def test_opencode_deny_patterns_do_not_match_format_table():
    # opencode applies the rules to command text case-insensitively (TASK-118
    # caught `Format-Table` with lowercase `format*`), so the narrowed list
    # relies on the space, dot, or `-` after `format`. This documents the
    # expected prefix behavior; live rule semantics are the acceptance run.
    def denied(command):
        return any(command.lower().startswith(pattern[:-1].lower())
                   for pattern in opencode.SHELL_DENY_PATTERNS)

    assert denied('format C:') and denied('format.com X:')
    assert denied('Format-Volume -DriveLetter C')
    assert not denied('Format-Table Name,Id')
    assert not denied('Get-Process | Format-Table Name,Id')


def test_opencode_headless_permissions_never_ask(tmp_path):
    # Headless OpenCode auto-rejects `ask` and ends the turn, so none/auto must
    # use only allow/deny and hide unlisted tools.
    for mode in ('review', 'implementation'):
        for shell in ('none', 'auto'):
            _, env = opencode.command('opencode', tmp_path, 'p', 'm', None, mode, shell)
            permissions = json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']
            actions = [v for v in permissions.values() if isinstance(v, str)]
            for rule in (v for v in permissions.values() if isinstance(v, dict)):
                actions += list(rule.values())
            assert 'ask' not in actions


@pytest.mark.parametrize('adapter,event', [
    (codex, {'type': 'error', 'message': "You've hit your usage limit. Try again in 5 minutes."}),
    (opencode, {'type': 'error', 'error': {'name': 'APIError', 'data': {'message': 'insufficient_quota'}}}),
])
def test_quota_only_native_errors(adapter, event):
    summary = native.Summary()
    summary.consume(adapter, 'stdout', json.dumps(event))
    assert summary.reason == 'quota'
    for event in [
        {'type': 'text', 'part': {'text': 'insufficient_quota'}},
        {'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'status': 'completed', 'output': 'insufficient_quota'}}},
        {'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': "You've hit your usage limit"}},
        {'type': 'error', 'error': {'message': 'HTTP 429 Too Many Requests'}},
    ]:
        summary = native.Summary()
        summary.consume(adapter, 'stdout', json.dumps(event))
        assert summary.reason != 'quota'


def test_opencode_tool_use_marks_tools_observed():
    summary = native.Summary()
    summary.consume(opencode, 'stdout', json.dumps({
        'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'status': 'running'}}}))
    assert summary.tools_observed is True
    assert summary.last_tool == 'bash'
    assert summary.completed_tools == 0


@pytest.mark.parametrize('marker', ['quota exceeded', 'insufficient quota',
                                    'insufficient balance', 'balance depleted'])
def test_ported_quota_markers_in_native_error_channel(marker):
    for adapter, event in [
        (codex, {'type': 'error', 'message': marker}),
        (opencode, {'type': 'error', 'error': {'message': marker}}),
    ]:
        summary = native.Summary()
        summary.consume(adapter, 'stdout', json.dumps(event))
        assert summary.reason == 'quota'


def test_resource_exhausted_requires_explicit_quota_context():
    for marker in ('RESOURCE_EXHAUSTED', 'RESOURCE_EXHAUSTED: rate limit exceeded'):
        summary = native.Summary()
        summary.consume(opencode, 'stdout', json.dumps({'type': 'error', 'error': {'message': marker}}))
        assert summary.reason == 'native_error'
    summary = native.Summary()
    summary.consume(opencode, 'stdout', json.dumps({
        'type': 'error', 'error': {'message': 'RESOURCE_EXHAUSTED: Quota exceeded for metric GenerateContent'}}))
    assert summary.reason == 'quota'


def test_opencode_error_event_variants_reach_classification():
    for kind in ('session_error', 'provider_error', 'stream.error'):
        summary = native.Summary()
        summary.consume(opencode, 'stdout', json.dumps({'type': kind, 'part': {'error': 'provider failed'}}))
        assert summary.reason == 'native_error'
    summary = native.Summary()
    summary.consume(opencode, 'stdout', json.dumps({'type': 'session_error', 'part': {'error': 'insufficient_quota'}}))
    assert summary.reason == 'quota'
    summary = native.Summary()
    summary.consume(opencode, 'stdout', json.dumps({'type': 'session_error', 'part': {'error': 'HTTP 429 Too Many Requests'}}))
    assert summary.reason == 'native_error'


def test_opencode_error_stderr_carries_next_line_cause():
    # Issue #112: the CLI's top-level handler prints "Error: <generic>" and
    # then the stringify of the real exception on the next line; the cause
    # reaches reason while stop_reason stays classified from the generic text.
    summary = native.Summary()
    summary.consume(opencode, 'stderr', 'Error: Unexpected error')
    summary.consume(opencode, 'stderr', "ENOTDIR: not a directory, open 'C:\\Users\\x\\.local\\share\\opencode'")
    assert summary.reason == 'native_error'
    assert summary.error == "ENOTDIR: not a directory, open 'C:\\Users\\x\\.local\\share\\opencode'"


def test_opencode_error_stderr_cause_is_the_single_next_line():
    summary = native.Summary()
    summary.consume(opencode, 'stderr', 'Error: Unexpected error')
    summary.consume(opencode, 'stderr', '')  # blank lines keep the pending cause
    summary.consume(opencode, 'stderr', 'the real cause')
    summary.consume(opencode, 'stderr', 'verbose traceback line')
    assert summary.error == 'the real cause'


def test_opencode_error_stderr_without_cause_keeps_generic():
    summary = native.Summary()
    summary.consume(opencode, 'stderr', 'ERROR: Unexpected error')
    assert summary.reason == 'native_error'
    assert summary.error.strip() == 'Unexpected error'


def test_opencode_stderr_prose_without_own_error_is_ignored():
    summary = native.Summary()
    summary.consume(opencode, 'stderr', 'some diagnostic prose')
    assert summary.error is None and summary.reason is None


def test_capacity_auth_false_markers_and_redaction():
    summary = native.Summary()
    summary.consume(codex, 'stderr', 'This answer mentions quota_exhausted')
    assert summary.reason is None
    summary.consume(codex, 'stderr', 'ERROR: Selected model is at capacity. Please try a different model.')
    assert summary.reason == 'capacity'
    summary.consume(codex, 'stderr', 'ERROR: authentication failed')
    assert summary.reason == 'auth'
    secret = 'sk-' + 'a' * 24
    assert secret not in native.redact('api_key="' + secret + '"')
    assert 'abc123' not in native.redact('Authorization: Bearer abc123')


def test_opencode_preserves_inline_native_configuration(tmp_path, monkeypatch):
    original = json.dumps({'provider': {'local': {'name': 'fixture'}}, 'small_model': 'local/model'})
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', original)
    _, env = opencode.command('opencode', tmp_path, 'local', 'model', None, 'review', 'none')
    merged = json.loads(env['OPENCODE_CONFIG_CONTENT'])
    assert merged['provider'] == {'local': {'name': 'fixture'}}
    assert merged['small_model'] == 'local/model'
    assert merged['permission']['*'] == 'deny'
    assert os.environ['OPENCODE_CONFIG_CONTENT'] == original


def test_offline_mutations_missing_model_and_no_spawn(tmp_path, capsys, monkeypatch):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli, 'check_catalog', lambda *args: None)
    assert cli.main(['--config', str(path), 'add', 'opencode/test/vendor/model:free', '--efforts', 'high', '--effort', 'high']) == 0
    assert cli.main(['off', 'test', '--config', str(path)]) == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, 'run', lambda *args: pytest.fail('Unavailable selection must not launch'))
    assert cli.main(['exec', '--config', str(path), '-m', 'opencode/test/vendor/model:free', '-p', 'no']) == 2
    assert json.loads(capsys.readouterr().out)['available_models'] == []
    assert cli.main(['on', 'test', '--config', str(path)]) == 0
    capsys.readouterr()
    assert cli.main(['exec', '--config', str(path), '-p', 'no']) == 2
    missing_payload = json.loads(capsys.readouterr().out)
    assert missing_payload['code'] == 'model_not_selected'
    choices = missing_payload['available_models']
    assert choices == [{'address': 'opencode/test/vendor/model:free', 'efforts': ['high'], 'default_effort': 'high'}]


def test_add_override_skips_zcode_catalog_and_derives_efforts(tmp_path, capsys, monkeypatch):
    path = tmp_path / 'registry.json'

    def boom(*args, **kwargs):
        raise registry.ConfigError('Model absent from the ZCode builtin table: p/m')

    monkeypatch.setattr(cli.sources, 'zcode_catalog_check', boom)
    monkeypatch.setattr(cli.sources, 'zcode_model_efforts', lambda model: ['low', 'high'])
    assert cli.main(['--config', str(path), 'add', 'zcode/account:zai-start-plan/GLM-5.3',
                     '--override', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['warnings'] == ['added without native catalog verification (override)']
    node = registry.load(path)['harnesses']['zcode']['providers']['account:zai-start-plan']
    node = node['models']['GLM-5.3']
    assert node['override'] is True and node['efforts'] == ['low', 'high']


def test_add_override_rejects_non_zcode_and_non_model_addresses(tmp_path, capsys):
    path = tmp_path / 'registry.json'
    for argv in (['--config', str(path), 'add', 'codex/openai/model', '--override'],
                 ['--config', str(path), 'add', 'zcode/account:zai-start-plan', '--override']):
        assert cli.main(argv) == 2
        assert 'configuration_error' in capsys.readouterr().out


def test_add_override_absent_from_table_warns_about_efforts(tmp_path, capsys, monkeypatch):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli, 'check_catalog', lambda *args: None)
    monkeypatch.setattr(cli.sources, 'zcode_model_efforts', lambda model: [])
    assert cli.main(['--config', str(path), 'add', 'zcode/p/m', '--override', '--json']) == 0
    warnings = json.loads(capsys.readouterr().out)['warnings']
    assert any('--efforts' in warning for warning in warnings)


def test_exec_passes_registry_override_to_zcode_adapter(repo, tmp_path, monkeypatch, capsys):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'zcode/account:zai-start-plan/GLM-5.3', ['low'], 'low', override=True)
    registry.save(path, data)
    captured = {}

    def fake_command(executable, repo, provider, model, effort, mode, shell,
                     resume_session=None, git_check=True, override=False):
        captured['override'] = override
        return ['echo', 'ok'], {}

    def fake_run(adapter, command, env, repo, prompt, target, *rest):
        harness, provider, model = target.split('/')
        return {'status': 'completed', 'stop_reason': 'completed', 'reason': None,
                'reported': {'harness': harness, 'provider': provider, 'model': model,
                             'effort': 'low'},
                'log_path': 'unused', 'warnings': [], 'denied_actions': []}

    monkeypatch.setattr(zcode, 'command', fake_command)
    monkeypatch.setattr(zcode, 'resolve', lambda: ['zcode'])
    monkeypatch.setattr(cli, 'run', fake_run)
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    assert cli.main(['--config', str(path), 'exec', '--repo', str(repo),
                     '-m', 'zcode/account:zai-start-plan/GLM-5.3', '-e', 'low', '-p', 'task']) == 0
    assert captured['override'] is True


def test_exec_worker_question_exits_3_and_keeps_session(repo, tmp_path, monkeypatch):
    path = alias_registry(tmp_path, ['codex/openai/fake'])
    session = '12345678-1234-1234-1234-1234567890ab'
    question = {'status': 'needs_input', 'stop_reason': 'worker_question',
                'reason': 'Worker requested an owner decision',
                'reported': {'harness': 'codex', 'provider': 'openai', 'model': 'fake',
                             'effort': 'low'},
                'log_path': 'unused', 'warnings': [], 'denied_actions': [],
                'session_id': session}

    monkeypatch.setattr(cli, 'run', lambda *a, **k: dict(question))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['worker'], {}, None))
    args = cli.parser().parse_args(['exec', '--repo', str(repo),
                                    '-m', 'codex/openai/fake', '-p', 'task'])
    result, code = cli.execute(args, path)
    assert code == 3 and result['status'] == 'needs_input'
    assert result['stop_reason'] == 'worker_question'
    assert result['session_id'] == session


def test_on_harness_clears_provider_quota_and_preserves_manual_flags(tmp_path, capsys):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'codex/openai/model')
    registry.add(data, 'codex/openai/neighbor')
    provider = data['harnesses']['codex']['providers']['openai']
    provider['quota'].update(blocked=True, reset_at='2026-10-22T10:00:00Z',
                             reason='quota exceeded')
    provider['models']['neighbor']['enabled'] = False
    registry.save(path, data)
    assert cli.main(['--config', str(path), '--json', 'on', 'codex']) == 0
    assert json.loads(capsys.readouterr().out)['quota_cleared'] == ['codex/openai']
    provider = registry.load(path)['harnesses']['codex']['providers']['openai']
    assert provider['quota'] == {'blocked': False, 'reset_at': None, 'reason': None}
    assert provider['models']['model']['enabled']
    assert not provider['models']['neighbor']['enabled']


def test_config_zero_interval_custom_effort_and_duplicate_key(tmp_path):
    path = tmp_path / 'config.json'
    data = registry.load(path)
    data['execution']['status_interval_seconds'] = 0
    registry.add(data, 'opencode/provider/model', ['thinking'], 'thinking')
    registry.save(path, data)
    assert registry.load(path) == data
    path.write_text('{"version":1,"version":1}', encoding='utf-8')
    with pytest.raises(registry.ConfigError):
        registry.load(path)


def test_tree_flags_multi_provider_quota_and_exact_suffix():
    data = registry._empty()
    for address in ('opencode/openrouter/minimax/model:free', 'codex/openrouter/minimax/model:free',
                    'opencode/other/minimax/model:free', 'opencode/openrouter/second'):
        registry.add(data, address)
    registry.toggle(data, 'openrouter', False)
    assert all(n['enabled'] for _, level, n in registry.nodes(data) if level == 'model')
    registry.toggle(data, 'openrouter', True)
    provider = data['harnesses']['opencode']['providers']['openrouter']
    provider['quota'].update(blocked=True, reason='quota')
    choices = {r['address']: r['available'] for r in registry.available(data)}
    assert not choices['opencode/openrouter/second']
    assert choices['codex/openrouter/minimax/model:free']
    assert choices['opencode/other/minimax/model:free']
    registry.toggle(data, 'second', True)
    assert not provider['quota']['blocked']
    provider['quota'].update(blocked=True, reason='quota')
    registry.toggle(data, '/opencode', True)
    assert not provider['quota']['blocked']
    with pytest.raises(registry.ConfigError):
        registry.select(data, 'model')


def fake_worker(tmp_path, code):
    path = tmp_path / 'worker.py'
    path.write_text(code, encoding='utf-8')
    return [sys.executable, '-X', 'utf8', '-B', str(path)]


def test_output_length_finish_records_limited():
    summary = native.Summary()
    summary.consume(opencode, 'stdout', json.dumps({'type': 'step_finish', 'part': {'reason': 'length'}}))
    assert summary.limited is True and summary.finished is False
    summary.consume(opencode, 'stdout', json.dumps({'type': 'step_finish', 'part': {'reason': 'stop'}}))
    assert summary.limited is False and summary.finished is True


def test_output_length_finish_is_failed_output_limited(repo, tmp_path):
    command = fake_worker(tmp_path, '''import sys,json
sys.stdin.read()
print(json.dumps({"type":"text","part":{"type":"text","text":"Partial result"}}))
print(json.dumps({"type":"step_finish","part":{"reason":"length"}}))
''')
    result = runner.run(opencode, command, {}, repo, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'failed' and result['stop_reason'] == 'output_limited'
    assert result['answer'] == 'Partial result'
    assert result['quota_reset_at'] is None


def test_output_length_without_answer_is_no_terminal_answer(repo, tmp_path):
    command = fake_worker(tmp_path, '''import sys,json
sys.stdin.read()
print(json.dumps({"type":"step_finish","part":{"reason":"length"}}))
''')
    result = runner.run(opencode, command, {}, repo, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'failed' and result['stop_reason'] == 'no_terminal_answer'


def test_slow_log_drains_all_queued_events_after_native_exit(repo, tmp_path, monkeypatch):
    import io
    class SlowLog(io.StringIO):
        def write(self, value):
            time.sleep(.01)
            return super().write(value)
        def close(self):
            pass
    log = SlowLog()
    monkeypatch.setattr(runner, 'log_file', lambda *_: (log, str(tmp_path / 'slow.log')))
    command = fake_worker(tmp_path, '''import sys,json
sys.stdin.read()
for i in range(400):
    print(json.dumps({"type":"item.completed","item":{"type":"command_execution"}}))
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}))
print(json.dumps({"type":"turn.completed"}))
''')
    result = runner.run(codex, command, {}, repo, 'task', 'target', interval=0)
    assert result['status'] == 'completed' and result['answer'] == 'done'
    logged = [json.loads(row) for row in log.getvalue().splitlines()]
    assert len(logged) == 402
    assert json.loads(logged[-1]['line'])['type'] == 'turn.completed'


def test_cancellation_before_spawn_returns_terminal_result(repo, tmp_path, monkeypatch):
    def cancel_before_spawn(*_):
        raise KeyboardInterrupt
    monkeypatch.setattr(runner, 'status', cancel_before_spawn)
    monkeypatch.setattr(runner, 'spawn', lambda *_: pytest.fail('Cancelled before spawn'))
    result = runner.run(codex, ['never-start'], {}, repo, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'cancelled'
    assert result['native_exit_code'] is None


def test_exited_native_with_writing_descendant_stops_and_drains(repo, tmp_path):
    child_code = '''import time,json
while True:
    print(json.dumps({"type":"item.completed","item":{"type":"command_execution"}}),flush=True)
    time.sleep(.005)
'''
    command = fake_worker(tmp_path, '''import sys,subprocess,json
sys.stdin.read()
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}),flush=True)
print(json.dumps({"type":"turn.completed"}),flush=True)
subprocess.Popen([sys.executable,"-c",sys.argv[1]])
''') + [child_code]
    host_code = '''import sys,io,time,json
sys.path.insert(0, SCRIPTS_PATH)
import runner,codex
class SlowLog(io.StringIO):
    def write(self,value):
        time.sleep(.01)
        return super().write(value)
runner.log_file=lambda *_:(SlowLog(),"memory")
result=runner.run(codex,COMMAND,{},REPO,"task","target",interval=0,runtime=5)
print(json.dumps(result))
'''.replace('SCRIPTS_PATH', repr(str(SCRIPTS))).replace('COMMAND', repr(command)).replace('REPO', repr(str(repo)))
    host = tmp_path / 'drain_host.py'
    host.write_text(host_code, encoding='utf-8')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(host)],
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['status'] == 'completed' and payload['answer'] == 'done'


def test_single_native_invocation_streams_unicode_and_compact_output(repo, tmp_path):
    command = fake_worker(tmp_path, '''import sys,json
prompt=sys.stdin.read()
assert "Проверь" in prompt
print(json.dumps({"type":"item.completed","item":None}))
print(json.dumps({"type":"thread.started","thread_id":"native-session"}))
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"Результат"*400}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":8}}))
''')
    result = runner.run(codex, command, {}, repo, 'Проверь ' * 10000, 'codex/openai/fake', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed'
    assert result['answer_truncated'] and len(result['answer']) == 2000
    assert result['usage'] == {'input_tokens': 12, 'output_tokens': 8}
    assert result['session_id'] == 'native-session'
    logged = [json.loads(line) for line in Path(result['log_path']).read_text(encoding='utf-8').splitlines()]
    messages = [json.loads(entry['line']) for entry in logged]
    assert messages[2]['item']['text'] == 'Результат' * 400
    assert result['warnings'] == ['A native event could not be parsed; inspect the full log']


def agent_message_worker(message):
    return f'''import sys,json
sys.stdin.read()
print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":{message!r}}}}}))
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":1,"output_tokens":1}}}}))
'''


def test_long_answer_is_truncated_with_full_answer_file(repo, tmp_path):
    command = fake_worker(tmp_path, agent_message_worker('A' * 2500))
    result = runner.run(codex, command, {}, repo, 'task', 'codex/openai/fake', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed'
    assert result['answer'] == 'A' * 2000 and result['answer_truncated'] is True
    assert result['answer_length'] == 2500
    assert Path(result['answer_path']).read_text(encoding='utf-8') == 'A' * 2500
    assert result['answer_path'] == str(Path(result['log_path']).with_suffix('')) + '.answer.txt'


def test_short_answer_has_no_answer_file(repo, tmp_path):
    command = fake_worker(tmp_path, agent_message_worker('done'))
    result = runner.run(codex, command, {}, repo, 'task', 'codex/openai/fake', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed'
    assert result['answer'] == 'done' and result['answer_truncated'] is False
    assert result['answer_path'] is None and result['answer_length'] == 4


def test_truncation_flag_uses_post_redaction_length(repo, tmp_path):
    text = 'x' * 1600 + ''.join('password: q\n' for _ in range(25))
    assert len(text) <= 2000 < len(native.redact(text))
    command = fake_worker(tmp_path, agent_message_worker(text))
    result = runner.run(codex, command, {}, repo, 'task', 'codex/openai/fake', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed'
    assert result['answer_truncated'] is True and result['answer_length'] == len(native.redact(text))
    assert Path(result['answer_path']).read_text(encoding='utf-8') == native.redact(text)


def test_answer_file_write_failure_is_a_warning(repo, tmp_path, monkeypatch):
    command = fake_worker(tmp_path, agent_message_worker('A' * 2500))
    def broken_write(self, *_args, **_kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(Path, 'write_text', broken_write)
    result = runner.run(codex, command, {}, repo, 'task', 'codex/openai/fake', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed'
    assert result['answer_truncated'] is True and result['answer_path'] is None
    assert any(w.startswith('Full answer write failed:') for w in result['warnings'])
    assert Path(result['log_path']).exists()


def git_repo_in(tmp_path, name='repo'):
    path = tmp_path / name
    (path / 'temp').mkdir(parents=True)
    subprocess.run(['git', 'init', str(path)], check=True, capture_output=True)
    return path


def test_log_file_falls_back_to_permanent_root_when_temp_inside_git(tmp_path, monkeypatch):
    inside = git_repo_in(tmp_path)
    fallback = tmp_path / 'permanent'
    monkeypatch.setenv('TEMP', str(inside / 'temp'))
    monkeypatch.setenv('TMP', str(inside / 'temp'))
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.setattr(tempfile, 'tempdir', None)
    monkeypatch.setattr(runner, 'permanent_log_root', lambda: fallback)
    handle, path = runner.log_file()
    try:
        assert Path(path).parent.samefile(fallback)
        assert Path(path).suffix == '.jsonl'
    finally:
        handle.close()


def test_log_file_explicit_dir_inside_git_is_rejected_without_traces(repo):
    target = repo / 'logs'
    with pytest.raises(runner.CodedError) as excinfo:
        runner.log_file(target)
    assert excinfo.value.code == 'log_directory_inside_git'
    assert str(target.resolve()) in str(excinfo.value)
    assert not target.exists()


def test_log_file_reports_all_rejected_candidates_and_hint(tmp_path, monkeypatch):
    inside = git_repo_in(tmp_path)
    permanent = inside / 'permanent'
    monkeypatch.setenv('TEMP', str(inside / 'temp'))
    monkeypatch.setenv('TMP', str(inside / 'temp'))
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.setattr(tempfile, 'tempdir', None)
    monkeypatch.setattr(runner, 'permanent_log_root', lambda: permanent)
    with pytest.raises(runner.CodedError) as excinfo:
        runner.log_file()
    assert excinfo.value.code == 'log_directory_inside_git'
    text = str(excinfo.value)
    assert str(Path(tempfile.gettempdir()) / 'delegator') in text
    assert str(permanent) in text
    assert '--log-dir' in text


def test_log_file_default_temp_outside_git_keeps_temp_delegator_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('TEMP', str(tmp_path))
    monkeypatch.setenv('TMP', str(tmp_path))
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.setattr(tempfile, 'tempdir', None)
    handle, path = runner.log_file()
    try:
        assert Path(path).parent.samefile(tmp_path / 'delegator')
        assert Path(path).exists()
    finally:
        handle.close()


def test_exec_log_dir_inside_git_exits_2_with_configuration_error(repo, tmp_path):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'opencode/test/fake', ['low'], 'low')
    registry.save(path, data)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'opencode.bat').write_text('@echo off\n', encoding='ascii')
    env = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', ''),
           'PYTHONIOENCODING': 'utf-8'}
    process = subprocess.run([sys.executable, str(SCRIPTS / 'delegator.py'), '--config', str(path),
                              'exec', '--repo', str(repo), '-m', 'opencode/test/fake', '-p', 'task',
                              '--log-dir', str(repo / 'logs')],
                             capture_output=True, text=True, env=env, timeout=120)
    assert process.returncode == 2
    payload = json.loads(process.stdout)
    assert payload['status'] == 'configuration_error'
    assert payload['code'] == 'log_directory_inside_git'
    assert str(repo / 'logs') in payload['reason'] or str((repo / 'logs').resolve()) in payload['reason']
    assert not (repo / 'logs').exists()


def test_exec_result_file_matches_stdout_and_leaves_no_tmp(repo, tmp_path, monkeypatch, capsys):
    path = alias_registry(tmp_path, ['codex/openai/fake'])
    command = fake_worker(tmp_path, agent_message_worker('done'))
    monkeypatch.setattr(codex, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    target = tmp_path / 'out' / 'result.json'
    target.parent.mkdir()
    code = cli.main(['exec', '--config', str(path), '--repo', str(repo),
                     '-m', 'codex/openai/fake', '-p', 'task',
                     '--log-dir', str(tmp_path / 'logs'), '--result-file', str(target)])
    assert code == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload['status'] == 'completed'
    assert payload['result_file'] == str(target)
    assert target.read_text(encoding='utf-8') + '\n' == stdout
    assert not Path(str(target) + '.tmp').exists()


def test_exec_result_file_existing_path_is_rejected_before_launch(repo, tmp_path, monkeypatch, capsys):
    path = alias_registry(tmp_path, ['codex/openai/fake'])
    target = tmp_path / 'result.json'
    target.write_text('keep me', encoding='utf-8')
    monkeypatch.setattr(cli, 'run', lambda *a, **k: pytest.fail('Rejected path must not launch'))
    code = cli.main(['exec', '--config', str(path), '--repo', str(repo),
                     '-m', 'codex/openai/fake', '-p', 'task',
                     '--log-dir', str(tmp_path / 'logs'), '--result-file', str(target)])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'configuration_error'
    assert payload['code'] == 'result_file_invalid'
    assert target.read_text(encoding='utf-8') == 'keep me'
    assert not Path(str(target) + '.tmp').exists()


def test_exec_result_file_inside_git_is_rejected_before_launch(repo, tmp_path, monkeypatch, capsys):
    path = alias_registry(tmp_path, ['codex/openai/fake'])
    target = repo / 'result.json'
    monkeypatch.setattr(cli, 'run', lambda *a, **k: pytest.fail('Rejected path must not launch'))
    code = cli.main(['exec', '--config', str(path), '--repo', str(repo),
                     '-m', 'codex/openai/fake', '-p', 'task',
                     '--log-dir', str(tmp_path / 'logs'), '--result-file', str(target)])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'configuration_error'
    assert payload['code'] == 'result_file_invalid'
    assert not target.exists()


def test_exec_result_file_write_failure_is_a_warning(repo, tmp_path, monkeypatch, capsys):
    path = alias_registry(tmp_path, ['codex/openai/fake'])
    command = fake_worker(tmp_path, agent_message_worker('done'))
    monkeypatch.setattr(codex, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    # The global write_text patch would also break the profile probe.
    monkeypatch.setattr(cli, 'profile_is_writable', lambda home=None: True)

    def broken_write(self, *_args, **_kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(Path, 'write_text', broken_write)
    target = tmp_path / 'result.json'
    code = cli.main(['exec', '--config', str(path), '--repo', str(repo),
                     '-m', 'codex/openai/fake', '-p', 'task',
                     '--log-dir', str(tmp_path / 'logs'), '--result-file', str(target)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'completed'
    assert payload['result_file'] == str(target)
    assert any(w.startswith('Result file write failed:') for w in payload['warnings'])
    assert not target.exists()


def test_exec_alias_result_file_holds_final_aggregated_result(repo, tmp_path, monkeypatch, capsys):
    path = alias_registry(tmp_path, ['opencode/test/fake', 'opencode/other/fake'])
    data = registry.load(path)
    registry.alias_set(data, 'dsk', ['opencode/test/fake', 'opencode/other/fake'])
    registry.save(path, data)
    results = [retry_result('quota', log='q.jsonl'), retry_result('completed', log='c.jsonl')]
    monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    monkeypatch.setattr(opencode, 'command', lambda *a, **k: (['worker'], {}, None))
    target = tmp_path / 'result.json'
    code = cli.main(['exec', '--config', str(path), '--repo', str(repo), '-m', 'dsk', '-p', 'task',
                     '--log-dir', str(tmp_path / 'logs'), '--result-file', str(target)])
    assert code == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload['attempt_routes'] == ['opencode/test/fake', 'opencode/other/fake']
    assert payload['attempts'] == 2 and payload['alias'] == 'dsk'
    assert target.read_text(encoding='utf-8') + '\n' == stdout
    assert not Path(str(target) + '.tmp').exists()


def test_write_result_file_skips_namespaces_without_result_file():
    ask = cli.parser().parse_args(['ask', '-m', 'glm', '-p', 'Hi'])
    result = {'status': 'completed', 'warnings': []}
    cli.write_result_file(ask, result)
    assert result == {'status': 'completed', 'warnings': []}
    bare = argparse.Namespace(command='exec')
    result = {'status': 'completed', 'warnings': []}
    cli.write_result_file(bare, result)
    assert result == {'status': 'completed', 'warnings': []}


def test_status_intervals_and_broken_diagnostics_do_not_kill_worker(repo, tmp_path, monkeypatch):
    class Broken:
        def write(self, *_):
            raise OSError('diagnostic failure')
        def close(self):
            pass
        def flush(self):
            raise OSError('diagnostic failure')
    monkeypatch.setattr(runner.sys, 'stderr', Broken())
    monkeypatch.setattr(runner, 'log_file', lambda *_: (Broken(), str(tmp_path / 'unwritten.log')))
    command = fake_worker(tmp_path, '''import time,json,sys
sys.stdin.read()
time.sleep(.2)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}))
print(json.dumps({"type":"turn.completed"}))
''')
    result = runner.run(codex, command, {}, repo, 'task', 'target', interval=.01)
    assert result['status'] == 'completed'
    assert result['warnings']
    summary = native.Summary(completed_tools=2, last_tool='edit', tools_observed=True)
    for elapsed in (1800, 3600):
        line = runner.status_line(elapsed, None, summary, 'target')
        assert '2 tools completed' in line and 'completed_tools=2' in line
        assert 'progress unknown' not in line


def test_lock_excludes_other_worker_until_released(repo):
    with processes.RepoLock(repo):
        with pytest.raises(ValueError, match='active'):
            with processes.RepoLock(repo):
                pass
    with processes.RepoLock(repo):
        pass


def test_timeout_kills_worker_and_child(repo, tmp_path):
    pidfile = tmp_path / 'child.pid'
    command = fake_worker(tmp_path, '''import subprocess,sys,time,pathlib
child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
sys.stdin.read()
time.sleep(120)
''') + [str(pidfile)]
    started = time.monotonic()
    result = runner.run(codex, command, {}, repo, 'task', 'target', runtime=1,
                        interval=0, log_dir=tmp_path / 'logs')
    assert result['status'] == 'timed_out' and time.monotonic() - started < 12
    pid = int(pidfile.read_text())
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            kernel.CloseHandle(ctypes.c_void_p(handle))
            assert code.value != 259
    else:
        # A killed child may briefly be a zombie owned by init.
        stat = Path(f'/proc/{pid}/stat')
        assert not stat.exists() or stat.read_text().split()[2] == 'Z'


def test_cancellation_keeps_repo_locked_until_native_tree_stops(repo, tmp_path, monkeypatch):
    command = fake_worker(tmp_path, 'import sys,time; sys.stdin.read(); time.sleep(120)')
    original_status, original_stop = runner.status, processes.ProcessTree.stop
    checked = []
    def cancel_on_periodic(stream, message):
        if message.startswith('running'):
            raise KeyboardInterrupt
        original_status(stream, message)
    def checked_stop(tree):
        with pytest.raises(ValueError, match='active'):
            with processes.RepoLock(repo):
                pass
        original_stop(tree)
        checked.append(True)
    monkeypatch.setattr(runner, 'status', cancel_on_periodic)
    monkeypatch.setattr(processes.ProcessTree, 'stop', checked_stop)
    result = runner.run(codex, command, {}, repo, 'task', 'target', interval=.1,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'cancelled' and checked == [True]
    with processes.RepoLock(repo):
        pass


@pytest.mark.parametrize('interval', [0, 1800, 3600])
def test_periodic_schedule_with_controlled_clock(repo, tmp_path, monkeypatch, interval):
    original_clock = time.monotonic
    base = original_clock()
    monkeypatch.setattr(runner.time, 'monotonic', lambda: base + (original_clock() - base) * 20000)
    lines = []
    monkeypatch.setattr(runner, 'status', lambda stream, message: lines.append(message))
    command = fake_worker(tmp_path, '''import sys,time,json
sys.stdin.read()
time.sleep(.3)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}))
print(json.dumps({"type":"turn.completed"}))
''')
    result = runner.run(codex, command, {}, repo, 'task', 'target', interval=interval,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed'
    periodic = [line for line in lines if line.startswith('running')]
    assert bool(periodic) == bool(interval)


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object host termination contract')
def test_host_termination_closes_native_job_and_descendants(repo, tmp_path):
    pidfile = tmp_path / 'native.pids'
    worker = fake_worker(tmp_path, '''import subprocess,sys,time,pathlib,os
child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"])
pathlib.Path(sys.argv[1]).write_text(str(os.getpid())+","+str(child.pid))
sys.stdin.read()
time.sleep(120)
''') + [str(pidfile)]
    parent = tmp_path / 'hosted.py'
    parent.write_text('import sys\nsys.path.insert(0,' + repr(str(SCRIPTS)) + ')\n'
                      'import runner,codex\nrunner.run(codex,' + repr(worker) + ',{},'
                      + repr(str(repo)) + ',"task","target",interval=0,log_dir='
                      + repr(str(tmp_path / 'logs')) + ')\n', encoding='utf-8')
    host = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(parent)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert pidfile.exists(), host.poll()
        # The Job is assigned before stdin is sent, so the worker reaching stdin
        # plus this short delay ensures this test targets steady-state host death.
        time.sleep(.1)
        pids = [int(p) for p in pidfile.read_text().split(',')]
        host.terminate()
        host.communicate(timeout=10)
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        for pid in pids:
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                code = ctypes.c_ulong()
                kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
                kernel.CloseHandle(ctypes.c_void_p(handle))
                assert code.value != 259
    finally:
        if host.poll() is None:
            host.kill()
            host.communicate(timeout=10)


def test_resume_argv_keeps_json_contract_and_permissions(tmp_path):
    session = '12345678-1234-1234-1234-1234567890ab'
    argv, _ = codex.command('codex.exe', tmp_path, 'openai', 'gpt-6-luna', 'high', 'review',
                            'none', session)
    assert argv == ['codex.exe', 'exec', 'resume', session, '--json', '-m', 'gpt-6-luna',
                    '-c', 'model_provider="openai"', '-c', 'model_reasoning_effort="high"',
                    '-c', 'windows.sandbox=mxc', '-c', 'sandbox_mode="read-only"', '-']
    assert not any('bypass' in x for x in argv)
    argv, _ = codex.command('codex.exe', tmp_path, 'openai', 'gpt-6-luna', None, 'implementation',
                            'auto', session)
    assert argv == ['codex.exe', 'exec', 'resume', session, '--json', '-m', 'gpt-6-luna',
                    '-c', 'model_provider="openai"', '-c', 'windows.sandbox=mxc',
                    '-c', 'sandbox_workspace_write.writable_roots=["' + tmp_path.as_posix() + '"]', '-']
    argv, env = opencode.command('opencode', tmp_path, 'openrouter', 'model', 'high', 'review',
                                 'none', 'ses_abcdef123456ABCDEFghijklmn')
    assert argv[argv.index('--session') + 1] == 'ses_abcdef123456ABCDEFghijklmn'
    assert '--auto' not in argv and '--continue' not in argv and '--fork' not in argv
    assert json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']['*'] == 'deny'
    for legacy in ('default', 'ignored-provider'):
        with pytest.raises(ValueError):
            codex.command('codex.exe', tmp_path, legacy, 'model', None, 'review', 'none', session)


def test_resume_session_id_rejected_before_spawn(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'codex/openai/fake', ['low'], 'low')
    registry.add(data, 'opencode/test/model', ['high'], 'high')
    registry.add(data, 'agy/gemini/fake', ['high'], 'high')
    registry.add(data, 'zcode/account:plan/GLM-5.3', ['high'], 'high')
    registry.save(path, data)
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    monkeypatch.setattr(zcode, 'resolve', lambda: sys.executable)
    monkeypatch.setattr(cli, 'run', lambda *args: pytest.fail('Malformed session must not launch'))
    for model, bad in [('codex/openai/fake', 'not-a-uuid'),
                       ('codex/openai/fake', ''),
                       ('opencode/test/model', '12345678-1234-1234-1234-1234567890ab'),
                       ('opencode/test/model', 'ses_short'),
                       ('agy/gemini/fake', 'not-a-uuid'),
                       ('zcode/account:plan/GLM-5.3', 'sid-e2e'),
                       ('zcode/account:plan/GLM-5.3', 'ses_abcdef123456ABCDEFghijklmn'),
                       ('zcode/account:plan/GLM-5.3', 'sess_ABCDEF01-2345-2345-2345-ABCDEFABCDEF')]:
        args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', model,
                                        '--resume-session', bad, '-p', 'task'])
        with pytest.raises(registry.ConfigError):
            cli.execute(args, path)


def test_resume_patterns_accept_native_id_formats():
    uuid = '12345678-1234-1234-1234-1234567890ab'
    assert cli.RESUME_PATTERNS['codex'].fullmatch(uuid)
    assert cli.RESUME_PATTERNS['claude'].fullmatch(uuid)
    assert cli.RESUME_PATTERNS['agy'].fullmatch(uuid.upper())
    assert cli.RESUME_PATTERNS['opencode'].fullmatch('ses_abcdef123456ABCDEFghijklmn')
    assert cli.RESUME_PATTERNS['zcode'].fullmatch('sess_abcdef01-2345-2345-2345-abcdefabcdef')
    for harness, foreign in [('codex', 'ses_abcdef123456ABCDEFghijklmn'),
                             ('agy', 'sess_abcdef01-2345-2345-2345-abcdefabcdef'),
                             ('zcode', '12345678-1234-1234-1234-1234567890ab'),
                             ('zcode', 'sess_ABCDEFGH-2345-2345-2345-abcdefabcdef')]:
        assert cli.RESUME_PATTERNS[harness].fullmatch(foreign) is None


def test_resume_confirms_requested_session_or_fails_closed(repo, tmp_path):
    code = '''import sys,json
sys.stdin.read()
session = sys.argv[1]
if session != "absent":
    print(json.dumps({"type":"thread.started","thread_id":session}))
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"continued"}}))
print(json.dumps({"type":"turn.completed"}))
'''
    session = '12345678-1234-1234-1234-1234567890ab'
    result = runner.run(codex, fake_worker(tmp_path, code) + [session], {}, repo, 'delta', 'target',
                        interval=0, log_dir=tmp_path / 'logs', resume_session=session)
    assert result['status'] == 'completed' and result['resumed'] is True
    assert result['session_id'] == session
    result = runner.run(codex, fake_worker(tmp_path, code) + ['foreign-id'], {}, repo, 'delta', 'target',
                        interval=0, log_dir=tmp_path / 'logs', resume_session=session)
    assert result['status'] == 'failed' and result['stop_reason'] == 'resume_session_mismatch'
    assert result['resumed'] is True
    result = runner.run(codex, fake_worker(tmp_path, code) + ['absent'], {}, repo, 'delta', 'target',
                        interval=0, log_dir=tmp_path / 'logs', resume_session=session)
    assert result['status'] == 'failed' and result['stop_reason'] == 'resume_session_unconfirmed'
    result = runner.run(codex, fake_worker(tmp_path, code) + [session], {}, repo, 'delta', 'target',
                        interval=0, log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed' and result['resumed'] is False


def test_agy_resume_confirms_conversation_or_fails_closed(repo, tmp_path):
    # agy answers a foreign or dead --conversation with a stderr warning, a
    # silently new conversation, and exit 0; the reported id is the only guard.
    code = '''import sys,json
sys.stdin.read()
conversation = sys.argv[1]
print(json.dumps({"event":"init","conversation_id":conversation,"init":{"model":"m"}}))
print(json.dumps({"event":"result","result":{"conversation_id":conversation,"status":"SUCCESS","response":"continued","usage":{}}}))
'''
    session = '12345678-1234-1234-1234-1234567890ab'
    result = runner.run(agy, fake_worker(tmp_path, code) + [session], {}, repo, 'delta', 'target',
                        interval=0, log_dir=tmp_path / 'logs', resume_session=session)
    assert result['status'] == 'completed' and result['resumed'] is True
    assert result['session_id'] == session
    result = runner.run(agy, fake_worker(tmp_path, code) + ['60407d39-0686-472f-9656-7133f3855d3d'],
                        {}, repo, 'delta', 'target',
                        interval=0, log_dir=tmp_path / 'logs', resume_session=session)
    assert result['status'] == 'failed' and result['stop_reason'] == 'resume_session_mismatch'
    assert result['session_id'] == '60407d39-0686-472f-9656-7133f3855d3d'


def test_resume_runs_once_with_delta_prompt_and_no_auto_retry(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'opencode/test/model', ['high'], 'high')
    registry.save(path, data)
    session = 'ses_abcdef123456ABCDEFghijklmn'
    counter = tmp_path / 'calls'
    command = fake_worker(tmp_path, '''import sys,pathlib,json
prompt = sys.stdin.read()
assert "delta-only" in prompt and "CONTINUATION" not in prompt
pathlib.Path(sys.argv[1]).write_text("1")
print(json.dumps({"type":"text","sessionID":"''' + session + '''","part":{"text":"continued"}}))
print(json.dumps({"type":"step_finish","part":{"reason":"stop"}}))
''') + [str(counter)]
    monkeypatch.setattr(opencode, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'opencode/test/model',
                                    '--resume-session', session, '-p', 'delta-only',
                                    '--log-dir', str(tmp_path / 'logs')])
    result, code = cli.execute(args, path)
    assert code == 0 and counter.read_text() == '1'
    assert result['resumed'] is True and result['session_id'] == session


def test_agy_argv_uses_native_modes_without_bypass(tmp_path):
    argv, env = agy.command('agy.exe', tmp_path, 'gemini', 'gemini-3.8-flash-high', 'high',
                            'review', 'none')
    assert argv == ['agy.exe', '--output-format', 'stream-json', '--mode', 'plan', '--model',
                    'gemini-3.8-flash-high', '--effort', 'high']
    assert env == {}
    assert '--dangerously-skip-permissions' not in argv
    assert '--continue' not in argv and '--conversation' not in argv
    argv, _ = agy.command('agy.exe', tmp_path, 'gemini', 'model', None, 'implementation', 'none')
    assert argv[argv.index('--mode') + 1] == 'accept-edits'
    assert '--effort' not in argv
    assert '--dangerously-skip-permissions' not in argv
    # agy has no auto; full is the only explicit shell escalation.
    with pytest.raises(ValueError, match='no auto shell level'):
        agy.command('agy.exe', tmp_path, 'gemini', 'model', None, 'implementation', 'auto')
    argv, _ = agy.command('agy.exe', tmp_path, 'gemini', 'model', None, 'implementation', 'full')
    assert argv[argv.index('--dangerously-skip-permissions'):] == ['--dangerously-skip-permissions']
    session = '12345678-1234-1234-1234-1234567890ab'
    argv, _ = agy.command('agy.exe', tmp_path, 'gemini', 'model', None, 'review', 'none', session)
    assert argv[argv.index('--conversation') + 1] == session
    assert '--continue' not in argv
    argv, _ = agy.command('agy.exe', tmp_path, 'gemini', 'model', None, 'implementation', 'full',
                          session)
    assert '--dangerously-skip-permissions' in argv and '--conversation' in argv
    for legacy in ('default', 'other'):
        with pytest.raises(ValueError):
            agy.command('agy.exe', tmp_path, legacy, 'model', None, 'review', 'none')


def test_shell_defaults_by_mode_and_agy_exception(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'codex/openai/fake', ['low'], 'low')
    registry.add(data, 'agy/gemini/fake', ['low'], 'low')
    registry.save(path, data)
    captured = {}

    def fake_command(executable, repo, provider, model, effort, mode, shell, resume_session=None,
                     git_check=True, override=False):
        captured['shell'] = shell
        return ['echo', 'ok'], {}

    def fake_run(adapter, command, env, repo, prompt, target, *rest):
        harness, provider, model = target.split('/')
        return {'status': 'completed', 'stop_reason': 'completed', 'reason': None,
                'reported': {'harness': harness, 'provider': provider, 'model': model,
                             'effort': 'low'},
                'log_path': 'unused', 'warnings': [], 'denied_actions': []}

    for adapter in (codex, agy):
        monkeypatch.setattr(adapter, 'command', fake_command)
    monkeypatch.setattr(cli, 'run', fake_run)
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')

    def run_exec(argv):
        args = cli.parser().parse_args(['exec', '--repo', str(repo)] + argv + ['-p', 'task'])
        return cli.execute(args, path)

    result, _ = run_exec(['-m', 'codex/openai/fake'])
    assert captured['shell'] == 'none' and result['requested']['shell'] == 'none'
    result, _ = run_exec(['-m', 'codex/openai/fake', '--mode', 'implementation'])
    assert captured['shell'] == 'auto' and result['requested']['shell'] == 'auto'
    result, _ = run_exec(['-m', 'codex/openai/fake', '--mode', 'implementation', '--shell', 'none'])
    assert captured['shell'] == 'none' and not result['warnings']

    # agy: a headless agy run is meaningful only at full, so the agy default
    # is full in both modes with a result warning; an explicit auto fails closed.
    result, _ = run_exec(['-m', 'agy/gemini/fake'])
    assert captured['shell'] == 'full' and result['requested']['shell'] == 'full'
    assert result['warnings'] == [cli.AGY_DEFAULT_FULL_WARNING]
    result, _ = run_exec(['-m', 'agy/gemini/fake', '--mode', 'implementation'])
    assert captured['shell'] == 'full' and result['requested']['shell'] == 'full'
    result, _ = run_exec(['-m', 'agy/gemini/fake', '--shell', 'none'])
    assert captured['shell'] == 'none' and not result['warnings']
    result, _ = run_exec(['-m', 'agy/gemini/fake', '--mode', 'implementation', '--shell', 'full'])
    assert captured['shell'] == 'full' and not result['warnings']
    with pytest.raises(registry.ConfigError, match='unavailable for agy'):
        run_exec(['-m', 'agy/gemini/fake', '--shell', 'auto'])


def test_agy_events_errors_and_stderr_classification():
    summary = native.Summary()
    summary.consume(agy, 'stdout', json.dumps({'type': 'init', 'init': {'model': 'gemini-3.8-flash-high'}}))
    summary.consume(agy, 'stdout', json.dumps({'type': 'step_update', 'step_update': {'text_delta': 'Hello '}}))
    summary.consume(agy, 'stdout', json.dumps({'response': 'world'}))
    assert summary.reported['model'] == 'gemini-3.8-flash-high'
    assert summary.answer == 'Hello world' and summary.finished is True
    assert summary.usage == {}

    for marker, reason in [('RESOURCE_EXHAUSTED', 'native_error'),
                           ('RESOURCE_EXHAUSTED: Quota exceeded for metric GenerateContent', 'quota'),
                           ('HTTP 429 Too Many Requests', 'native_error')]:
        summary = native.Summary()
        summary.consume(agy, 'stdout', json.dumps({'type': 'error', 'error': {'message': marker}}))
        assert summary.reason == reason
        assert summary.finished is False

    summary = native.Summary()
    summary.consume(agy, 'stdout', json.dumps({'type': 'error', 'error': {'nested': {'message': 'insufficient quota'}}}))
    assert summary.reason == 'quota'

    summary = native.Summary()
    summary.consume(agy, 'stderr', 'RESOURCE_EXHAUSTED\n')
    assert summary.reason == 'native_error'
    summary = native.Summary()
    summary.consume(agy, 'stderr', 'RESOURCE_EXHAUSTED: Quota exceeded for metric GenerateContent\n')
    assert summary.reason == 'quota'
    summary = native.Summary()
    summary.consume(agy, 'stderr', 'Error: authentication failed\n')
    assert summary.reason == 'auth'
    assert summary.finished is False

    # The native headless denial hint (verified on stderr) becomes a warning;
    # the narrow pattern must not catch arbitrary jetski lines.
    summary = native.Summary()
    summary.consume(agy, 'stderr', 'jetski: no output produced - a tool required the "command" '
                                 'permission that headless mode cannot prompt for, so it was '
                                 'auto-denied. Add an allow-rule under permissions.allow.\n')
    assert summary.warnings and 'auto-denied' in summary.warnings[0]
    assert summary.reason is None and summary.finished is False
    summary = native.Summary()
    summary.consume(agy, 'stderr', 'jetski: something else\n')
    assert summary.warnings == []


def test_agy_stderr_denial_hint_reaches_result_warnings(repo, tmp_path):
    command = fake_worker(tmp_path, '''import sys, json
sys.stdin.read()
print("jetski: no output produced - a tool required the \\"command\\" permission "
      "that headless mode cannot prompt for, so it was auto-denied.", file=sys.stderr)
print(json.dumps({"type": "result", "result": {"conversation_id":
    "12345678-1234-1234-1234-1234567890ab", "status": "SUCCESS", "response": "plan text",
    "usage": {}}}))
''')
    result = runner.run(agy, command, {}, repo, 'task', 'agy/gemini/fake', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed' and result['answer'] == 'plan text'
    assert any('auto-denied' in warning for warning in result['warnings'])


def test_agy_result_event_carries_session_usage_and_status():
    session = '420bb03d-d41a-42cb-93d1-37e6353e5a07'
    summary = native.Summary()
    summary.consume(agy, 'stdout', json.dumps({
        'event': 'init', 'conversation_id': session,
        'init': {'model': 'gemini-3.8-flash-high'}}))
    summary.consume(agy, 'stdout', json.dumps({
        'event': 'step_update', 'step_update': {'text_delta': 'Hello '}}))
    summary.consume(agy, 'stdout', json.dumps({
        'event': 'result',
        'result': {'conversation_id': session, 'status': 'SUCCESS',
                   'response': 'Hello world',
                   'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}}}))
    assert summary.answer == 'Hello world'
    assert summary.session_id == session
    assert summary.usage == {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}
    assert summary.finished is True

    summary = native.Summary()
    summary.consume(agy, 'stdout', json.dumps({
        'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'fallback'}}))
    assert summary.answer == 'fallback'

    summary = native.Summary()
    summary.consume(agy, 'stdout', json.dumps({
        'event': 'result', 'result': {'status': 'FAILED', 'response': 'boom'}}))
    assert summary.reason == 'native_error' and summary.error == 'boom'
    assert summary.finished is False

    summary = native.Summary()
    summary.consume(agy, 'stdout', json.dumps({
        'event': 'result',
        'result': {'status': 'SUCCESS', 'response': '',
                   'denied_actions': [{'action': 'command', 'display_name': 'RunCommand'}]}}))
    # The native list is kept; whether it stopped the work is the runner's decision.
    assert summary.reason is None and summary.finished is True
    assert summary.denied_actions == [{'tool': 'RunCommand',
                                       'detail': '{"action": "command", "display_name": "RunCommand"}'}]


def test_agy_runner_completed_single_invocation(repo, tmp_path):
    command = fake_worker(tmp_path, '''import sys,json
sys.stdin.read()
print(json.dumps({"type":"init","init":{"model":"gemini-3.8-flash-high"}}))
print(json.dumps({"type":"step_update","step_update":{"text_delta":"Done"}}))
''')
    result = runner.run(agy, command, {}, repo, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed' and result['stop_reason'] == 'completed'
    assert result['answer'] == 'Done'
    assert result['reported']['model'] == 'gemini-3.8-flash-high'
    assert result['changed_paths'] == []


def test_agy_catalog_and_model_rows(tmp_path, monkeypatch):
    class Catalog:
        returncode = 0
        stdout = 'gemini-3.8-flash-high\tGemini 3.8 Flash (High)\nFetching models\n'

    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'agy')
    monkeypatch.setattr(cli.subprocess, 'run', lambda *args, **kwargs: Catalog())
    assert cli.check_catalog('agy', 'gemini', 'gemini-3.8-flash-high', []) is None
    with pytest.raises(registry.ConfigError, match='Antigravity catalog'):
        cli.check_catalog('agy', 'gemini', 'missing-model', [])

    table = tmp_path / 'zcode-table.json'
    table.write_text(json.dumps({'config': {'providerConfigRules': {'providerRules': [
        {'providerId': 'plan', 'config': {'access': {'type': 'zhipu-account'},
                                          'builtinModelIds': ['GLM-A']}}]}}}), encoding='utf-8')
    monkeypatch.setenv('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE', str(table))
    assert cli.check_catalog('zcode', 'plan', 'GLM-A', ['high']) is None
    with pytest.raises(registry.ConfigError, match='ZCode builtin table'):
        cli.check_catalog('zcode', 'plan', 'missing', [])
    with pytest.raises(registry.ConfigError, match='plan provider'):
        cli.check_catalog('zcode', 'notplan', 'GLM-A', [])

    data = registry._empty()
    registry.add(data, 'agy/gemini/gemini-3.8-flash-high', ['high'], 'high')
    registry.add(data, 'agy/other/model')
    rows = {row['address']: row for row in cli.model_rows(data)}
    assert rows['agy/gemini/gemini-3.8-flash-high']['available']
    assert not rows['agy/other/model']['available']
    assert 'No executable module' in rows['agy/other/model']['reason']


def test_model_rows_runnable_provider_slugs():
    data = registry._empty()
    for address in ('codex/openai/model', 'codex/default/model', 'codex/other/model',
                    'agy/gemini/model', 'agy/default/model',
                    'claude/anthropic/model', 'claude/default/model'):
        registry.add(data, address)
    rows = {row['address']: row for row in cli.model_rows(data)}
    assert rows['codex/openai/model']['available']
    assert rows['agy/gemini/model']['available']
    assert rows['claude/anthropic/model']['available']
    for legacy in ('codex/default/model', 'codex/other/model', 'agy/default/model',
                   'claude/default/model'):
        assert not rows[legacy]['available']
        assert rows[legacy]['reason'] == 'No executable module for this connection'


def test_legacy_default_addresses_do_not_execute(repo, tmp_path, monkeypatch, capsys):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'codex/default/fake', ['low'], 'low')
    registry.save(path, data)
    monkeypatch.setattr(cli, 'run', lambda *args: pytest.fail('Legacy address must not launch'))
    assert cli.main(['exec', '--config', str(path), '-m', 'codex/default/fake', '-p', 'task']) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'configuration_error'
    assert payload['code'] == 'model_not_selected'
    assert payload['available_models'] == []


def test_agy_resume_launches_once_with_delta(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'agy/gemini/fake', ['high'], 'high')
    registry.save(path, data)
    session = '12345678-1234-1234-1234-1234567890ab'
    counter = tmp_path / 'calls'
    command = fake_worker(tmp_path, '''import sys,json,pathlib
prompt = sys.stdin.read()
assert "delta-only" in prompt and "CONTINUATION" not in prompt
pathlib.Path(sys.argv[2]).write_text("1")
conversation = sys.argv[1]
print(json.dumps({"event":"init","conversation_id":conversation,"init":{"model":"fake"}}))
print(json.dumps({"event":"result","result":{"conversation_id":conversation,"status":"SUCCESS","response":"continued","usage":{}}}))
''') + [session, str(counter)]
    monkeypatch.setattr(agy, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'agy/gemini/fake',
                                    '--resume-session', session, '-p', 'delta-only',
                                    '--log-dir', str(tmp_path / 'logs')])
    result, code = cli.execute(args, path)
    assert code == 0 and counter.read_text() == '1'
    assert result['resumed'] is True and result['session_id'] == session


def test_agy_quota_blocks_provider_after_one_run(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'agy/gemini/fake', ['high'], 'high')
    registry.add(data, 'agy/gemini/neighbor')
    registry.save(path, data)
    counter = tmp_path / 'calls'
    command = fake_worker(tmp_path, '''import sys,pathlib,json
path=pathlib.Path(sys.argv[1])
path.write_text(path.read_text()+"1" if path.exists() else "1")
sys.stdin.read()
print(json.dumps({"type":"error","error":{"message":"RESOURCE_EXHAUSTED: Quota exceeded for metric GenerateContent"}}))
sys.exit(1)
''') + [str(counter)]
    monkeypatch.setattr(agy, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'agy/gemini/fake',
                                    '-p', 'task', '--log-dir', str(tmp_path / 'logs')])
    result, code = cli.execute(args, path)
    assert code == 1 and result['stop_reason'] == 'quota'
    assert counter.read_text() == '1'
    states = {r['address']: r['available'] for r in registry.available(registry.load(path))}
    assert not states['agy/gemini/fake'] and not states['agy/gemini/neighbor']


def test_resume_flag_in_help_and_skill_contract(tmp_path):
    import io
    from contextlib import redirect_stdout
    buffer = io.StringIO()
    with redirect_stdout(buffer), pytest.raises(SystemExit):
        cli.parser().parse_args(['exec', '--help'])
    assert '--resume-session' in buffer.getvalue()
    text = Path(__file__).parents[1].joinpath('SKILL.md').read_text(encoding='utf-8')
    assert '--resume-session' in text and '--continue' in text
    assert 'no other wrapper retries' in text and 'automatic resume' in text
    assert '--capacity-retries' in text and '--route-retries' in text


def test_help_documents_commands_arguments_and_examples():
    import io
    from contextlib import redirect_stdout

    def help_text(argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer), pytest.raises(SystemExit):
            cli.parser().parse_args(argv)
        return ' '.join(buffer.getvalue().split())

    root = help_text(['--help'])
    for command in ('list', 'add', 'on', 'off', 'delete', 'sync', 'hide', 'unhide', 'hidden',
                    'alias', 'exec', 'ask', 'test'):
        assert command in root
    for example in ('delegator add ', 'delegator list ', 'delegator sync ', 'delegator exec ',
                    'delegator alias add ', 'delegator ask ', 'delegator test '):
        assert example in root

    cases = {
        'list': ('List registry nodes', ['--active', '--json'],
                 'delegator list --active'),
        'add': ('Add an explicit', ['address', '--efforts', '--effort', '--override'],
                'delegator add codex/openai/gpt-6-luna'),
        'on': ('Enable a node', ['address'], 'delegator on opencode/openrouter'),
        'off': ('Disable a node', ['address'], 'delegator off openrouter'),
        'delete': ('Delete registry records', ['--recursive', '--yes', '--dry-run'],
                   'delegator delete opencode/openrouter --recursive --dry-run'),
        'sync': ('Show NEW, STALE, and HIDDEN', ['--add', '--prune', '--yes', '--json'],
                 'delegator sync --add 1,3'),
        'hide': ('Hide models or providers', ['address'], 'delegator hide codex/openai/gpt-5.6-terra'),
        'unhide': ('Stop hiding an address', ['address'], 'delegator unhide opencode/openrouter'),
        'hidden': ('List hidden addresses', ['--json'], 'delegator hidden'),
        'alias': ('ordered full model routes', ['add', 'remove', 'list'],
                  'delegator alias add glm-flash'),
        'exec': ('Start exactly one native worker',
                 ['--model', '--effort', '--prompt', '--prompt-file', '--repo', '--mode',
                  '--shell', '--log-dir', '--result-file', '--resume-session', '--capacity-retries',
                  '--capacity-retry-seconds', '--route-retries'],
                 'delegator exec -m codex/openai/gpt-6-luna'),
        'ask': ('One native turn for a single prompt',
                ['--model', '--effort', '--prompt', '--log-dir', '--json'],
                'delegator ask -m glm-flash'),
        'test': ('Offline validation of each model',
                 ['--model', '--effort', '--log-dir', '--json'],
                 'delegator test -m glm-flash'),
    }
    for command, (description, arguments, example) in cases.items():
        text = help_text([command, '--help'])
        assert description in text
        assert 'examples:' in text and example in text
        for argument in arguments:
            assert argument in text


def test_output_limited_failure_runs_once_and_does_not_block(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'opencode/test/fake', ['high'], 'high')
    registry.save(path, data)
    counter = tmp_path / 'calls'
    command = fake_worker(tmp_path, '''import sys,pathlib,json
path=pathlib.Path(sys.argv[1])
path.write_text(path.read_text()+"1" if path.exists() else "1")
sys.stdin.read()
print(json.dumps({"type":"text","part":{"type":"text","text":"Partial result"}}))
print(json.dumps({"type":"step_finish","part":{"reason":"length"}}))
''') + [str(counter)]
    monkeypatch.setattr(opencode, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'opencode/test/fake',
                                    '-p', 'task', '--log-dir', str(tmp_path / 'logs')])
    result, code = cli.execute(args, path)
    assert code == 1 and result['status'] == 'failed' and result['stop_reason'] == 'output_limited'
    assert counter.read_text() == '1'
    states = {r['address']: r['available'] for r in registry.available(registry.load(path))}
    assert states['opencode/test/fake']


def test_quota_failure_runs_once_and_saves_provider_block(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'codex/openai/fake', ['low'], 'low')
    registry.add(data, 'codex/openai/neighbor')
    registry.add(data, 'opencode/other/model')
    registry.save(path, data)
    counter = tmp_path / 'calls'
    command = fake_worker(tmp_path, '''import sys,pathlib,json
path=pathlib.Path(sys.argv[1])
path.write_text(path.read_text()+"1" if path.exists() else "1")
sys.stdin.read()
print(json.dumps({"type":"error","message":"You've hit your usage limit. Try again in 5 minutes."}))
sys.exit(1)
''') + [str(counter)]
    monkeypatch.setattr(codex, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'codex/openai/fake',
                                    '-p', 'task', '--log-dir', str(tmp_path / 'logs')])
    result, code = cli.execute(args, path)
    assert code == 1 and result['stop_reason'] == 'quota'
    assert counter.read_text() == '1'
    states = {r['address']: r['available'] for r in registry.available(registry.load(path))}
    assert not states['codex/openai/fake'] and not states['codex/openai/neighbor']
    assert states['opencode/other/model']


def test_sync_prune_address_and_all(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli.sources, 'read_sources', fake_source_map)
    data = registry.load(path)
    registry.add(data, 'opencode/p/ghost')
    # Legacy pre-rename slug: no longer offered by the Codex source, shows as STALE.
    registry.add(data, 'codex/default/ghost')
    registry.save(path, data)

    assert cli.main(['--config', str(path), 'sync', '--prune', 'opencode/p/ghost', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['applied'] == ['opencode/p/ghost']
    assert 'ghost' not in registry.load(path)['harnesses']['opencode']['providers']['p']['models']

    assert cli.main(['--config', str(path), 'sync', '--prune', 'all', '--yes', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['applied'] == ['codex/default/ghost'] and payload['skipped'] == []
    assert 'ghost' not in registry.load(path)['harnesses']['codex']['providers']['default']['models']


def test_sync_add_requires_candidate(tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli.sources, 'read_sources', fake_source_map)
    assert cli.main(['--config', str(path), 'sync', '--add', 'opencode/p/not-there']) == 2
    assert not path.exists()


def fake_source_map():
    return {
        'codex': cli.sources.SourceResult('codex', True, [
            {'address': 'codex/openai/a', 'favorite': False,
             'efforts': ['low'], 'default_effort': 'low'},
            {'address': 'codex/openai/b', 'favorite': False,
             'efforts': [], 'default_effort': None}]),
        'opencode': cli.sources.SourceResult('opencode', True, [
            {'address': 'opencode/p/m', 'favorite': True, 'efforts': [], 'default_effort': None},
            {'address': 'opencode/p/s/sub', 'favorite': True, 'efforts': [], 'default_effort': None}]),
        'zcode': cli.sources.SourceResult('zcode', True, [
            {'address': 'zcode/plan/GLM-A', 'favorite': False,
             'efforts': ['high'], 'default_effort': None}]),
        'agy': cli.sources.SourceResult('agy', True, [
            {'address': 'agy/gemini/gem', 'favorite': False, 'efforts': [], 'default_effort': None}]),
    }


def test_sync_report_add_prune_and_idempotency(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli.sources, 'read_sources', fake_source_map)
    assert cli.main(['--config', str(path), 'sync']) == 0
    out = capsys.readouterr().out
    assert 'NEW (6)' in out and 'STALE (0)' in out and 'planned' not in out

    assert cli.main(['--config', str(path), 'sync', '--add', 'codex/openai/a', '--json']) == 0
    model = registry.load(path)['harnesses']['codex']['providers']['openai']['models']['a']
    assert model['efforts'] == ['low'] and model['default_effort'] == 'low'

    data = registry.load(path)
    data['harnesses']['codex']['providers']['openai']['models']['a']['enabled'] = False
    registry.save(path, data)
    assert cli.main(['--config', str(path), 'sync', '--add', 'codex/openai/a', '--json']) == 0
    model = registry.load(path)['harnesses']['codex']['providers']['openai']['models']['a']
    assert model['enabled'] is False and model['efforts'] == ['low']

    data = registry.load(path)
    registry.add(data, 'opencode/p/ghost')
    registry.save(path, data)
    assert cli.main(['--config', str(path), 'sync', '--prune', '1', '--json']) == 0
    assert 'ghost' not in registry.load(path)['harnesses']['opencode']['providers']['p']['models']


def test_sync_all_adds_every_source_and_requires_yes(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli.sources, 'read_sources', fake_source_map)
    assert cli.main(['--config', str(path), 'sync', '--add', 'all']) == 2
    assert not path.exists()
    capsys.readouterr()
    assert cli.main(['--config', str(path), 'sync', '--add', 'all', '--yes', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['skipped'] == []
    assert len(payload['applied']) == 6
    data = registry.load(path)
    assert set(data['harnesses']) == {'codex', 'opencode', 'zcode', 'agy'}
    model = data['harnesses']['zcode']['providers']['plan']['models']['GLM-A']
    assert model['efforts'] == ['high'] and model['default_effort'] is None


def test_sync_guards_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli.sources, 'read_sources', fake_source_map)
    for argv in (['sync', '--add', '99'], ['sync', '--add', 'all,1'],
                 ['sync', '--prune', '1']):
        assert cli.main(['--config', str(path)] + argv) == 2
    assert not path.exists()
    with pytest.raises(SystemExit):
        cli.main(['--config', str(path), 'sync', '--add', '1', '--prune', '1'])
    with pytest.raises(SystemExit):
        cli.main(['--config', str(path), 'sync', '--hide', '1', '--add', '1'])


def test_sync_hide_indices_addresses_and_all(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli.sources, 'read_sources', fake_source_map)
    assert cli.main(['--config', str(path), 'sync', '--hide', '1', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['applied'] == ['agy/gemini/gem']
    assert registry.load(path)['hidden'] == ['agy/gemini/gem']

    assert cli.main(['--config', str(path), 'sync', '--hide', 'zcode/plan/GLM-A', '--json']) == 0
    assert registry.load(path)['hidden'] == ['agy/gemini/gem', 'zcode/plan/GLM-A']

    assert cli.main(['--config', str(path), 'sync', '--hide', 'all']) == 2
    capsys.readouterr()
    assert cli.main(['--config', str(path), 'sync', '--hide', 'all', '--yes', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    # Hidden entries are gone from NEW, so 'all' covers only the rest.
    assert payload['skipped'] == [] and len(payload['applied']) == 4
    data = registry.load(path)
    assert set(data['hidden']) == {'agy/gemini/gem', 'zcode/plan/GLM-A', 'codex/openai/a',
                                   'codex/openai/b', 'opencode/p/m', 'opencode/p/s/sub'}


def test_sync_hide_provider_prefix_and_guards(tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli.sources, 'read_sources', fake_source_map)
    assert cli.main(['--config', str(path), 'sync', '--hide', 'zcode/plan']) == 0
    assert registry.load(path)['hidden'] == ['zcode/plan']
    for argv in (['sync', '--hide', 'zcode/nope'], ['sync', '--hide', 'opencode/p/zzz'],
                 ['sync', '--hide', '1,zcode/plan']):
        assert cli.main(['--config', str(path)] + argv) == 2


def test_hide_rejects_glob_but_unhide_still_cleans(tmp_path):
    path = tmp_path / 'registry.json'
    assert cli.main(['--config', str(path), 'hide', 'zcode/account:bigmodel-*']) == 2
    assert not path.exists()
    data = registry.load(path)
    data['hidden'] = ['zcode/account:bigmodel-*']  # a pre-glob-guard entry
    registry.save(path, data)
    assert cli.main(['--config', str(path), 'unhide', 'zcode/account:bigmodel-*']) == 0
    assert registry.load(path)['hidden'] == []


def test_sync_add_accepts_argument_array_tokens(tmp_path, monkeypatch, capsys):
    # PowerShell turns an unquoted `--add 1,3` into separate arguments.
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli.sources, 'read_sources', fake_source_map)
    assert cli.main(['--config', str(path), 'sync', '--add', '1', '3', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['applied'] == ['agy/gemini/gem', 'codex/openai/b']
    # After that add, NEW renumbers: 1 codex/openai/a, 2 opencode/p/m, 3 opencode/p/s/sub.
    assert cli.main(['--config', str(path), 'sync', '--hide', '2', '3', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['applied'] == ['opencode/p/m', 'opencode/p/s/sub']


def test_hide_unhide_and_hidden_roundtrip(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'opencode/p/m')
    registry.save(path, data)
    assert cli.main(['--config', str(path), 'hide', 'codex/openai/a']) == 0
    assert registry.load(path)['hidden'] == ['codex/openai/a']
    assert cli.main(['--config', str(path), 'hide', 'codex/openai/a']) == 0
    assert registry.load(path)['hidden'] == ['codex/openai/a']
    capsys.readouterr()
    assert cli.main(['--config', str(path), 'hidden', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['hidden'] == ['codex/openai/a']
    assert cli.main(['--config', str(path), 'hide', '/codex/openai/b']) == 0
    assert registry.load(path)['hidden'] == ['codex/openai/a', 'codex/openai/b']
    assert cli.main(['--config', str(path), 'unhide', 'codex/openai/a']) == 0
    assert registry.load(path)['hidden'] == ['codex/openai/b']
    assert cli.main(['--config', str(path), 'hide', 'opencode/p/m']) == 2
    with pytest.raises(SystemExit):
        cli.main(['--config', str(path), 'hidden', 'unexpected'])


def test_sync_add_exact_hidden_model_unhides_it(tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli.sources, 'read_sources', fake_source_map)
    assert cli.main(['--config', str(path), 'hide', 'codex/openai/a']) == 0
    assert cli.main(['--config', str(path), 'sync', '--add', 'codex/openai/a', '--json']) == 0
    data = registry.load(path)
    assert data['hidden'] == []
    assert 'a' in data['harnesses']['codex']['providers']['openai']['models']


OC_SESSION = 'ses_e96749c8dffeisGoO7xaRP74Hg'


def oc_tool(tool, status, error=None):
    state = {'status': status, 'input': {}}
    if error:
        state['error'] = error
    return json.dumps({'type': 'tool_use', 'sessionID': OC_SESSION,
                       'part': {'type': 'tool', 'tool': tool, 'state': state}})


OC_RULE_DENIAL = ('The user has specified a rule which prevents you from using this specific tool call. '
                  'Here are some of the relevant rules [{"permission":"external_directory","action":"deny"}]')
OC_ASK_REJECTION = 'The user rejected permission to use this specific tool call.'


def worker_printing(tmp_path, lines):
    return fake_worker(tmp_path, 'import sys\nsys.stdin.read()\n'
                       + ''.join('print(' + repr(line) + ')\n' for line in lines))


def test_opencode_overcome_denial_completes_with_denied_actions(repo, tmp_path):
    command = worker_printing(tmp_path, [
        oc_tool('read', 'error', OC_RULE_DENIAL),
        oc_tool('read', 'completed'),
        json.dumps({'type': 'text', 'part': {'text': 'Verdict: no defects.'}}),
        json.dumps({'type': 'step_finish', 'part': {'reason': 'stop'}})])
    result = runner.run(opencode, command, {}, repo, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed' and result['stop_reason'] == 'completed'
    assert result['answer'] == 'Verdict: no defects.' and result['reason'] is None
    assert [item['tool'] for item in result['denied_actions']] == ['read']
    assert 'prevents you from using' in result['denied_actions'][0]['detail']


def test_opencode_denial_that_stopped_the_turn_needs_user_input(repo, tmp_path):
    # Headless ask auto-rejection: the native ends the turn on tool-calls without an answer.
    command = worker_printing(tmp_path, [
        oc_tool('bash', 'error', OC_ASK_REJECTION),
        json.dumps({'type': 'step_finish', 'part': {'reason': 'tool-calls'}})])
    result = runner.run(opencode, command, {}, repo, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'failed' and result['stop_reason'] == 'needs_user_input'
    assert result['reason'] == 'Native tool permission denied'
    assert [item['tool'] for item in result['denied_actions']] == ['bash']


def test_ordinary_tool_error_is_not_a_denial(repo, tmp_path):
    command = worker_printing(tmp_path, [
        oc_tool('bash', 'error', 'Command exited with code 1'),
        json.dumps({'type': 'text', 'part': {'text': 'Tests fail.'}}),
        json.dumps({'type': 'step_finish', 'part': {'reason': 'stop'}})])
    result = runner.run(opencode, command, {}, repo, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed' and result['denied_actions'] == []


AGY_DENIED = [{'action': 'command', 'display_name': 'RunCommand'}]


@pytest.mark.parametrize('response,status,stop_reason', [
    ('Edited the file; the command was not run.', 'completed', 'completed'),
    ('', 'failed', 'needs_user_input')])
def test_agy_denied_actions_kept_and_classified_by_answer(repo, tmp_path, response, status, stop_reason):
    command = worker_printing(tmp_path, [json.dumps({
        'event': 'result', 'result': {'conversation_id': 'c1', 'status': 'SUCCESS',
                                      'response': response, 'denied_actions': AGY_DENIED}})])
    result = runner.run(agy, command, {}, repo, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == status and result['stop_reason'] == stop_reason
    assert [json.loads(item['detail']) for item in result['denied_actions']] == AGY_DENIED


def test_denied_actions_are_capped_and_redacted():
    summary = native.Summary()
    for _ in range(native.MAX_DENIED_ACTIONS + 5):
        summary.consume(opencode, 'stdout', oc_tool('bash', 'error', OC_ASK_REJECTION + ' token=abc123'))
    assert len(summary.denied_actions) == native.MAX_DENIED_ACTIONS
    assert 'abc123' not in summary.denied_actions[0]['detail']


def test_codex_policy_rejection_is_a_denial_not_a_failure():
    # Router log line shape from a live workspace-write run (escaping simplified).
    line = ('2026-10-24T06:41:05.593160Z ERROR codex_core::tools::router: error=exec_command failed: '
            'CreateProcess { message: "Rejected(\\"`\\\\\\"pwsh.exe\\\\\\" -Command \\\\\\"Remove-Item '
            '-Recurse C:/outside\\\\\\"` rejected: blocked by policy\\")" }')
    summary = native.Summary()
    summary.consume(codex, 'stderr', line)
    assert summary.reason is None
    assert summary.denied_actions == [{'tool': 'exec_command', 'detail':
                                       'blocked by policy: pwsh.exe" -Command "Remove-Item -Recurse C:/outside'}]


def test_zcode_busy_marker_is_capacity_with_false_marker_staying_clean():
    summary = native.Summary()
    summary.consume(codex, 'stderr',
                    'ERROR: The system is busy, and automatic retries have reached the maximum count. '
                    'Please try again later or upgrade your account.')
    assert summary.reason == 'capacity'
    summary = native.Summary()
    summary.consume(codex, 'stderr', 'the system is busy today, the answer said in prose')
    assert summary.reason is None


def test_transient_server_error_markers_are_capacity():
    for marker in ('Provider returned a server error.',
                   'server_error',
                   'Internal server error',
                   '503 Service Unavailable',
                   'bad gateway',
                   'gateway timeout',
                   'The model is overloaded. Please try again later.',
                   'Network connection failed for the provider request.'):
        summary = native.Summary()
        summary.consume(codex, 'stderr', 'ERROR: ' + marker)
        assert summary.reason == 'capacity'
    summary = native.Summary()
    summary.consume(codex, 'stderr', 'the service was unavailable last week, the answer said')
    assert summary.reason is None
    summary = native.Summary()
    summary.consume(codex, 'stdout', json.dumps(
        {'type': 'error', 'message': 'HTTP 429 Too Many Requests'}))
    assert summary.reason == 'native_error'


def test_execution_capacity_retry_keys_optional_and_validated(tmp_path):
    path = tmp_path / 'config.json'
    legacy = {'version': 1, 'harnesses': {},
              'execution': {'status_interval_seconds': 1800, 'max_runtime_seconds': None},
              'hidden': [], 'aliases': {}}
    path.write_text(json.dumps(legacy), encoding='utf-8')
    assert registry.load(path) == legacy
    data = registry.load(path)
    data['execution'].update(capacity_retries=1, capacity_retry_seconds=5)
    registry.save(path, data)
    execution = registry.load(path)['execution']
    assert execution['capacity_retries'] == 1 and execution['capacity_retry_seconds'] == 5
    for bad in ({'capacity_retries': -1}, {'capacity_retries': 1.5}, {'capacity_retries': True},
                {'capacity_retry_seconds': 0}, {'capacity_retry_seconds': 'x'}):
        document = {'version': 1, 'harnesses': {},
                    'execution': {'status_interval_seconds': 1800, 'max_runtime_seconds': None,
                                  'capacity_retries': 3, 'capacity_retry_seconds': 30},
                    'hidden': []}
        document['execution'].update(bad)
        path.write_text(json.dumps(document), encoding='utf-8')
        with pytest.raises(registry.ConfigError):
            registry.load(path)


def test_exec_capacity_flags_parse():
    args = cli.parser().parse_args(['exec', '-m', 'codex/openai/m', '--capacity-retries', '0',
                                    '--capacity-retry-seconds', '5', '-p', 'x'])
    assert args.capacity_retries == 0 and args.capacity_retry_seconds == 5.0
    args = cli.parser().parse_args(['exec', '-m', 'codex/openai/m', '-p', 'x'])
    assert args.capacity_retries is None and args.capacity_retry_seconds is None


class RecordingAdapter:
    def __init__(self):
        self.resumes = []
        self.shells = []

    def command(self, executable, repo, provider, model, effort, mode, shell,
                resume_session=None, git_check=True, override=False):
        self.resumes.append(resume_session)
        self.shells.append(shell)
        return ['worker'], {}, None


def retry_result(stop_reason='capacity', session_id=None, log='run.jsonl', usage=None,
                 pre=None, changed=None):
    completed = stop_reason == 'completed'
    status = 'completed' if completed else ('needs_input' if stop_reason == 'worker_question'
                                            else 'failed')
    return {'status': status, 'stop_reason': stop_reason,
            'answer': 'ok' if completed else '', 'reason': None,
            'reported': dict(harness=None, provider=None, model=None, effort=None),
            'session_id': session_id, 'resumed': False, 'usage': usage,
            'elapsed_seconds': 1.0, 'warnings': [], 'log_path': log,
            'changed_paths': changed or [], 'preexisting_changed_paths': pre or [],
            'quota_reset_at': None, 'native_exit_code': 0 if completed else 1}


def test_capacity_retry_resumes_observed_session_and_merges(tmp_path, monkeypatch):
    adapter = RecordingAdapter()
    results = [retry_result(session_id='sess-1', log='a.jsonl', usage={'input_tokens': 2},
                            pre=['preexisting.txt']),
               retry_result('completed', session_id='sess-1', log='b.jsonl',
                            usage={'input_tokens': 3}, changed=['new.txt'])]
    monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
    waits = []
    monkeypatch.setattr(cli, 'retry_wait', waits.append)
    result = cli.run_with_capacity_retries(adapter, 'w', tmp_path, 'openai', 'm', None, 'review', 'auto',
                                           'task', 'target', 0, None, None, None, 3, 30)
    assert result['status'] == 'completed' and result['attempts'] == 2
    assert waits == [30]
    assert adapter.resumes == [None, 'sess-1'] and adapter.shells == ['auto', 'auto']
    assert result['log_paths'] == ['a.jsonl', 'b.jsonl'] and result['log_path'] == 'b.jsonl'
    assert result['preexisting_changed_paths'] == ['preexisting.txt']
    assert result['usage'] == {'input_tokens': 5}
    assert result['resumed'] is False
    assert any('Attempt 1 failed (capacity)' in w for w in result['warnings'])


def test_capacity_retries_exhausted_with_progressive_waits(tmp_path, monkeypatch):
    adapter = RecordingAdapter()
    results = [retry_result(session_id=f's{i}', log=f'{i}.jsonl') for i in range(4)]
    monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
    waits = []
    monkeypatch.setattr(cli, 'retry_wait', waits.append)
    result = cli.run_with_capacity_retries(adapter, 'w', tmp_path, 'openai', 'm', None, 'review', 'auto',
                                           'task', 'target', 0, None, None, None, 3, 30)
    assert result['status'] == 'failed' and result['stop_reason'] == 'capacity'
    assert result['attempts'] == 4 and waits == [30, 60, 120]
    assert adapter.resumes == [None, 's0', 's1', 's2']


def test_no_retry_for_non_capacity_reasons_and_disabled_retries(tmp_path, monkeypatch):
    for stop in ('quota', 'auth', 'native_error', 'needs_user_input', 'no_terminal_answer'):
        adapter = RecordingAdapter()
        results = [retry_result(stop_reason=stop)]
        monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
        monkeypatch.setattr(cli, 'retry_wait', lambda s: pytest.fail('Must not wait'))
        result = cli.run_with_capacity_retries(adapter, 'w', tmp_path, 'openai', 'm', None,
                                               'review', 'auto', 'task', 'target', 0, None, None, None, 3, 30)
        assert result['attempts'] == 1 and adapter.resumes == [None]
    adapter = RecordingAdapter()
    results = [retry_result()]
    monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
    result = cli.run_with_capacity_retries(adapter, 'w', tmp_path, 'openai', 'm', None,
                                           'review', 'auto', 'task', 'target', 0, None, None, None, 0, 30)
    assert result['attempts'] == 1 and result['stop_reason'] == 'capacity'


def test_capacity_retry_fresh_without_session_and_user_resume_fallback(tmp_path, monkeypatch):
    adapter = RecordingAdapter()
    results = [retry_result(session_id=None, log='a.jsonl'),
               retry_result('completed', log='b.jsonl')]
    monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
    waits = []
    monkeypatch.setattr(cli, 'retry_wait', waits.append)
    result = cli.run_with_capacity_retries(adapter, 'w', tmp_path, 'openai', 'm', None, 'review', 'auto',
                                           'task', 'target', 0, None, None, None, 3, 30)
    assert adapter.resumes == [None, None] and result['attempts'] == 2
    adapter = RecordingAdapter()
    results = [retry_result(session_id=None, log='a.jsonl'),
               retry_result('completed', log='b.jsonl')]
    monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
    result = cli.run_with_capacity_retries(adapter, 'w', tmp_path, 'openai', 'm', None, 'review', 'auto',
                                           'task', 'target', 0, None, None, 'user-sess', 3, 30)
    assert adapter.resumes == ['user-sess', 'user-sess']


def test_capacity_retry_stops_when_runtime_budget_is_spent(tmp_path, monkeypatch):
    adapter = RecordingAdapter()
    results = [retry_result(log='a.jsonl')]
    monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
    monkeypatch.setattr(cli, 'retry_wait', lambda s: pytest.fail('Must not wait'))
    ticks = iter([0.0, 5.0, 5.0, 5.0])
    monkeypatch.setattr(cli.time, 'monotonic', lambda: next(ticks))
    result = cli.run_with_capacity_retries(adapter, 'w', tmp_path, 'openai', 'm', None, 'review', 'auto',
                                           'task', 'target', 0, 1, None, None, 3, 30)
    assert result['attempts'] == 1
    assert any('runtime budget is spent' in w for w in result['warnings'])


def test_capacity_retry_end_to_end_with_real_worker(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'codex/openai/fake', ['low'], 'low')
    registry.save(path, data)
    session = '12345678-1234-1234-1234-1234567890ab'
    marker = tmp_path / 'attempted'
    command = fake_worker(tmp_path, '''import sys,pathlib,json
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"''' + session + '''"}))
path=pathlib.Path(sys.argv[1])
if path.exists():
    print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}))
    print(json.dumps({"type":"turn.completed"}))
else:
    path.write_text("1")
    print("ERROR: Selected model is at capacity. Please try a different model.", file=sys.stderr)
    sys.exit(1)
''') + [str(marker)]
    monkeypatch.setattr(codex, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    waits = []
    monkeypatch.setattr(cli, 'retry_wait', waits.append)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'codex/openai/fake',
                                    '-p', 'task', '--log-dir', str(tmp_path / 'logs')])
    result, code = cli.execute(args, path)
    assert code == 0 and result['status'] == 'completed' and result['answer'] == 'done'
    assert result['attempts'] == 2 and waits == [30]
    assert result['session_id'] == session and result['resumed'] is False
    assert len(result['log_paths']) == 2 and result['log_path'] == result['log_paths'][-1]
    assert any('Attempt 1 failed (capacity)' in w for w in result['warnings'])


@pytest.mark.skipif(os.name != 'nt', reason='installer requires Windows PowerShell')
def test_installer_writes_launcher_and_quota_cmd(tmp_path):
    bin_dir = tmp_path / 'bin'
    subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', str(SCRIPTS / 'install.ps1'), '-BinDir', str(bin_dir)],
                   check=True, capture_output=True, timeout=120)
    launcher_path = bin_dir / 'delegator.ps1'
    cmd_path = bin_dir / 'delegator-quota.cmd'
    assert launcher_path.exists() and cmd_path.exists()

    launcher_text = launcher_path.read_text(encoding='utf-8-sig')
    entrypoint = str((SCRIPTS / 'delegator.py').resolve()).casefold()
    assert f"'{entrypoint}'" in launcher_text.casefold()

    raw = cmd_path.read_bytes()
    assert b'\r\n' in raw and b'\n' not in raw.replace(b'\r\n', b'')
    cmd_text = raw.decode('ascii')
    low = cmd_text.casefold()
    assert 'quota --format html' in low
    launcher_ref = str(launcher_path.resolve()).casefold()
    assert f'"{launcher_ref}"' in low
    out_ref = str(Path.home() / '.agents' / 'delegator-quota.html').casefold()
    # The written file and the opened file must be the same path.
    assert low.count(out_ref) == 2
    assert 'if %errorlevel%==0' in low and 'start ""' in low and 'pause' in low
# --- Aliases and route chains (issue #101) ---


def alias_registry(tmp_path, addresses):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    for address in addresses:
        registry.add(data, address, ['low', 'high'], 'low')
    registry.save(path, data)
    return path


def test_alias_commands_comma_array_and_guards(tmp_path, capsys):
    path = alias_registry(tmp_path, ['opencode/test/fake', 'opencode/other/fake'])
    assert cli.main(['--config', str(path), 'alias', 'add', 'dsk',
                     'opencode/test/fake,opencode/other/fake']) == 0
    assert cli.main(['--config', str(path), 'alias', 'add', 'dsk2',
                     'opencode/test/fake', 'opencode/other/fake']) == 0
    data = registry.load(path)
    routes = ['opencode/test/fake', 'opencode/other/fake']
    assert data['aliases'] == {'dsk': {'routes': routes}, 'dsk2': {'routes': routes}}
    capsys.readouterr()
    assert cli.main(['--config', str(path), 'alias', 'list', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['aliases'] == {'dsk': routes, 'dsk2': routes}
    assert cli.main(['--config', str(path), 'alias', 'add', 'bad',
                     'opencode/test/unregistered']) == 2
    assert cli.main(['--config', str(path), 'alias', 'add', 'bad/name',
                     'opencode/test/fake']) == 2
    assert cli.main(['--config', str(path), 'alias', 'add', 'bad',
                     'opencode/test/fake,opencode/other/fake,opencode/test/fake']) == 2
    assert 'bad' not in registry.load(path)['aliases']
    assert cli.main(['--config', str(path), 'alias', 'remove', 'dsk']) == 0
    assert cli.main(['--config', str(path), 'alias', 'remove', 'dsk']) == 0
    assert list(registry.load(path)['aliases']) == ['dsk2']


def test_delete_warns_when_alias_references_deleted_address(tmp_path, capsys):
    path = alias_registry(tmp_path, ['opencode/test/fake', 'opencode/other/fake'])
    data = registry.load(path)
    registry.alias_set(data, 'dsk', ['opencode/test/fake', 'opencode/other/fake'])
    registry.save(path, data)
    assert cli.main(['--config', str(path), 'delete', 'opencode/test/fake', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['addresses'] == ['opencode/test/fake']
    assert any('alias dsk still references a deleted address' in w for w in payload['warnings'])
    assert cli.main(['--config', str(path), 'delete', 'opencode', '--recursive', '--yes',
                     '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any('alias dsk still references a deleted address' in w for w in payload['warnings'])


def test_sync_prune_warns_when_alias_references_pruned_address(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(cli.sources, 'read_sources', fake_source_map)
    data = registry.load(path)
    registry.add(data, 'opencode/p/ghost')
    registry.add(data, 'codex/openai/a')
    registry.alias_set(data, 'dsk', ['opencode/p/ghost', 'codex/openai/a'])
    registry.save(path, data)
    assert cli.main(['--config', str(path), 'sync', '--prune', 'opencode/p/ghost', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['applied'] == ['opencode/p/ghost']
    assert any('alias dsk still references a deleted address' in w for w in payload['warnings'])


def test_exec_unknown_alias_lists_defined_aliases(tmp_path, capsys):
    path = alias_registry(tmp_path, ['opencode/test/fake'])
    data = registry.load(path)
    registry.alias_set(data, 'dsk', ['opencode/test/fake'])
    registry.save(path, data)
    args = cli.parser().parse_args(['exec', '-m', 'nosuch', '-p', 'task'])
    result, code = cli.execute(args, path)
    assert code == 2 and result['status'] == 'configuration_error'
    assert result['code'] == 'unknown_alias'
    assert result['aliases'] == ['dsk']
    assert 'Unknown alias' in result['reason']


def test_test_unknown_alias_reports_code(tmp_path, capsys):
    path = alias_registry(tmp_path, ['opencode/test/fake'])
    data = registry.load(path)
    registry.alias_set(data, 'dsk', ['opencode/test/fake'])
    registry.save(path, data)
    assert cli.main(['--config', str(path), 'test', '-m', 'nosuch']) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'configuration_error'
    assert payload['code'] == 'unknown_alias'
    assert payload['aliases'] == ['dsk']
    assert 'Unknown alias' in payload['reason']


def test_exec_alias_unavailable_routes_reported_with_reasons(repo, tmp_path):
    path = alias_registry(tmp_path, ['opencode/test/fake', 'opencode/other/fake'])
    data = registry.load(path)
    registry.alias_set(data, 'dsk', ['opencode/test/fake', 'opencode/other/fake'])
    data['harnesses']['opencode']['providers']['test']['enabled'] = False
    data['harnesses']['opencode']['providers']['other']['quota']['blocked'] = True
    registry.save(path, data)
    args = cli.parser().parse_args(['exec', '-m', 'dsk', '-p', 'task', '--repo', str(repo)])
    result, code = cli.execute(args, path)
    assert code == 2 and result['status'] == 'configuration_error'
    assert result['code'] == 'no_available_route'
    assert result['alias'] == 'dsk'
    assert [r['available'] for r in result['routes']] == [False, False]
    assert 'disabled' in result['routes'][0]['reason']
    assert 'quota' in result['routes'][1]['reason'].lower()


def test_exec_alias_effort_filter_and_guards(repo, tmp_path, monkeypatch):
    path = alias_registry(tmp_path, ['codex/openai/a', 'codex/openai/b'])
    data = registry.load(path)
    data['harnesses']['codex']['providers']['openai']['models']['b']['efforts'] = ['high']
    data['harnesses']['codex']['providers']['openai']['models']['b']['default_effort'] = 'high'
    registry.alias_set(data, 'dsk', ['codex/openai/a', 'codex/openai/b'])
    registry.save(path, data)
    args = cli.parser().parse_args(['exec', '-m', 'dsk', '-e', 'high', '-p', 'task',
                                    '--repo', str(repo)])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: None)
    result, code = cli.execute(args, path)
    assert code == 2 and result['reason'] == 'No route of the alias was run'
    assert any('Route codex/openai/b skipped: native CLI is not installed: codex' in w
               for w in result['warnings'])
    data = registry.load(path)
    data['harnesses']['codex']['providers']['openai']['models']['a']['efforts'] = ['low']
    data['harnesses']['codex']['providers']['openai']['models']['b']['efforts'] = ['medium']
    data['harnesses']['codex']['providers']['openai']['models']['b']['default_effort'] = 'medium'
    registry.save(path, data)
    args = cli.parser().parse_args(['exec', '-m', 'dsk', '-e', 'high', '-p', 'task',
                                    '--repo', str(repo)])
    result, code = cli.execute(args, path)
    assert code == 2 and result['status'] == 'configuration_error'
    assert [r['available'] for r in result['routes']] == [False, False]
    assert 'high' in result['routes'][0]['reason'] and 'high' in result['routes'][1]['reason']
    args = cli.parser().parse_args(['exec', '-m', 'dsk', '--resume-session', 'x', '-p', 't'])
    with pytest.raises(registry.ConfigError):
        cli.execute(args, path)
    args = cli.parser().parse_args(['exec', '-m', 'dsk', '-p', 't', '--capacity-retries', '1'])
    with pytest.raises(registry.ConfigError):
        cli.execute(args, path)
    args = cli.parser().parse_args(['exec', '-m', 'codex/openai/a', '-p', 't',
                                    '--route-retries', '1'])
    with pytest.raises(registry.ConfigError):
        cli.execute(args, path)
    args = cli.parser().parse_args(['exec', '-m', 'codex/openai/a', '-p', 't'])
    assert args.route_retries is None


def prepare_chain(tmp_path, monkeypatch, results, candidates, route_retries=1, path=None,
                  command=None):
    waits = []
    monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
    monkeypatch.setattr(cli, 'retry_wait', waits.append)
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    monkeypatch.setattr(codex, 'command',
                        command or (lambda *a, **k: (['worker'], {}, None)))
    args = cli.parser().parse_args(['exec', '-m', 'dsk', '-p', 'task',
                                    '--route-retries', str(route_retries)])
    result, code = cli.run_route_chain(args, path or (tmp_path / 'registry.json'), 'dsk',
                                       candidates, 'auto', tmp_path, 'task', 0, None,
                                       route_retries, 30)
    return result, code, waits


def chain_candidates(*addresses):
    return [{'address': a, 'harness': a.split('/')[0], 'provider': a.split('/')[1],
             'model': a.split('/', 2)[2], 'effort': 'low'} for a in addresses]


def test_route_chain_capacity_fails_over_and_aggregates(tmp_path, monkeypatch):
    results = [retry_result('capacity', session_id='s1', log='a1.jsonl', usage={'input_tokens': 2},
                            pre=['pre.txt']),
               retry_result('capacity', session_id='s2', log='a2.jsonl', usage={'input_tokens': 3},
                            pre=['pre.txt'], changed=['pre.txt', 'edit.txt']),
               retry_result('completed', log='b1.jsonl', usage={'input_tokens': 5})]
    result, code, waits = prepare_chain(
        tmp_path, monkeypatch, results, chain_candidates('codex/openai/a', 'codex/other/b'))
    assert code == 0 and result['status'] == 'completed'
    assert result['attempts'] == 3 and result['attempt_routes'] == [
        'codex/openai/a', 'codex/openai/a', 'codex/other/b']
    assert result['log_paths'] == ['a1.jsonl', 'a2.jsonl', 'b1.jsonl']
    assert result['usage'] == {'input_tokens': 10}
    assert result['preexisting_changed_paths'] == ['pre.txt']
    assert result['alias'] == 'dsk'
    assert result['requested'] == {'harness': 'codex', 'provider': 'other', 'model': 'b',
                                   'effort': 'low', 'shell': 'auto'}
    assert waits == [30]
    assert any('Route codex/openai/a failed (capacity); continuing with the next route'
               in w and 'edit.txt' in w for w in result['warnings'])


def test_route_chain_quota_blocks_provider_and_skips_same_provider(tmp_path, monkeypatch):
    path = alias_registry(tmp_path, ['codex/openai/a', 'codex/openai/b', 'codex/other/c'])
    results = [retry_result('quota', log='q.jsonl'), retry_result('completed', log='c.jsonl')]
    result, code, waits = prepare_chain(
        tmp_path, monkeypatch, results,
        chain_candidates('codex/openai/a', 'codex/openai/b', 'codex/other/c'), route_retries=0,
        path=path)
    assert code == 0
    assert result['attempts'] == 2
    assert result['attempt_routes'] == ['codex/openai/a', 'codex/other/c']
    assert waits == []
    blocked = registry.load(path)['harnesses']['codex']['providers']['openai']['quota']
    assert blocked['blocked'] is True
    assert any('Route codex/openai/b skipped: provider quota is blocked' in w
               for w in result['warnings'])
    assert any('Route codex/openai/a failed (quota)' in w for w in result['warnings'])


def test_route_chain_stops_on_other_outcomes(tmp_path, monkeypatch):
    for stop in ('needs_user_input', 'output_limited', 'cancelled'):
        results = [retry_result(stop, log='a.jsonl'),
                   retry_result('completed', log='b.jsonl')]
        result, code, waits = prepare_chain(
            tmp_path, monkeypatch, results,
            chain_candidates('codex/openai/a', 'codex/other/b'), route_retries=0)
        assert code == 1 and result['attempts'] == 1 and result['log_paths'] == ['a.jsonl']
        assert waits == []
        assert result['attempt_routes'] == ['codex/openai/a']


def test_route_chain_stops_on_worker_question(tmp_path, monkeypatch):
    results = [retry_result('worker_question', session_id='sess-1', log='a.jsonl'),
               retry_result('completed', log='b.jsonl')]
    result, code, waits = prepare_chain(
        tmp_path, monkeypatch, results,
        chain_candidates('codex/openai/a', 'codex/other/b'), route_retries=0)
    assert code == 3 and result['status'] == 'needs_input'
    assert result['stop_reason'] == 'worker_question'
    assert result['attempts'] == 1 and result['log_paths'] == ['a.jsonl']
    assert result['session_id'] == 'sess-1'
    assert result['attempt_routes'] == ['codex/openai/a']
    assert waits == []


def test_route_chain_budget_spent_before_next_route(tmp_path, monkeypatch):
    results = [retry_result('capacity', log='a.jsonl')]
    monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
    monkeypatch.setattr(cli, 'retry_wait', lambda s: pytest.fail('Must not wait'))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['worker'], {}, None))
    ticks = iter([0.0, 0.0, 0.0, 0.0, 5.0])
    monkeypatch.setattr(cli.time, 'monotonic', lambda: next(ticks))
    args = cli.parser().parse_args(['exec', '-m', 'dsk', '-p', 'task', '--route-retries', '0'])
    result, code = cli.run_route_chain(args, tmp_path / 'registry.json', 'dsk',
                                       chain_candidates('codex/openai/a', 'codex/other/b'),
                                       'auto', tmp_path, 'task', 0, 1, 0, 30)
    assert code == 1 and result['attempts'] == 1
    assert any('Route codex/other/b skipped: the whole-call runtime budget is spent' in w
               for w in result['warnings'])


def test_route_chain_skips_preparation_failures(tmp_path, monkeypatch):
    # A zcode route without an explicit or default effort is skipped; the codex route runs.
    results = [retry_result('completed', log='b.jsonl')]
    monkeypatch.setattr(cli, 'run', lambda *a, **k: results.pop(0))
    monkeypatch.setattr(cli, 'retry_wait', lambda s: pytest.fail('Must not wait'))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['worker'], {}, None))
    candidates = [{'address': 'zcode/plan/glm', 'harness': 'zcode', 'provider': 'plan',
                   'model': 'glm', 'effort': None},
                  {'address': 'codex/openai/a', 'harness': 'codex', 'provider': 'openai',
                   'model': 'a', 'effort': 'low'}]
    args = cli.parser().parse_args(['exec', '-m', 'dsk', '-p', 'task'])
    result, code = cli.run_route_chain(args, tmp_path / 'registry.json', 'dsk', candidates,
                                       'auto', tmp_path, 'task', 0, None, 1, 30)
    assert code == 0 and result['attempts'] == 1
    assert any('Route zcode/plan/glm skipped' in w for w in result['warnings'])
    assert result['attempt_routes'] == ['codex/openai/a']
    # A route whose native CLI is missing is skipped with a warning.
    results = [retry_result('completed', log='b.jsonl')]
    monkeypatch.setattr(cli.shutil, 'which',
                        lambda name: None if name == 'agy' else sys.executable)
    result, code = cli.run_route_chain(args, tmp_path / 'registry.json', 'dsk',
                                       chain_candidates('agy/gemini/gem', 'codex/openai/a'),
                                       'auto', tmp_path, 'task', 0, None, 1, 30)
    assert code == 0
    assert any('Route agy/gemini/gem skipped: native CLI is not installed: agy' in w
               for w in result['warnings'])
    assert result['attempt_routes'] == ['codex/openai/a']


def test_route_chain_skips_adapter_preparation_failure(tmp_path, monkeypatch):
    # Issue #110: a preparation failure raised inside the harness adapter
    # (here the model `a` command) is skipped with a warning; the chain runs.
    def command(executable, repo_path, provider, model, effort, mode, shell, resume,
                git_check=True, override=False):
        if model == 'a':
            raise ValueError('ZCode plan key unavailable: provider=account:x')
        return ['worker'], {}, None

    results = [retry_result('completed', log='b1.jsonl')]
    result, code, waits = prepare_chain(
        tmp_path, monkeypatch, results, chain_candidates('codex/openai/a', 'codex/other/b'),
        route_retries=0, command=command)
    assert code == 0 and result['status'] == 'completed'
    assert result['attempt_routes'] == ['codex/other/b']
    assert result['requested'] == {'harness': 'codex', 'provider': 'other', 'model': 'b',
                                   'effort': 'low', 'shell': 'auto'}
    assert waits == []
    assert any('Route codex/openai/a skipped: ZCode plan key unavailable: provider=account:x'
               in w for w in result['warnings'])


def test_route_chain_all_routes_fail_adapter_preparation(tmp_path, monkeypatch):
    def command(*args, **kwargs):
        raise ValueError('cannot decrypt ZCode credential')

    result, code, waits = prepare_chain(
        tmp_path, monkeypatch, [], chain_candidates('codex/openai/a', 'codex/other/b'),
        route_retries=0, command=command)
    assert code == 2 and result['status'] == 'configuration_error'
    assert result['code'] == 'no_route_ran'
    assert result['reason'] == 'No route of the alias was run'
    assert any('Route codex/openai/a skipped: cannot decrypt ZCode credential' in w
               for w in result['warnings'])
    assert any('Route codex/other/b skipped: cannot decrypt ZCode credential' in w
               for w in result['warnings'])


def test_route_chain_all_routes_blocked_by_environment(tmp_path, monkeypatch):
    # Issue #112: when every route was skipped by a blocked write, the reason
    # names the calling environment; per-route specifics stay in warnings.
    def command(*args, **kwargs):
        raise ValueError('ZCode home is not writable: C:\\Users\\x\\.zcode; '
                         'invoke outside a restricted sandbox')

    result, code, waits = prepare_chain(
        tmp_path, monkeypatch, [], chain_candidates('codex/openai/a', 'codex/other/b'),
        route_retries=0, command=command)
    assert code == 2 and result['status'] == 'configuration_error'
    assert result['code'] == 'environment_blocked'
    assert result['reason'].startswith('The calling environment blocks writes')
    assert 'restricted sandbox' in result['reason']
    assert any('Route codex/openai/a skipped: ZCode home is not writable' in w
               for w in result['warnings'])


def test_profile_canary_blocks_exec_before_any_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'profile_is_writable', lambda home=None: False)
    args = cli.parser().parse_args(['exec', '-m', 'codex/openai/fake', '-p', 'Hi'])
    result, code = cli.execute(args, tmp_path / 'unused.json')
    assert code == 2 and result['status'] == 'configuration_error'
    assert result['code'] == 'profile_not_writable'
    assert result['reason'].startswith('The calling environment blocks writes')
    assert 'restricted sandbox' in result['reason']


def test_profile_canary_probe_mechanics(tmp_path):
    assert cli.profile_is_writable(tmp_path) is True
    (tmp_path / f'.delegator-write-probe-{os.getpid()}').mkdir()
    assert cli.profile_is_writable(tmp_path) is False
    # The probe never creates parents: a missing profile is not writable.
    assert cli.profile_is_writable(tmp_path / 'missing') is False


def test_exec_full_address_adapter_preparation_failure_is_configuration_error(
        repo, tmp_path, monkeypatch, capsys):
    # A full address keeps the no-chain behavior: the same preparation failure
    # ends the call as a configuration error with exit code 2.
    path = alias_registry(tmp_path, ['codex/openai/fake'])

    def command(*args, **kwargs):
        raise ValueError('ZCode plan key unavailable: provider=account:x')

    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    monkeypatch.setattr(codex, 'command', command)
    code = cli.main(['exec', '--config', str(path), '--repo', str(repo),
                     '-m', 'codex/openai/fake', '-p', 'task'])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'configuration_error'
    assert 'code' not in payload
    assert 'ZCode plan key unavailable' in payload['reason']


def test_route_chain_agy_explicit_auto_is_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'run', lambda *a, **k: pytest.fail('Must not run'))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '-m', 'dsk', '-p', 'task', '--shell', 'auto'])
    with pytest.raises(registry.ConfigError):
        cli.run_route_chain(args, tmp_path / 'registry.json', 'dsk',
                            chain_candidates('agy/gemini/gem', 'codex/openai/a'),
                            'auto', tmp_path, 'task', 0, None, 1, 30)


def test_exec_alias_end_to_end_with_real_worker(repo, tmp_path, monkeypatch):
    path = alias_registry(tmp_path, ['codex/openai/fake', 'codex/other/fake'])
    data = registry.load(path)
    registry.alias_set(data, 'dsk', ['codex/openai/fake', 'codex/other/fake'])
    registry.save(path, data)
    command = fake_worker(tmp_path, '''import sys,json
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"12345678-1234-1234-1234-1234567890ab"}))
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}))
print(json.dumps({"type":"turn.completed"}))
''')
    monkeypatch.setattr(codex, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'dsk',
                                    '-p', 'task', '--log-dir', str(tmp_path / 'logs')])
    result, code = cli.execute(args, path)
    assert code == 0 and result['status'] == 'completed' and result['answer'] == 'done'
    assert result['alias'] == 'dsk' and result['attempt_routes'] == ['codex/openai/fake']
    assert result['requested'] == {'harness': 'codex', 'provider': 'openai', 'model': 'fake',
                                   'effort': 'low', 'shell': 'none'}
    assert result['attempts'] == 1 and result['resumed'] is False


def test_route_retries_config_default_and_validation(tmp_path):
    path = tmp_path / 'config.json'
    document = {'version': 1, 'harnesses': {},
                'execution': {'status_interval_seconds': 1800, 'max_runtime_seconds': None},
                'hidden': []}
    path.write_text(json.dumps(document), encoding='utf-8')
    data = registry.load(path)
    assert data['execution'].get('route_retries', 1) == 1
    data['execution']['route_retries'] = 2
    registry.save(path, data)
    assert registry.load(path)['execution']['route_retries'] == 2
    for bad in (-1, 1.5, True, 'x'):
        document = {'version': 1, 'harnesses': {},
                    'execution': {'status_interval_seconds': 1800, 'max_runtime_seconds': None,
                                  'route_retries': bad},
                    'hidden': []}
        path.write_text(json.dumps(document), encoding='utf-8')
        with pytest.raises(registry.ConfigError):
            registry.load(path)
