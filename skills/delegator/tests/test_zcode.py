import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
zcode = importlib.import_module('zcode')
native = importlib.import_module('native')
runner = importlib.import_module('runner')
cli = importlib.import_module('delegator')
registry = importlib.import_module('registry')

PROVIDER = 'account:zai-individual-coding-plan'
MODEL = 'GLM-5.3'
RESUME_ID = 'sess_170a25a0-97f2-43db-a08d-20b3c756bcaf'


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    subprocess.run(['git', 'init', str(path)], check=True, capture_output=True)
    return path


MODEL_RULES = [
    {'modelMatch': '.*',
     'config': {'optionSpecs': {'reasoningLevel': {'values': ['disabled', 'enabled']}}}},
    {'modelMatch': r'.*glm-5\.3(?:-flash)?(?:[.\-:/\[].*)?',
     'config': {'optionSpecs': {'reasoningLevel': {'values': ['low', 'high', 'max']}}}},
]


def write_table(tmp_path, rules=None):
    table = {
        'revision': 30,
        'config': {
            'providerConfigRules': {'providerRules': [
                {'providerId': PROVIDER,
                 'config': {'builtinModelIds': ['GLM-5.3', 'GLM-5.3-Flash'],
                            'access': {'type': 'zhipu-account', 'accountType': 'zai',
                                       'mode': 'individual-coding-plan'}}},
                {'providerId': 'account:zai-start-plan',
                 'config': {'builtinModelIds': ['GLM-5.3-Flash'],
                            'access': {'type': 'zhipu-account', 'accountType': 'zai',
                                       'mode': 'start-plan'}}},
            ]},
            'modelConfigRules': {'modelRules': MODEL_RULES if rules is None else rules},
        },
    }
    path = tmp_path / 'zcode-builtin.json'
    path.write_text(json.dumps(table), encoding='utf-8')
    return path


def make_home(tmp_path, key='secret-key'):
    home = tmp_path / 'zhome'
    (home / 'v2').mkdir(parents=True)
    (home / 'v2' / 'credentials.json').write_text(json.dumps({
        'account-provider:cp:account:zai-individual-coding-plan:account:u:api-key': key,
    }), encoding='utf-8')
    return home


def selection(tmp_path, monkeypatch, key='secret-key'):
    table_path = write_table(tmp_path)
    home = make_home(tmp_path, key)
    monkeypatch.setenv('ZCODE_HOME', str(home))
    monkeypatch.setenv('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE', str(table_path))
    table = json.loads(table_path.read_text(encoding='utf-8'))
    return table_path, home, table


def read_result(model=MODEL, provider=PROVIDER, level='high'):
    current = {'modelId': model, 'providerId': provider}
    if level is not None:
        current['options'] = {'reasoningLevel': level}
    return {'settings': {'model': {'current': current}}}


def drive_to_send(session, summary):
    messages = session.initial(summary, 'ASSIGNMENT')
    assert messages[0]['method'] == 'provider/updateAccountConfig'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/create'
    messages = session.event(summary, {'id': messages[0]['id'],
                                       'result': {'session': {'sessionId': 'sid-1'}}})
    assert messages[0]['method'] == 'session/setModel'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/read'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': read_result()})
    assert messages[0]['method'] == 'session/subscribe'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/send'
    return session.event(summary, {'id': messages[0]['id'], 'result': {}})


def complete_turn(session, summary, answer='done', usage=None):
    assert session.event(summary, {'method': 'session/event', 'params': {
        'type': 'model.streaming', 'payload': {'delta': answer[:2]}}}) == []
    messages = session.event(summary, {'method': 'session/event', 'params': {
        'type': 'turn.completed', 'payload': {'usage': usage or {'inputTokens': 3}}}})
    assert messages[0]['method'] == 'session/messages'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {
        'messages': [{'info': {'role': 'assistant'},
                      'parts': [{'text': answer}]}]}})
    assert messages[0]['method'] == 'session/read'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': read_result()})
    assert messages == []


def drive_to_resume_send(session, summary):
    messages = session.initial(summary, 'DELTA')
    assert messages[0]['method'] == 'provider/updateAccountConfig'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/resume'
    assert messages[0]['params'] == {'sessionId': RESUME_ID}
    messages = session.event(summary, {'id': messages[0]['id'],
                                       'result': {'session': {'sessionId': RESUME_ID}}})
    # resume takes no mode; setMode re-pins the requested level first.
    assert messages[0]['method'] == 'session/setMode'
    assert messages[0]['params'] == {'sessionId': RESUME_ID, 'mode': session.mode}
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/setModel'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/read'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': read_result()})
    assert messages[0]['method'] == 'session/subscribe'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/send'
    return session.event(summary, {'id': messages[0]['id'], 'result': {}})


def resume_session(tmp_path, monkeypatch, mode='yolo'):
    table_path, _, table = selection(tmp_path, monkeypatch)
    return zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table,
                                 RESUME_ID, mode=mode)


def test_resume_flow_answers_from_terminal_payload_only(tmp_path, monkeypatch):
    session = resume_session(tmp_path, monkeypatch)
    summary = native.Summary()
    assert drive_to_resume_send(session, summary) == []
    assert summary.session_id == RESUME_ID
    assert session.event(summary, {'method': 'session/event', 'params': {
        'type': 'model.streaming', 'payload': {'delta': 'MANG'}}}) == []
    messages = session.event(summary, {'method': 'session/event', 'params': {
        'type': 'turn.completed',
        'payload': {'response': 'MANGO-77', 'usage': {'inputTokens': 5}}}})
    # Resume must not walk the transcript: it still holds prior answers.
    assert messages[0]['method'] == 'session/read'
    messages = session.event(summary, {'id': messages[0]['id'], 'result': read_result()})
    assert messages == []
    assert session.done and summary.finished and summary.answer == 'MANGO-77'
    assert summary.usage == {'inputTokens': 5}
    assert summary.reason is None


def test_resume_turn_falls_back_to_current_run_deltas(tmp_path, monkeypatch):
    session = resume_session(tmp_path, monkeypatch)
    summary = native.Summary()
    drive_to_resume_send(session, summary)
    assert session.event(summary, {'method': 'session/event', 'params': {
        'type': 'model.streaming', 'payload': {'delta': 'MANG'}}}) == []
    assert session.event(summary, {'method': 'session/event', 'params': {
        'type': 'model.streaming', 'payload': {'delta': 'O-77'}}}) == []
    messages = session.event(summary, {'method': 'session/event', 'params': {
        'type': 'turn.completed', 'payload': {}}})
    assert messages[0]['method'] == 'session/read'
    session.event(summary, {'id': messages[0]['id'], 'result': read_result()})
    assert session.done and summary.answer == 'MANGO-77'


def test_resume_refusal_maps_unconfirmed_without_reason(tmp_path, monkeypatch):
    session = resume_session(tmp_path, monkeypatch)
    summary = native.Summary()
    messages = session.initial(summary, 'DELTA')
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/resume'
    session.event(summary, {'id': messages[0]['id'],
                            'error': {'code': -32004, 'message': 'Session not found: ' + RESUME_ID}})
    assert session.done and summary.reason is None and summary.session_id is None
    assert 'Session not found' in summary.error


def test_resume_foreign_echo_sets_reported_id_for_mismatch(tmp_path, monkeypatch):
    session = resume_session(tmp_path, monkeypatch)
    summary = native.Summary()
    messages = session.initial(summary, 'DELTA')
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    session.event(summary, {'id': messages[0]['id'],
                            'result': {'session': {'sessionId': 'sess_' + 'f' * 8 + '-0000-0000-0000-000000000000'}}})
    assert session.done and summary.reason is None
    assert summary.session_id == 'sess_' + 'f' * 8 + '-0000-0000-0000-000000000000'


def test_resolve_order_and_missing(tmp_path, monkeypatch):
    cjs = tmp_path / 'glm' / 'zcode.cjs'
    cjs.parent.mkdir()
    cjs.write_text('// fake\n', encoding='utf-8')
    monkeypatch.setattr(zcode.shutil, 'which', lambda name: str(cjs) if name == 'zcode' else 'node')
    assert zcode.resolve() == ['node', str(cjs)]
    monkeypatch.setattr(zcode.shutil, 'which', lambda name: None)
    monkeypatch.setattr(zcode.os, 'name', 'posix')
    monkeypatch.setattr(zcode.sys, 'platform', 'linux')
    with pytest.raises(ValueError, match='not found'):
        zcode.resolve()


def test_command_validates_selection_and_effort(tmp_path, monkeypatch):
    _, _, _ = selection(tmp_path, monkeypatch)
    cjs = tmp_path / 'zcode.cjs'
    cjs.write_text('// fake\n', encoding='utf-8')
    argv, env, session = zcode.command([sys.executable, str(cjs)], tmp_path, PROVIDER, MODEL,
                                       'high', 'review', 'none')
    assert argv[-2:] == ['app-server', '--stdio']
    assert session.table_path and session.mode == 'plan'
    assert isinstance(session, zcode.ProtocolSession)
    with pytest.raises(ValueError, match='explicit effort'):
        zcode.command([str(cjs)], tmp_path, PROVIDER, MODEL, None, 'review', 'none')
    with pytest.raises(ValueError, match='not offered'):
        zcode.command([str(cjs)], tmp_path, PROVIDER, 'GLM-4', 'high', 'review', 'none')
    with pytest.raises(ValueError, match='not allowed'):
        zcode.command([str(cjs)], tmp_path, PROVIDER, MODEL, 'medium', 'review', 'none')
    with pytest.raises(ValueError, match='plan provider'):
        zcode.command([str(cjs)], tmp_path, 'account:missing', MODEL, 'high', 'review', 'none')
    argv, _, session = zcode.command([sys.executable, str(cjs)], tmp_path, PROVIDER, MODEL,
                                     'high', 'review', 'none', RESUME_ID)
    assert argv[-2:] == ['app-server', '--stdio']
    assert session.resume_session == RESUME_ID and session.mode == 'plan'


def start_plan_selection(tmp_path, monkeypatch, table_file='zcode-builtin.json'):
    table_path = write_table(tmp_path)
    home = tmp_path / 'zhome'
    (home / 'v2').mkdir(parents=True, exist_ok=True)
    (home / 'v2' / 'credentials.json').write_text(json.dumps({
        'zcodejwttoken': 'login-jwt',
    }), encoding='utf-8')
    monkeypatch.setenv('ZCODE_HOME', str(home))
    monkeypatch.setenv('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE', str(table_path))
    cjs = tmp_path / 'zcode.cjs'
    cjs.write_text('// fake\n', encoding='utf-8')
    return cjs


def test_override_admits_model_outside_plan_list(tmp_path, monkeypatch):
    cjs = start_plan_selection(tmp_path, monkeypatch)
    # GLM-5.3 is not in the start-plan list: rejected without the override.
    with pytest.raises(ValueError, match='not offered'):
        zcode.command([str(cjs)], tmp_path, 'account:zai-start-plan', 'GLM-5.3', 'high',
                      'review', 'none')
    # The override admits it; the reasoning levels from modelConfigRules still apply.
    _, _, session = zcode.command([str(cjs)], tmp_path, 'account:zai-start-plan', 'GLM-5.3',
                                  'high', 'review', 'none', override=True)
    assert session.override is True
    with pytest.raises(ValueError, match='not allowed'):
        zcode.command([str(cjs)], tmp_path, 'account:zai-start-plan', 'GLM-5.3', 'medium',
                      'review', 'none', override=True)
    # A model absent from the table entirely has no known levels: effort passes.
    table_path = write_table(tmp_path, rules=[MODEL_RULES[1]])
    monkeypatch.setenv('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE', str(table_path))
    _, _, session = zcode.command([str(cjs)], tmp_path, 'account:zai-start-plan', 'GLM-9',
                                  'medium', 'review', 'none', override=True)
    assert session.override is True


def test_account_payload_appends_override_model(tmp_path):
    table_path = write_table(tmp_path)
    payload = zcode._account_payload(table_path, 'account:zai-start-plan', 'GLM-5.3')
    assert payload['providers']['account:zai-start-plan']['builtinModelIds'] == \
        ['GLM-5.3-Flash', 'GLM-5.3']
    plain = zcode._account_payload(table_path, 'account:zai-start-plan')
    assert plain['providers']['account:zai-start-plan']['builtinModelIds'] == ['GLM-5.3-Flash']
    assert plain['providers'][PROVIDER]['builtinModelIds'] == ['GLM-5.3', 'GLM-5.3-Flash']
    dup = zcode._account_payload(table_path, PROVIDER, 'GLM-5.3')
    assert dup['providers'][PROVIDER]['builtinModelIds'] == ['GLM-5.3', 'GLM-5.3-Flash']


def test_override_session_pushes_extended_model_list(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession('account:zai-start-plan', 'GLM-5.3', 'high', tmp_path,
                                    table_path, 'key', table, override=True)
    requests = session.initial(native.Summary(), 'task')
    assert requests[0]['method'] == 'provider/updateAccountConfig'
    providers = requests[0]['params']['providers']
    assert providers['account:zai-start-plan']['builtinModelIds'] == ['GLM-5.3-Flash', 'GLM-5.3']
    plain = zcode.ProtocolSession('account:zai-start-plan', 'GLM-5.3-Flash', 'high', tmp_path,
                                  table_path, 'key', table)
    requests = plain.initial(native.Summary(), 'task')
    assert requests[0]['params']['providers']['account:zai-start-plan']['builtinModelIds'] == \
        ['GLM-5.3-Flash']


def test_session_mode_mapping_and_command_modes(tmp_path, monkeypatch):
    assert zcode.session_mode('review', 'none') == 'plan'
    assert zcode.session_mode('implementation', 'none') == 'edit'
    assert zcode.session_mode('review', 'auto') == 'guarded'
    assert zcode.session_mode('implementation', 'auto') == 'guarded'
    assert zcode.session_mode('implementation', 'full') == 'yolo'
    _, _, _ = selection(tmp_path, monkeypatch)
    cjs = tmp_path / 'zcode.cjs'
    cjs.write_text('// fake\n', encoding='utf-8')
    for mode, shell, native in (('implementation', 'auto', 'guarded'),
                                ('implementation', 'full', 'yolo'),
                                ('review', 'full', 'yolo')):
        _, _, session = zcode.command([str(cjs)], tmp_path, PROVIDER, MODEL, 'high', mode, shell)
        assert session.mode == native


def test_create_and_resume_carry_session_mode(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table,
                                    mode='guarded')
    summary = native.Summary()
    messages = session.initial(summary, 'ASSIGNMENT')
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/create'
    assert messages[0]['params']['mode'] == 'guarded'
    # Escalation reuses the session: resume echoes, then setMode re-pins the
    # raised level before the model is pinned again.
    escalated = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key',
                                      table, RESUME_ID, mode='yolo')
    summary = native.Summary()
    messages = escalated.initial(summary, 'DELTA')
    messages = escalated.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/resume'
    messages = escalated.event(summary, {'id': messages[0]['id'],
                                         'result': {'session': {'sessionId': RESUME_ID}}})
    assert messages[0]['method'] == 'session/setMode'
    assert messages[0]['params'] == {'sessionId': RESUME_ID, 'mode': 'yolo'}


def test_command_refuses_missing_key_and_unwritable_home(tmp_path, monkeypatch):
    table_path = write_table(tmp_path)
    home = tmp_path / 'empty'
    (home / 'v2').mkdir(parents=True)
    (home / 'v2' / 'credentials.json').write_text('{}', encoding='utf-8')
    monkeypatch.setenv('ZCODE_HOME', str(home))
    monkeypatch.setenv('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE', str(table_path))
    cjs = tmp_path / 'zcode.cjs'
    cjs.write_text('// fake\n', encoding='utf-8')
    with pytest.raises(ValueError, match='plan key unavailable'):
        zcode.command([str(cjs)], tmp_path, PROVIDER, MODEL, 'high', 'review', 'none')
    monkeypatch.setattr(zcode, '_home_is_writable', lambda home: False)
    with pytest.raises(ValueError, match='not writable'):
        zcode.command([str(cjs)], tmp_path, PROVIDER, MODEL, 'high', 'review', 'none')


def test_protocol_success_and_reported_model(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    assert drive_to_send(session, summary) == []
    assert summary.session_id == 'sid-1'
    complete_turn(session, summary, 'finished answer', {'inputTokens': 7, 'other': 'x'})
    assert session.done
    assert summary.finished and summary.answer == 'finished answer'
    assert summary.usage == {'inputTokens': 7}
    assert summary.reported == {'harness': None, 'provider': PROVIDER,
                                'model': MODEL, 'effort': 'high'}
    assert summary.reason is None


def test_pre_turn_mismatch_does_not_send(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    messages = session.initial(summary, 'ASSIGNMENT')
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    messages = session.event(summary, {'id': messages[0]['id'],
                                       'result': {'session': {'sessionId': 'sid-2'}}})
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    assert messages[0]['method'] == 'session/read'
    messages = session.event(summary, {'id': messages[0]['id'],
                                       'result': read_result(model='GLM-5.2')})
    assert messages == []
    assert session.done and summary.reason is None
    assert summary.reported['model'] == 'GLM-5.2'


def test_missing_model_report_fails_closed(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    messages = session.initial(summary, 'ASSIGNMENT')
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    messages = session.event(summary, {'id': messages[0]['id'],
                                       'result': {'session': {'sessionId': 'sid-3'}}})
    messages = session.event(summary, {'id': messages[0]['id'], 'result': {}})
    session.event(summary, {'id': messages[0]['id'], 'result': {'settings': {'model': {}}}})
    assert session.done and summary.reason == 'native_error'
    assert 'model report unavailable' in summary.error


def test_turn_text_fallback_chain():
    assert zcode._extract_text({'response': 'terminal'}, None, ['delta']) == 'terminal'
    messages = [{'info': {'role': 'assistant'}, 'parts': [{'text': 'from parts'}]}]
    assert zcode._extract_text({}, messages, ['delta']) == 'from parts'
    assert zcode._extract_text({}, None, ['a', 'b']) == 'ab'
    assert zcode._extract_text({}, None, []) is None


def test_quota_1301_only_on_failure(tmp_path, monkeypatch):
    assert zcode.QUOTA_MARKER.search('"code": 1301')
    assert not zcode.QUOTA_MARKER.search('HTTP 429')
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    drive_to_send(session, summary)
    session.stderr(summary, 'diagnostic 1301 without a failed turn')
    complete_turn(session, summary)
    assert summary.reason is None

    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    drive_to_send(session, summary)
    session.event(summary, {'method': 'session/event', 'params': {
        'type': 'turn.failed',
        'payload': {'error': {'code': 1301, 'message': 'Insufficient balance or token expired'}}}})
    assert session.done and summary.reason == 'quota'
    assert '1301' in summary.error


def test_generic_429_is_not_quota(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    drive_to_send(session, summary)
    session.event(summary, {'method': 'session/event', 'params': {
        'type': 'turn.failed', 'payload': {'error': {'code': 429, 'message': 'HTTP 429'}}}})
    assert session.done and summary.reason == 'native_error'


def test_tool_updated_reports_last_tool_and_completed_count(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()

    def tool_event(payload):
        return session.event(summary, {'method': 'session/event',
                                       'params': {'type': 'tool.updated', 'payload': payload}})

    assert tool_event({'toolCallId': 'call_1', 'toolName': 'Read', 'kind': 'scheduled'}) == []
    assert tool_event({'toolCallId': 'call_1', 'toolName': 'Read', 'kind': 'started'}) == []
    assert tool_event({'toolCallId': 'call_2', 'toolName': 'Bash', 'kind': 'progress'}) == []
    assert tool_event({'toolCallId': 'call_1', 'kind': 'result',
                       'result': {'success': True}}) == []
    assert tool_event({'toolCallId': 'call_2', 'kind': 'result',
                       'result': {'success': True}}) == []
    assert tool_event({'toolCallIds': ['call_1', 'call_2'], 'kind': 'batch'}) == []
    assert summary.last_tool == 'Bash'
    assert summary.completed_tools == 2
    assert summary.tools_observed is True


def test_repeated_tool_result_does_not_grow_completed_count(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    result = {'toolCallId': 'call_1', 'kind': 'result', 'result': {'success': True}}
    for _ in range(2):
        session.event(summary, {'method': 'session/event',
                                'params': {'type': 'tool.updated', 'payload': result}})
    assert summary.completed_tools == 1
    assert summary.tools_observed is True


def test_absent_tool_events_report_unknown_telemetry(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    drive_to_send(session, summary)
    complete_turn(session, summary)
    assert summary.tools_observed is False
    line = runner.status_line(1800, None, summary, 'target')
    assert 'completed_tools=unknown' in line
    assert 'progress unknown' in line


def test_status_line_reports_known_tool_count(tmp_path):
    summary = native.Summary(completed_tools=3, last_tool='Read', tools_observed=True)
    line = runner.status_line(60, 30, summary, 'target')
    assert 'completed_tools=3' in line
    assert '3 tools completed' in line
    assert 'last_tool=Read' in line
    assert 'progress unknown' not in line


def test_user_input_interaction_is_denied_and_fails(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    drive_to_send(session, summary)
    reply = session.event(summary, {'id': 99, 'method': 'interaction/requestUserInput',
                                    'params': {}})
    assert reply == [{'id': 99, 'result': zcode.INTERACTION_DENY}]
    session.event(summary, {'method': 'session/event', 'params': {'type': 'turn.completed',
                                                                  'payload': {}}})
    assert session.done and summary.reason == 'needs_user_input'


def test_permission_request_is_declined_recorded_and_turn_continues(tmp_path, monkeypatch):
    # Params shape from a live run: a risky command asked for approval.
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    drive_to_send(session, summary)
    reply = session.event(summary, {'id': 99, 'method': 'interaction/requestPermission', 'params': {
        'toolName': 'Bash', 'input': {'command': 'rm -rf ../outside', 'description': 'cleanup'},
        'reason': 'Dangerous command requires your approval for this execution',
        'riskLevel': 'high', 'requestId': 'perm_1', 'sessionId': 'sid-1'}})
    assert reply == [{'id': 99, 'result': zcode.INTERACTION_DENY}]
    messages = session.event(summary, {'method': 'session/event', 'params': {
        'type': 'turn.completed', 'payload': {}}})
    assert summary.reason is None and not session.done
    assert messages[0]['method'] == 'session/messages'
    assert summary.denied_actions[0]['tool'] == 'Bash'
    detail = json.loads(summary.denied_actions[0]['detail'])
    assert detail['input']['command'] == 'rm -rf ../outside' and detail['riskLevel'] == 'high'


def test_host_requests_and_provider_key_reply(tmp_path, monkeypatch):
    table_path, _, table = selection(tmp_path, monkeypatch)
    session = zcode.ProtocolSession(PROVIDER, MODEL, 'high', tmp_path, table_path, 'key', table)
    summary = native.Summary()
    assert session.event(summary, {'id': 1, 'method': 'session/requestRuntimePreferences'}) == [
        {'id': 1, 'result': zcode.RUNTIME_PREFERENCES_REPLY}]
    assert session.event(summary, {'id': 2, 'method': 'interaction/requestProviderRuntimeHeaders'}) == [
        {'id': 2, 'result': {'headersApplied': True, 'requestAuth': {'apiKey': 'key'}}}]
    assert session.event(summary, {'id': 4, 'method': 'interaction/requestOfficialMcpAuthHeaders',
                                   'params': {'mcpKey': 'image_search'}}) == [
        {'id': 4, 'error': {'code': -32601, 'message': 'headless worker: unsupported'}}]
    assert not session.done and summary.reason is None
    assert session.event(summary, {'id': 3, 'method': 'unknown/method'}) == [
        {'id': 3, 'error': {'code': -32601, 'message': 'headless worker: unsupported'}}]
    assert session.done and summary.reason == 'native_error'


def test_secret_never_in_argv_env_or_session_repr(tmp_path, monkeypatch):
    table_path, _, _ = selection(tmp_path, monkeypatch, key='top-secret-value')
    cjs = tmp_path / 'zcode.cjs'
    cjs.write_text('// fake\n', encoding='utf-8')
    argv, env, session = zcode.command([str(cjs)], tmp_path, PROVIDER, MODEL, 'high', 'review', 'none')
    assert 'top-secret-value' not in ' '.join(argv)
    assert 'top-secret-value' not in ' '.join(env.values())
    assert 'top-secret-value' in session.plan_key


@pytest.mark.skipif(not shutil.which('node'), reason='node is required for the encrypted credential')
def test_encrypted_credential_is_decrypted(tmp_path, monkeypatch):
    secret = 'test-credential-secret'
    proc = subprocess.run(['node', '-e', '''
const crypto = require("crypto");
let plain = "";
process.stdin.on("data", c => plain += c);
process.stdin.on("end", () => {
  const iv = crypto.randomBytes(12);
  const key = crypto.createHash("sha256").update(process.env.ZCODE_CREDENTIAL_SECRET).digest();
  const d = crypto.createCipheriv("aes-256-gcm", key, iv);
  const data = Buffer.concat([d.update(plain, "utf8"), d.final()]);
  process.stdout.write("enc:v1:" + [iv.toString("base64url"),
                                    d.getAuthTag().toString("base64url"),
                                    data.toString("base64url")].join("."));
});
'''], input='plain-plan-key', capture_output=True, text=True, check=True,
        env={**os.environ, 'ZCODE_CREDENTIAL_SECRET': secret})
    home = tmp_path / 'zhome'
    (home / 'v2').mkdir(parents=True)
    (home / 'v2' / 'credentials.json').write_text(json.dumps({
        'account-provider:cp:account:zai-individual-coding-plan:account:u:api-key':
            proc.stdout.strip(),
    }), encoding='utf-8')
    monkeypatch.setenv('ZCODE_CREDENTIAL_SECRET', secret)
    assert zcode._load_plan_key(home, PROVIDER) == 'plain-plan-key'


def test_start_plan_uses_login_jwt(tmp_path):
    home = tmp_path / 'zhome'
    (home / 'v2').mkdir(parents=True)
    (home / 'v2' / 'credentials.json').write_text(json.dumps({
        'zcodejwttoken': 'login-jwt',
    }), encoding='utf-8')
    assert zcode._load_plan_key(home, 'account:zai-start-plan', 'start-plan') == 'login-jwt'
    # Other plan modes keep the api-key contract even with a login JWT present.
    with pytest.raises(ValueError, match='plan key unavailable'):
        zcode._load_plan_key(home, PROVIDER, 'individual-coding-plan')
    empty = tmp_path / 'zempty'
    (empty / 'v2').mkdir(parents=True)
    (empty / 'v2' / 'credentials.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='plan key unavailable'):
        zcode._load_plan_key(empty, 'account:zai-start-plan', 'start-plan')


def test_start_plan_prefers_login_jwt_over_stored_api_key(tmp_path):
    home = tmp_path / 'zhome'
    (home / 'v2').mkdir(parents=True)
    (home / 'v2' / 'credentials.json').write_text(json.dumps({
        'account-provider:cp:account:zai-start-plan:account:u:api-key': 'stored-key',
        'zcodejwttoken': 'login-jwt',
    }), encoding='utf-8')
    assert zcode._load_plan_key(home, 'account:zai-start-plan', 'start-plan') == 'login-jwt'
    assert zcode._load_plan_key(home, 'account:zai-start-plan') == 'stored-key'


def test_command_carries_plan_mode_for_start_plan(tmp_path, monkeypatch):
    table_path = write_table(tmp_path)
    home = tmp_path / 'zhome'
    (home / 'v2').mkdir(parents=True)
    (home / 'v2' / 'credentials.json').write_text(json.dumps({
        'zcodejwttoken': 'login-jwt',
    }), encoding='utf-8')
    monkeypatch.setenv('ZCODE_HOME', str(home))
    monkeypatch.setenv('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE', str(table_path))
    cjs = tmp_path / 'zcode.cjs'
    cjs.write_text('// fake\n', encoding='utf-8')
    _, _, session = zcode.command([str(cjs)], tmp_path, 'account:zai-start-plan',
                                  'GLM-5.3-Flash', 'high', 'review', 'none')
    assert session.plan_key == 'login-jwt'


def test_load_login_jwt(tmp_path):
    home = tmp_path / 'zhome'
    (home / 'v2').mkdir(parents=True)
    (home / 'v2' / 'credentials.json').write_text(json.dumps({'zcodejwttoken': ' login-jwt '}),
                                                  encoding='utf-8')
    assert zcode.load_login_jwt(home) == 'login-jwt'
    blank = tmp_path / 'zblank'
    (blank / 'v2').mkdir(parents=True)
    (blank / 'v2' / 'credentials.json').write_text(json.dumps({'zcodejwttoken': '   '}),
                                                   encoding='utf-8')
    assert zcode.load_login_jwt(blank) is None
    (blank / 'v2' / 'credentials.json').write_text('{}', encoding='utf-8')
    assert zcode.load_login_jwt(blank) is None


class ScriptedSession:
    def __init__(self, answer='ok'):
        self.answer = answer
        self.done = False
        self.prompt = None

    def initial(self, summary, prompt):
        self.prompt = prompt
        return [{'id': 1, 'method': 'start'}]

    def event(self, summary, event):
        if event.get('id') == 1:
            summary.finished = True
            summary.answer = self.answer
            summary.session_id = 'sid-scripted'
            self.done = True
        return []

    def poll(self, summary):
        return []

    def stderr(self, summary, line):
        pass


def fake_worker(tmp_path, code):
    path = tmp_path / 'worker.py'
    path.write_text(code, encoding='utf-8')
    return [sys.executable, str(path)]


class DummyAdapter:
    def stderr(self, summary, line):
        pass

    def event(self, summary, event):
        pass


def test_runner_session_prompt_and_grace_kill(repo, tmp_path, monkeypatch):
    session = ScriptedSession('answer')
    command = fake_worker(tmp_path, 'import json,sys,time\n'
                                    'sys.stdin.readline()\n'
                                    'print(json.dumps({"id": 1, "result": {}}), flush=True)\n'
                                    'time.sleep(30)\n')
    monkeypatch.setattr(runner, 'DONE_GRACE_SECONDS', 0.2)
    result = runner.run(DummyAdapter(), command, {}, repo, 'ASSIGNMENT', 'zcode/test',
                        interval=0, session=session)
    assert result['status'] == 'completed'
    assert result['answer'] == 'answer'
    assert session.prompt.endswith(runner.OWNER_INSTRUCTION)


def test_runner_session_empty_answer_is_no_terminal_answer(repo, tmp_path, monkeypatch):
    session = ScriptedSession('')
    command = fake_worker(tmp_path, 'import json,sys,time\n'
                                    'sys.stdin.readline()\n'
                                    'print(json.dumps({"id": 1, "result": {}}), flush=True)\n'
                                    'time.sleep(30)\n')
    monkeypatch.setattr(runner, 'DONE_GRACE_SECONDS', 0.2)
    result = runner.run(DummyAdapter(), command, {}, repo, 'ASSIGNMENT', 'zcode/test',
                        interval=0, session=session)
    assert result['status'] == 'failed'
    assert result['stop_reason'] == 'no_terminal_answer'


def test_runner_session_needs_user_input_marker(repo, tmp_path, monkeypatch):
    session = ScriptedSession('NEEDS_USER_INPUT\nWhich plan should be used?')
    command = fake_worker(tmp_path, 'import json,sys,time\n'
                                    'sys.stdin.readline()\n'
                                    'print(json.dumps({"id": 1, "result": {}}), flush=True)\n'
                                    'time.sleep(30)\n')
    monkeypatch.setattr(runner, 'DONE_GRACE_SECONDS', 0.2)
    result = runner.run(DummyAdapter(), command, {}, repo, 'ASSIGNMENT', 'zcode/test',
                        interval=0, session=session)
    assert result['status'] == 'needs_input'
    assert result['stop_reason'] == 'worker_question'
    assert result['answer'].startswith('NEEDS_USER_INPUT')
    assert result['session_id'] == 'sid-scripted'
    assert result['reason'] == 'Worker requested an owner decision'


def test_runner_session_needs_user_input_marker_survives_resume(repo, tmp_path, monkeypatch):
    session = ScriptedSession('NEEDS_USER_INPUT\nWhich plan should be used?')
    command = fake_worker(tmp_path, 'import json,sys,time\n'
                                    'sys.stdin.readline()\n'
                                    'print(json.dumps({"id": 1, "result": {}}), flush=True)\n'
                                    'time.sleep(30)\n')
    monkeypatch.setattr(runner, 'DONE_GRACE_SECONDS', 0.2)
    result = runner.run(DummyAdapter(), command, {}, repo, 'ASSIGNMENT', 'zcode/test',
                        interval=0, session=session, resume_session='sid-scripted')
    assert result['status'] == 'needs_input'
    assert result['stop_reason'] == 'worker_question'
    assert result['resumed'] is True
    assert result['session_id'] == 'sid-scripted'


def test_registry_accepts_colon_provider_and_zcode_model(tmp_path):
    data = registry.load(tmp_path / 'config.json')
    registry.add(data, f'zcode/{PROVIDER}/{MODEL}', ['high'], 'high')
    registry.save(tmp_path / 'config.json', data)
    rows = cli.model_rows(registry.load(tmp_path / 'config.json'))
    row = next(row for row in rows if row['address'] == f'zcode/{PROVIDER}/{MODEL}')
    assert row['available'] is True
    assert row['default_effort'] == 'high'


FAKE_SERVER = r'''
import json, sys

READ = {"settings": {"model": {"current": {
    "modelId": "%(model)s", "providerId": "%(provider)s",
    "options": {"reasoningLevel": "high"}}}}}
MODE = "%(mode)s"

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    rid = msg.get("id")
    method = msg.get("method")
    if rid is None or method is None:
        continue
    if method == "provider/updateAccountConfig":
        send({"id": rid, "result": {}})
    elif method == "session/create":
        send({"id": rid, "result": {"session": {"sessionId": "sid-e2e"}}})
    elif method == "session/resume":
        if MODE == "resume-notfound":
            send({"id": rid, "error": {"code": -32004,
                                       "message": "Session not found: " + msg["params"]["sessionId"]}})
        elif MODE == "resume-mismatch":
            send({"id": rid, "result": {"session": {"sessionId": "sess_00000000-0000-0000-0000-000000000000"}}})
        else:
            send({"id": rid, "result": {"session": {"sessionId": msg["params"]["sessionId"]}}})
    elif method == "session/setMode":
        send({"id": rid, "result": {}})
    elif method == "session/setModel":
        send({"id": rid, "result": {}})
    elif method == "session/read":
        send({"id": rid, "result": READ})
    elif method == "session/subscribe":
        send({"id": rid, "result": {}})
    elif method == "session/send":
        send({"id": rid, "result": {}})
        if MODE == "quota":
            send({"method": "session/event", "params": {"type": "turn.failed", "payload": {
                "error": {"code": 1301, "message": "Insufficient balance or token expired"}}}})
        elif MODE == "mismatch":
            pass
        else:
            send({"id": 900, "method": "interaction/requestProviderRuntimeHeaders"})
            send({"method": "session/event", "params": {"type": "turn.completed",
                                                        "payload": {"usage": {"inputTokens": 5},
                                                                    "response": "e2e answer"}}})
    elif method == "session/messages":
        send({"id": rid, "result": {"messages": [{"info": {"role": "assistant"},
                                                  "parts": [{"text": "e2e answer"}]}]}})
'''


def e2e_setup(tmp_path, monkeypatch, mode='ok', mismatch=False):
    table_path = write_table(tmp_path)
    home = make_home(tmp_path)
    monkeypatch.setenv('ZCODE_HOME', str(home))
    monkeypatch.setenv('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE', str(table_path))
    server = tmp_path / 'fake_zcode.py'
    read_model = 'GLM-5.2' if mismatch else MODEL
    server.write_text(FAKE_SERVER % {'provider': PROVIDER, 'model': read_model, 'mode': mode},
                      encoding='utf-8')
    monkeypatch.setattr(zcode, 'resolve', lambda: [sys.executable, str(server)])
    repo = tmp_path / 'e2e-repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    config = tmp_path / 'delegator.json'
    config.write_text(json.dumps({
        'version': 1,
        'harnesses': {'zcode': {'enabled': True, 'providers': {PROVIDER: {
            'enabled': True, 'quota': {'blocked': False, 'reset_at': None},
            'models': {MODEL: {'enabled': True, 'efforts': ['high'], 'default_effort': 'high'}}}}}},
        'execution': {'status_interval_seconds': 0, 'max_runtime_seconds': 30},
    }), encoding='utf-8')
    return repo, config, server


def test_e2e_success_and_secret_not_logged(tmp_path, monkeypatch, capsys):
    repo, config, _ = e2e_setup(tmp_path, monkeypatch)
    log_dir = tmp_path / 'log'
    code = cli.main(['exec', '-m', f'zcode/{PROVIDER}/{MODEL}', '-e', 'high',
                     '--mode', 'review', '--repo', str(repo), '-p', 'review this',
                     '--config', str(config), '--log-dir', str(log_dir)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload['status'] == 'completed'
    assert payload['answer'] == 'e2e answer'
    assert payload['reported']['model'] == MODEL
    assert payload['reported']['provider'] == PROVIDER
    assert payload['session_id'] == 'sid-e2e'
    log = Path(payload['log_path']).read_text(encoding='utf-8')
    assert 'secret-key' not in log
    assert 'secret-key' not in json.dumps(payload)


def test_e2e_quota_blocks_provider(tmp_path, monkeypatch, capsys):
    repo, config, _ = e2e_setup(tmp_path, monkeypatch, mode='quota')
    code = cli.main(['exec', '-m', f'zcode/{PROVIDER}/{MODEL}', '-e', 'high',
                     '--mode', 'review', '--repo', str(repo), '-p', 'review this',
                     '--config', str(config)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload['status'] == 'failed' and payload['stop_reason'] == 'quota'
    saved = json.loads(config.read_text(encoding='utf-8'))
    quota = saved['harnesses']['zcode']['providers'][PROVIDER]['quota']
    assert quota['blocked'] is True


def test_e2e_model_mismatch_does_not_send(tmp_path, monkeypatch, capsys):
    repo, config, _ = e2e_setup(tmp_path, monkeypatch, mode='mismatch', mismatch=True)
    code = cli.main(['exec', '-m', f'zcode/{PROVIDER}/{MODEL}', '-e', 'high',
                     '--mode', 'review', '--repo', str(repo), '-p', 'review this',
                     '--config', str(config)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload['stop_reason'] == 'reported_selection_mismatch'


def test_e2e_resume_completes_with_confirmed_id(tmp_path, monkeypatch, capsys):
    repo, config, _ = e2e_setup(tmp_path, monkeypatch, mode='resume-ok')
    code = cli.main(['exec', '-m', f'zcode/{PROVIDER}/{MODEL}', '-e', 'high',
                     '--resume-session', RESUME_ID, '--repo', str(repo), '-p', 'delta only',
                     '--config', str(config), '--log-dir', str(tmp_path / 'logs')])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload['status'] == 'completed'
    assert payload['resumed'] is True and payload['session_id'] == RESUME_ID
    assert payload['answer'] == 'e2e answer'
    log = Path(payload['log_path']).read_text(encoding='utf-8')
    # The worker log holds only native output; the resume echo proves the
    # resume path ran, and the create-only session id proves create did not.
    assert RESUME_ID in log and 'sid-e2e' not in log


def test_e2e_resume_not_found_fails_unconfirmed(tmp_path, monkeypatch, capsys):
    repo, config, _ = e2e_setup(tmp_path, monkeypatch, mode='resume-notfound')
    code = cli.main(['exec', '-m', f'zcode/{PROVIDER}/{MODEL}', '-e', 'high',
                     '--resume-session', RESUME_ID, '--repo', str(repo), '-p', 'delta only',
                     '--config', str(config)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload['status'] == 'failed'
    assert payload['stop_reason'] == 'resume_session_unconfirmed'
    assert 'Session not found' in payload['reason']
    saved = json.loads(config.read_text(encoding='utf-8'))
    quota = saved['harnesses']['zcode']['providers'][PROVIDER]['quota']
    assert quota['blocked'] is False


def test_e2e_resume_mismatch_fails_closed(tmp_path, monkeypatch, capsys):
    repo, config, _ = e2e_setup(tmp_path, monkeypatch, mode='resume-mismatch')
    code = cli.main(['exec', '-m', f'zcode/{PROVIDER}/{MODEL}', '-e', 'high',
                     '--resume-session', RESUME_ID, '--repo', str(repo), '-p', 'delta only',
                     '--config', str(config)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload['status'] == 'failed'
    assert payload['stop_reason'] == 'resume_session_mismatch'
    assert payload['session_id'] == 'sess_00000000-0000-0000-0000-000000000000'
