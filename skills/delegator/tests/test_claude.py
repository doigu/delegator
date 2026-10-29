import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
claude = importlib.import_module('claude')
native = importlib.import_module('native')
runner = importlib.import_module('runner')
cli = importlib.import_module('delegator')
registry = importlib.import_module('registry')

MODEL = 'claude-sonnet-5-5'
SESSION = '12345678-1234-1234-1234-1234567890ab'


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    subprocess.run(['git', 'init', str(path)], check=True, capture_output=True)
    return path


def consume(payload, channel='stdout'):
    summary = native.Summary()
    summary.consume(claude, channel, json.dumps(payload))
    return summary


def fake_worker(tmp_path, code):
    path = tmp_path / 'worker.py'
    path.write_text(code, encoding='utf-8')
    return [sys.executable, '-X', 'utf8', '-B', str(path)]


def test_claude_argv_uses_native_modes_without_bypass(tmp_path):
    argv, env = claude.command('claude.exe', tmp_path, 'anthropic', MODEL, 'high', 'review', 'none')
    assert argv == ['claude.exe', '--print', '--verbose', '--output-format', 'stream-json',
                    '--permission-mode', 'plan', '--permission-prompts', 'none',
                    '--model', MODEL, '--effort', 'high']
    assert env == {}
    argv, _ = claude.command('claude.exe', tmp_path, 'anthropic', MODEL, None, 'implementation', 'none')
    assert argv[argv.index('--permission-mode') + 1] == 'acceptEdits'
    assert argv[argv.index('--permission-prompts') + 1] == 'none'
    assert '--effort' not in argv
    # Shell levels select the native permission mode for both modes; auto/full
    # drop --permission-prompts none so the native classifier can approve
    # plainly safe commands headless (pinning it denies even those, live check).
    for mode in ('review', 'implementation'):
        argv, _ = claude.command('claude.exe', tmp_path, 'anthropic', MODEL, None, mode, 'auto')
        assert argv[argv.index('--permission-mode') + 1] == 'auto'
        assert '--permission-prompts' not in argv
        argv, _ = claude.command('claude.exe', tmp_path, 'anthropic', MODEL, None, mode, 'full')
        assert argv[argv.index('--permission-mode') + 1] == 'bypassPermissions'
        assert '--permission-prompts' not in argv
        for banned in ('--dangerously-skip-permissions', '--continue', '--fallback-model',
                       '--strict-mcp-config', '--allowedTools', '--disallowedTools'):
            assert banned not in argv
        assert argv[-1] != '-' and '-p' not in argv
    with pytest.raises(ValueError):
        claude.command('claude.exe', tmp_path, 'other', MODEL, None, 'review', 'none')


def test_claude_resume_argv_only_with_uuid(tmp_path):
    argv, _ = claude.command('claude.exe', tmp_path, 'anthropic', MODEL, 'high', 'review', 'none',
                             SESSION)
    assert argv[argv.index('--resume') + 1] == SESSION
    assert '--continue' not in argv and '--fork-session' not in argv
    # The shell level applies to the resumed call: escalation reuses the session.
    argv, _ = claude.command('claude.exe', tmp_path, 'anthropic', MODEL, None, 'implementation',
                             'full', SESSION)
    assert argv[argv.index('--permission-mode') + 1] == 'bypassPermissions'
    assert argv[argv.index('--resume') + 1] == SESSION
    argv, _ = claude.command('claude.exe', tmp_path, 'anthropic', MODEL, None, 'implementation',
                             'none')
    assert '--resume' not in argv


def test_claude_events_init_assistant_and_success_result():
    summary = consume({'type': 'system', 'subtype': 'init', 'session_id': SESSION,
                       'model': MODEL, 'permissionMode': 'plan', 'tools': ['Read']})
    assert summary.session_id == SESSION and summary.reported['model'] == MODEL
    summary.consume(claude, 'stdout', json.dumps({
        'type': 'assistant', 'message': {'model': 'claude-haiku-4-5', 'content': [
            {'type': 'text', 'text': 'Hello '},
            {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': 'x'}},
            {'type': 'thinking', 'thinking': 'quota exceeded'}]}}))
    assert summary.answer == 'Hello ' and summary.last_tool == 'Read'
    # A mid-turn message model never overrides the native init selection.
    assert summary.reported['model'] == MODEL
    summary.consume(claude, 'stdout', json.dumps({
        'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't'}]}}))
    assert summary.completed_tools == 1
    summary.consume(claude, 'stdout', json.dumps({
        'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': SESSION,
        'result': 'Hello world', 'total_cost_usd': 0.01,
        'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}}))
    assert summary.answer == 'Hello world' and summary.finished is True
    assert summary.usage == {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12,
                             'cost': 0.01}
    assert summary.reason is None


def test_claude_result_without_usage_stays_none_and_without_success_stays_unfinished():
    summary = consume({'type': 'result', 'subtype': 'success', 'is_error': False,
                       'session_id': SESSION, 'result': 'done'})
    assert summary.finished is True and summary.answer == 'done' and summary.usage == {}
    summary = consume({'type': 'result', 'subtype': 'error_max_turns', 'is_error': False,
                       'result': None})
    assert summary.finished is False and summary.reason is None


def test_claude_is_error_fails_with_result_text_or_subtype():
    summary = consume({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                       'result': 'boom'})
    assert summary.reason == 'native_error' and summary.error == 'boom'
    assert summary.finished is False
    summary = consume({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True,
                       'result': None})
    # An existing classification; no new output_limited reason is introduced.
    assert summary.reason == 'native_error' and summary.error == 'error_max_turns'
    summary = consume({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                       'result': 'You have hit your usage limit.'})
    assert summary.reason == 'quota'


def test_claude_missing_result_and_unknown_events_do_not_finish_or_crash():
    summary = consume({'type': 'system', 'subtype': 'init', 'session_id': SESSION, 'model': MODEL})
    summary.consume(claude, 'stdout', json.dumps({
        'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'partial'}]}}))
    assert summary.finished is False and summary.answer == 'partial'
    summary.consume(claude, 'stdout', json.dumps({'type': 'other_event', 'payload': {}}))
    assert summary.finished is False and summary.reason is None


def test_claude_rate_limit_event_allowed_is_not_an_error_or_quota():
    summary = consume({'type': 'rate_limit_event', 'status': 'allowed',
                       'rate_limit_type': 'five_hour',
                       'message': 'Claude AI usage limit reached|1760000000'})
    assert summary.reason is None and summary.finished is False and summary.usage == {}


def test_claude_stderr_classification():
    summary = consume({'type': 'system', 'subtype': 'init', 'session_id': SESSION, 'model': MODEL})
    summary.consume(claude, 'stderr', 'Error: HTTP 429 Too Many Requests\n')
    assert summary.reason == 'native_error'
    summary = native.Summary()
    summary.consume(claude, 'stderr', 'ERROR: authentication failed\n')
    assert summary.reason == 'auth'
    summary = native.Summary()
    summary.consume(claude, 'stderr', 'Claude AI usage limit reached|1760000000\n')
    assert summary.reason == 'quota' and summary.reset is None
    # Prose and diagnostics outside the explicit error lines are not scanned.
    summary = native.Summary()
    summary.consume(claude, 'stderr', 'note: quota exceeded wording in a diagnostic\n')
    assert summary.reason is None


def test_claude_runner_completed_single_invocation(repo, tmp_path):
    command = fake_worker(tmp_path, '''import sys,json
sys.stdin.read()
print(json.dumps({"type":"system","subtype":"init","session_id":"''' + SESSION + '''","model":"''' + MODEL + '''"}))
print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"Готово"}]}}))
print(json.dumps({"type":"result","subtype":"success","is_error":False,"session_id":"''' + SESSION + '''","result":"Готово","usage":{"input_tokens":7,"output_tokens":3}}))
''')
    result = runner.run(claude, command, {}, repo, 'task', 'claude/anthropic/' + MODEL, interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed' and result['stop_reason'] == 'completed'
    assert result['answer'] == 'Готово'
    assert result['reported'] == {'harness': None, 'provider': None, 'model': MODEL, 'effort': None}
    assert result['usage'] == {'input_tokens': 7, 'output_tokens': 3}
    assert result['session_id'] == SESSION and result['resumed'] is False
    assert result['changed_paths'] == []


def test_claude_runner_missing_result_is_no_terminal_answer(repo, tmp_path):
    command = fake_worker(tmp_path, '''import sys,json
sys.stdin.read()
print(json.dumps({"type":"system","subtype":"init","session_id":"''' + SESSION + '''","model":"''' + MODEL + '''"}))
print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}))
''')
    result = runner.run(claude, command, {}, repo, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'failed' and result['stop_reason'] == 'no_terminal_answer'


def test_claude_resume_confirms_requested_session_or_fails_closed(repo, tmp_path):
    code = '''import sys,json
sys.stdin.read()
session = sys.argv[1]
payload = {"type":"result","subtype":"success","is_error":False,"result":"continued"}
if session != "absent":
    payload["session_id"] = session
print(json.dumps(payload))
'''
    result = runner.run(claude, fake_worker(tmp_path, code) + [SESSION], {}, repo, 'delta', 'target',
                        interval=0, log_dir=tmp_path / 'logs', resume_session=SESSION)
    assert result['status'] == 'completed' and result['resumed'] is True
    assert result['session_id'] == SESSION
    result = runner.run(claude, fake_worker(tmp_path, code) + ['8c0bc9a1-1111-2222-3333-444455556666'],
                        {}, repo, 'delta', 'target', interval=0, log_dir=tmp_path / 'logs',
                        resume_session=SESSION)
    assert result['status'] == 'failed' and result['stop_reason'] == 'resume_session_mismatch'
    result = runner.run(claude, fake_worker(tmp_path, code) + ['absent'], {}, repo, 'delta', 'target',
                        interval=0, log_dir=tmp_path / 'logs', resume_session=SESSION)
    assert result['status'] == 'failed' and result['stop_reason'] == 'resume_session_unconfirmed'


def test_claude_registry_model_rows_and_offline_management(tmp_path, capsys):
    path = tmp_path / 'registry.json'
    assert cli.main(['--config', str(path), 'add', 'claude/anthropic/' + MODEL,
                     '--efforts', 'high', '--effort', 'high']) == 0
    assert cli.main(['--config', str(path), 'add', 'claude/other/model']) == 0
    rows = {row['address']: row for row in cli.model_rows(registry.load(path))}
    assert rows['claude/anthropic/' + MODEL]['available']
    assert not rows['claude/other/model']['available']
    assert 'No executable module' in rows['claude/other/model']['reason']
    capsys.readouterr()
    assert cli.main(['--config', str(path), '--json', 'off', 'claude/anthropic']) == 0
    rows = {row['address']: row for row in cli.model_rows(registry.load(path))}
    assert not rows['claude/anthropic/' + MODEL]['available']
    assert cli.main(['--config', str(path), '--json', 'on', 'claude/anthropic']) == 0
    rows = {row['address']: row for row in cli.model_rows(registry.load(path))}
    assert rows['claude/anthropic/' + MODEL]['available']
    assert cli.main(['--config', str(path), 'delete', 'claude/other', '--recursive', '--yes']) == 0
    assert 'other' not in registry.load(path)['harnesses']['claude']['providers']


def test_claude_unavailable_provider_and_malformed_resume_rejected_before_spawn(
        repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'claude/anthropic/fake', ['high'], 'high')
    registry.add(data, 'claude/other/model')
    registry.save(path, data)
    monkeypatch.setattr(cli, 'run', lambda *args: pytest.fail('Rejected selection must not launch'))
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'claude/other/model',
                                    '-p', 'task'])
    result, code = cli.execute(args, path)
    assert code == 2 and result['status'] == 'configuration_error'
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'claude/anthropic/fake',
                                    '--resume-session', 'not-a-uuid', '-p', 'task'])
    with pytest.raises(registry.ConfigError, match='session ID format'):
        cli.execute(args, path)


def test_claude_exec_completed_runs_once(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'claude/anthropic/' + MODEL, ['high'], 'high')
    registry.save(path, data)
    counter = tmp_path / 'calls'
    command = fake_worker(tmp_path, '''import sys,pathlib,json
pathlib.Path(sys.argv[1]).write_text("1")
sys.stdin.read()
print(json.dumps({"type":"system","subtype":"init","session_id":"''' + SESSION + '''","model":"''' + MODEL + '''"}))
print(json.dumps({"type":"result","subtype":"success","is_error":False,"session_id":"''' + SESSION + '''","result":"done"}))
''') + [str(counter)]
    monkeypatch.setattr(claude, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'claude/anthropic/' + MODEL,
                                    '-e', 'high', '--mode', 'implementation', '-p', 'task',
                                    '--log-dir', str(tmp_path / 'logs')])
    result, code = cli.execute(args, path)
    assert code == 0 and counter.read_text() == '1'
    assert result['status'] == 'completed' and result['requested']['effort'] == 'high'


def test_claude_reported_selection_mismatch_including_alias(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'claude/anthropic/sonnet', ['high'], 'high')
    registry.save(path, data)
    command = fake_worker(tmp_path, '''import sys,json
sys.stdin.read()
print(json.dumps({"type":"system","subtype":"init","session_id":"''' + SESSION + '''","model":"''' + MODEL + '''"}))
print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"done"}))
''')
    monkeypatch.setattr(claude, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'claude/anthropic/sonnet',
                                    '-p', 'task', '--log-dir', str(tmp_path / 'logs')])
    result, code = cli.execute(args, path)
    assert code == 1 and result['stop_reason'] == 'reported_selection_mismatch'
    assert result['reported']['model'] == MODEL and result['requested']['model'] == 'sonnet'


def test_claude_quota_line_blocks_provider_after_one_run(repo, tmp_path, monkeypatch):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    registry.add(data, 'claude/anthropic/fake', ['high'], 'high')
    registry.add(data, 'claude/anthropic/neighbor')
    registry.save(path, data)
    counter = tmp_path / 'calls'
    command = fake_worker(tmp_path, '''import sys,pathlib
path=pathlib.Path(sys.argv[1])
path.write_text(path.read_text()+"1" if path.exists() else "1")
sys.stdin.read()
print("Claude AI usage limit reached|1760000000", file=sys.stderr)
sys.exit(1)
''') + [str(counter)]
    monkeypatch.setattr(claude, 'command', lambda *args, **kwargs: (command, {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: sys.executable)
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'claude/anthropic/fake',
                                    '-p', 'task', '--log-dir', str(tmp_path / 'logs')])
    result, code = cli.execute(args, path)
    assert code == 1 and result['stop_reason'] == 'quota'
    assert result['quota_reset_at'] is None and counter.read_text() == '1'
    states = {r['address']: r['available'] for r in registry.available(registry.load(path))}
    assert not states['claude/anthropic/fake'] and not states['claude/anthropic/neighbor']


def test_claude_help_and_skill_document_the_harness():
    import io
    from contextlib import redirect_stdout
    buffer = io.StringIO()
    with redirect_stdout(buffer), pytest.raises(SystemExit):
        cli.parser().parse_args(['exec', '--help'])
    assert 'claude/anthropic/' + MODEL in buffer.getvalue()
    skill_dir = Path(__file__).parents[1]
    harnesses = (skill_dir / 'references' / 'harnesses.md').read_text(encoding='utf-8')
    assert 'claude/anthropic/' + MODEL in harnesses and 'acceptEdits' in harnesses
    assert 'references/harnesses.md' in (skill_dir / 'SKILL.md').read_text(encoding='utf-8')


def test_claude_permission_denials_are_reported_and_overcome_denial_completes(repo, tmp_path):
    # Shape from a live acceptEdits run with --permission-prompts none.
    denial = {'tool_name': 'Bash', 'tool_use_id': 'toolu_1', 'tool_input': {'command': 'python -c 1'}}
    lines = [{'type': 'system', 'subtype': 'init', 'session_id': SESSION, 'model': MODEL},
             {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': SESSION,
              'result': 'Step 1 REFUSED', 'permission_denials': [denial]}]
    command = fake_worker(tmp_path, 'import sys\nsys.stdin.read()\n'
                          + ''.join('print(' + repr(json.dumps(line)) + ')\n' for line in lines))
    result = runner.run(claude, command, {}, repo, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs')
    assert result['status'] == 'completed' and result['stop_reason'] == 'completed'
    assert result['denied_actions'] == [{'tool': 'Bash', 'detail': '{"command": "python -c 1"}'}]


def test_claude_denial_without_answer_is_recorded_without_failing_the_event():
    summary = consume({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '',
                       'permission_denials': [{'tool_name': 'Bash', 'tool_input': {}}]})
    assert summary.reason is None and summary.denied_actions[0]['tool'] == 'Bash'
