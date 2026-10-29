import importlib
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
cli = importlib.import_module('delegator')
registry = cli.registry
runner = importlib.import_module('runner')
codex = importlib.import_module('codex')


def fake_worker(tmp_path, code):
    path = tmp_path / 'worker.py'
    path.write_text(code, encoding='utf-8')
    return [sys.executable, '-X', 'utf8', '-B', str(path)]


def registry_with_models(tmp_path, models, aliases=None):
    path = tmp_path / 'registry.json'
    data = registry.load(path)
    for address, efforts, default in models:
        registry.add(data, address, efforts, default)
    for name, routes in (aliases or {}).items():
        registry.alias_set(data, name, routes)
    registry.save(path, data)
    return path


def completed_result(target, answer='Hi'):
    harness, provider, model = target.split('/', 2)
    return {'status': 'completed', 'stop_reason': 'completed', 'reason': None,
            'reported': {'harness': harness, 'provider': provider, 'model': model,
                         'effort': 'low'},
            'answer': answer, 'log_path': 'unused', 'warnings': [], 'denied_actions': [],
            'changed_paths': [], 'preexisting_changed_paths': []}


def capacity_result(target):
    result = completed_result(target, answer='')
    result.update(status='failed', stop_reason='capacity', reason='at capacity')
    return result


def install_fake_run(monkeypatch, results):
    """Serve results per call; record (repo, prompt, target, track) of every call."""
    seen = []

    def fake_run(adapter, command, env, repo, prompt, target, interval, runtime, log_dir,
                 resume, session, track=True, **kwargs):
        repo = Path(repo)
        assert not (repo / '.git').exists(), 'one-shot directory must not be a Git repository'
        seen.append({'repo': repo, 'prompt': prompt, 'target': target, 'track': track})
        item = results[len(seen) - 1]
        return item(target) if callable(item) else item

    monkeypatch.setattr(cli, 'run', fake_run)
    # ask/test must never wait between attempts; a wait is a test failure.
    monkeypatch.setattr(cli, 'retry_wait',
                        lambda seconds: pytest.fail('retry wait in a no-retry command'))
    return seen


def test_ask_runs_in_throwaway_dir_and_removes_it(tmp_path, monkeypatch):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    captured = {}
    monkeypatch.setattr(codex, 'command',
                        lambda *a, **k: captured.update(git_check=k.get('git_check'))
                        or (['echo', 'ok'], {}))
    seen = install_fake_run(monkeypatch, [completed_result])
    args = cli.parser().parse_args(['ask', '-m', 'codex/openai/fake', '-p', 'Hi'])
    result, code = cli.ask_execute(args, path)
    assert code == 0 and result['status'] == 'completed'
    assert result['requested'] == dict(harness='codex', provider='openai', model='fake',
                                       effort='low', shell='none')
    assert result['changed_paths'] == [] and result['preexisting_changed_paths'] == []
    assert seen[0]['prompt'] == 'Hi' and seen[0]['track'] is False
    assert captured['git_check'] is False, 'ask must waive the codex Git working-copy check'
    assert seen[0]['repo'].name.startswith('delegator-ask-')
    assert not seen[0]['repo'].exists(), 'temporary directory must be removed'


def test_exec_keeps_git_check(tmp_path, monkeypatch):
    import subprocess
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    argv, _ = codex.command('worker.exe', repo, 'openai', 'gpt', 'low', 'review', 'none')
    assert '--skip-git-repo-check' not in argv
    argv, _ = codex.command('worker.exe', repo, 'openai', 'gpt', 'low', 'review', 'none',
                            git_check=False)
    assert '--skip-git-repo-check' in argv
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    captured = {}
    monkeypatch.setattr(codex, 'command',
                        lambda *a, **k: captured.update(git_check=k.get('git_check'))
                        or (['echo', 'ok'], {}))
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(cli, 'run', lambda *a, **k: completed_result('codex/openai/fake'))
    args = cli.parser().parse_args(['exec', '--repo', str(repo), '-m', 'codex/openai/fake',
                                    '-p', 'Hi'])
    result, code = cli.execute(args, path)
    assert code == 0 and captured['git_check'] is True


def test_ask_rejects_missing_or_empty_prompt(tmp_path):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    for argv in (['ask', '-m', 'codex/openai/fake'],
                 ['ask', '-m', 'codex/openai/fake', '-p', '   ']):
        args = cli.parser().parse_args(argv)
        with pytest.raises(registry.ConfigError, match='nonempty'):
            cli.ask_execute(args, path)


def test_ask_full_address_runs_once_without_retries(tmp_path, monkeypatch):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['echo', 'ok'], {}))
    seen = install_fake_run(monkeypatch, [capacity_result])
    args = cli.parser().parse_args(['ask', '-m', 'codex/openai/fake', '-p', 'Hi'])
    result, code = cli.ask_execute(args, path)
    assert code == 1 and result['stop_reason'] == 'capacity'
    assert result['attempts'] == 1 and len(seen) == 1


def test_ask_alias_runs_each_route_once_and_hands_off(tmp_path, monkeypatch):
    path = registry_with_models(tmp_path, [('codex/openai/a', ['low'], 'low'),
                                           ('codex/openai/b', ['low'], 'low')],
                                aliases={'pair': ['codex/openai/a', 'codex/openai/b']})
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['echo', 'ok'], {}))
    seen = install_fake_run(monkeypatch, [capacity_result, completed_result])
    args = cli.parser().parse_args(['ask', '-m', 'pair', '-p', 'Hi'])
    result, code = cli.ask_execute(args, path)
    assert code == 0 and result['status'] == 'completed'
    assert result['alias'] == 'pair' and result['attempts'] == 2
    assert result['attempt_routes'] == ['codex/openai/a', 'codex/openai/b']
    assert result['requested']['model'] == 'b'
    assert [s['target'] for s in seen] == ['codex/openai/a', 'codex/openai/b']


def test_ask_failure_still_removes_temp_dir(tmp_path, monkeypatch, capsys):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['echo', 'ok'], {}))
    seen = []

    def broken_run(*args, **kwargs):
        seen.append({'repo': Path(args[3])})
        raise OSError('boom')

    monkeypatch.setattr(cli, 'run', broken_run)
    code = cli.main(['ask', '--config', str(path), '-m', 'codex/openai/fake', '-p', 'Hi'])
    captured = capsys.readouterr()
    assert code == 2 and 'ask failed' in captured.err and 'boom' in captured.err
    assert not seen[0]['repo'].exists()


def test_ask_plain_output_answer_and_errors(tmp_path, monkeypatch, capsys):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['echo', 'ok'], {}))

    install_fake_run(monkeypatch, [completed_result])
    code = cli.main(['ask', '--config', str(path), '-m', 'codex/openai/fake', '-p', 'Hi'])
    captured = capsys.readouterr()
    assert code == 0 and captured.out.strip() == 'Hi' and not captured.err.strip()

    install_fake_run(monkeypatch, [capacity_result])
    code = cli.main(['ask', '--config', str(path), '-m', 'codex/openai/fake', '-p', 'Hi'])
    captured = capsys.readouterr()
    assert code == 1 and not captured.out.strip()
    assert 'ask failed: capacity: at capacity' in captured.err

    question = completed_result('codex/openai/fake', 'NEEDS_USER_INPUT: Which task?')
    question.update(status='needs_input', stop_reason='worker_question',
                    reason='Worker requested an owner decision', session_id='sess-question')
    install_fake_run(monkeypatch, [question])
    code = cli.main(['ask', '--config', str(path), '-m', 'codex/openai/fake', '-p', 'Hi'])
    captured = capsys.readouterr()
    assert code == 3 and 'NEEDS_USER_INPUT: Which task?' in captured.out
    assert 'ask failed' not in captured.err

    native = completed_result('codex/openai/fake', '')
    native.update(status='failed', stop_reason='needs_user_input',
                  reason='Native tool permission denied')
    install_fake_run(monkeypatch, [native])
    code = cli.main(['ask', '--config', str(path), '-m', 'codex/openai/fake', '-p', 'Hi'])
    captured = capsys.readouterr()
    assert code == 1 and not captured.out.strip()
    assert 'ask failed: needs_user_input: Native tool permission denied' in captured.err

    install_fake_run(monkeypatch, [completed_result])
    code = cli.main(['ask', '--config', str(path), '-m', 'codex/openai/fake', '-p', 'Hi',
                     '--json'])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0 and payload['answer'] == 'Hi'


def test_ask_plain_output_prints_full_answer_from_file(tmp_path, monkeypatch, capsys):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['echo', 'ok'], {}))
    answer_file = tmp_path / 'run-abc.answer.txt'
    full = 'B' * 3000
    answer_file.write_text(full, encoding='utf-8')
    result = completed_result('codex/openai/fake', 'A' * 2000)
    result.update(answer_truncated=True, answer_path=str(answer_file), answer_length=len(full))
    install_fake_run(monkeypatch, [result])
    code = cli.main(['ask', '--config', str(path), '-m', 'codex/openai/fake', '-p', 'Hi'])
    captured = capsys.readouterr()
    assert code == 0 and captured.out == full + '\n'


def test_ask_missing_model_lists_available_in_stderr(tmp_path, monkeypatch, capsys):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    code = cli.main(['ask', '--config', str(path), '-m', 'codex/openai/absent', '-p', 'Hi'])
    captured = capsys.readouterr()
    assert code == 2 and not captured.out.strip()
    assert 'ask failed: Select an available full model address' in captured.err
    assert 'available: codex/openai/fake' in captured.err


def test_exec_still_requires_a_git_root(tmp_path):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    plain = tmp_path / 'plain'
    plain.mkdir()
    args = cli.parser().parse_args(['exec', '--repo', str(plain), '-m', 'codex/openai/fake',
                                    '-p', 'Hi'])
    with pytest.raises(registry.ConfigError, match='root of a Git working copy'):
        cli.execute(args, path)


def test_runner_track_false_skips_lock_and_git_snapshots(tmp_path, monkeypatch):
    plain = tmp_path / 'plain'
    plain.mkdir()
    monkeypatch.setattr(runner, 'git_paths',
                        lambda repo: pytest.fail('git snapshot in track=False mode'))
    monkeypatch.setattr(runner, 'RepoLock',
                        lambda repo: pytest.fail('repo lock in track=False mode'))
    command = fake_worker(tmp_path, '''import json,sys
sys.stdin.read()
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"Hi"}}))
print(json.dumps({"type":"turn.completed"}))
''')
    result = runner.run(codex, command, {}, plain, 'task', 'target', interval=0,
                        log_dir=tmp_path / 'logs', track=False)
    assert result['status'] == 'completed' and result['answer'] == 'Hi'
    assert result['changed_paths'] == [] and result['preexisting_changed_paths'] == []


def test_test_live_pass_and_summary(tmp_path, monkeypatch, capsys):
    path = registry_with_models(tmp_path, [('codex/openai/good', ['low'], 'low')])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['echo', 'ok'], {}))
    seen = install_fake_run(monkeypatch, [completed_result])
    code = cli.main(['test', '--config', str(path)])
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and len(seen) == 1
    assert out[0].startswith('codex/openai/good | ok | Hi')
    assert out[-1] == 'passed 1/1'


def test_test_explicit_unavailable_model_fails_offline(tmp_path, monkeypatch, capsys):
    path = registry_with_models(tmp_path, [('codex/other/bad', ['low'], 'low')])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['echo', 'ok'], {}))
    seen = install_fake_run(monkeypatch, [completed_result])
    code = cli.main(['test', '--config', str(path), '-m', 'codex/other/bad'])
    out = capsys.readouterr().out.splitlines()
    assert code == 1 and not seen
    assert out[0].startswith('codex/other/bad | fail | No executable module')
    assert out[-1] == 'passed 0/1'


def test_test_preflight_failure_skips_live_run(tmp_path, monkeypatch, capsys):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')

    def broken_command(*a, **k):
        raise ValueError('ZCode plan key unavailable')

    monkeypatch.setattr(codex, 'command', broken_command)
    seen = install_fake_run(monkeypatch, [completed_result])
    code = cli.main(['test', '--config', str(path), '-m', 'codex/openai/fake'])
    out = capsys.readouterr().out.splitlines()
    assert code == 1 and not seen
    assert 'plan key unavailable' in out[0] and out[0].startswith('codex/openai/fake | fail |')
    assert out[-1] == 'passed 0/1'


def test_test_effort_not_configured_fails_offline(tmp_path, monkeypatch, capsys):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['echo', 'ok'], {}))
    seen = install_fake_run(monkeypatch, [completed_result])
    code = cli.main(['test', '--config', str(path), '-m', 'codex/openai/fake', '-e', 'high'])
    out = capsys.readouterr().out.splitlines()
    assert code == 1 and not seen
    assert 'effort high is not configured' in out[0]


def test_test_alias_expands_routes(tmp_path, monkeypatch, capsys):
    path = registry_with_models(tmp_path, [('codex/openai/a', ['low'], 'low'),
                                           ('codex/openai/b', ['low'], 'low')],
                                aliases={'pair': ['codex/openai/a', 'codex/openai/b']})
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['echo', 'ok'], {}))
    seen = install_fake_run(monkeypatch, [capacity_result, completed_result])
    code = cli.main(['test', '--config', str(path), '-m', 'pair'])
    out = capsys.readouterr().out.splitlines()
    assert code == 1 and len(seen) == 2
    assert out[0].startswith('codex/openai/a | fail | at capacity')
    assert out[1].startswith('codex/openai/b | ok | Hi')
    assert out[-1] == 'passed 1/2'


def test_test_unknown_alias_is_configuration_error(tmp_path, capsys):
    path = registry_with_models(tmp_path, [('codex/openai/fake', ['low'], 'low')])
    code = cli.main(['test', '--config', str(path), '-m', 'nope'])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload['status'] == 'configuration_error'
    assert 'Unknown alias' in payload['reason'] and payload['aliases'] == []


def test_test_json_output_shape(tmp_path, monkeypatch, capsys):
    path = registry_with_models(tmp_path, [('codex/openai/good', ['low'], 'low')])
    monkeypatch.setattr(cli.shutil, 'which', lambda name: 'x')
    monkeypatch.setattr(codex, 'command', lambda *a, **k: (['echo', 'ok'], {}))
    install_fake_run(monkeypatch, [completed_result])
    code = cli.main(['test', '--config', str(path), '--json'])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    entry, summary = lines[0], lines[-1]
    assert entry['address'] == 'codex/openai/good' and entry['ok'] is True
    assert entry['phase'] == 'live' and entry['status'] == 'completed'
    assert entry['log_path'] == 'unused'
    assert summary == {'command': 'test', 'passed': 1, 'total': 1}
