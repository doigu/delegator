"""Antigravity CLI adapter, using native plan/accept-edits modes."""

import json


def command(executable, repo, provider, model, effort, mode, shell, resume_session=None,
            git_check=True, override=False):
    if provider != 'gemini':
        raise ValueError('Antigravity supports only provider gemini (the delegator connection label)')
    if shell == 'auto':
        raise ValueError('Antigravity has no auto shell level (--mode auto is unavailable '
                         'for this account)')
    argv = [executable, '--output-format', 'stream-json',
            '--mode', 'plan' if mode == 'review' else 'accept-edits', '--model', model]
    if shell == 'full':
        argv += ['--dangerously-skip-permissions']
    if effort:
        argv += ['--effort', effort]
    if resume_session is not None:
        # --conversation only; --continue is banned because it can pick up a
        # foreign session. A foreign or malformed id makes agy silently start
        # a new conversation, so runner confirms the reported id after the run.
        argv += ['--conversation', resume_session]
    return argv, {}


def stderr(summary, line):
    # Explicit CLI errors and the native gRPC error name. Classification still
    # requires quota context; a bare RESOURCE_EXHAUSTED stays native_error.
    text = line.strip()
    if text.startswith('Error:') or text.startswith('ERROR:'):
        summary.fail(text.split(':', 1)[1])
    elif text.startswith('RESOURCE_EXHAUSTED'):
        summary.fail(text)
    elif text.startswith('jetski:') and 'auto-denied' in text:
        # Native headless denial hint (verified on stderr); guidance, not a failure.
        summary.warn(text)


def event(summary, event):
    kind = event.get('type')
    is_error = kind == 'error' or 'error' in event or event.get('status') == 'error'
    if is_error:
        error = event.get('error')
        if error is None:
            error = event.get('message', 'Native error event')
        summary.fail(error if isinstance(error, str) else json.dumps(error, ensure_ascii=False))
        return
    if isinstance(event.get('conversation_id'), str):
        summary.session_id = event['conversation_id']
    init = event.get('init')
    if isinstance(init, dict) and isinstance(init.get('model'), str):
        summary.reported['model'] = init['model']
    step = event.get('step_update')
    if isinstance(step, dict):
        if isinstance(step.get('conversation_id'), str):
            summary.session_id = step['conversation_id']
        if isinstance(step.get('text_delta'), str):
            summary.answer += step['text_delta']
    result = event.get('result')
    if isinstance(result, dict):
        if isinstance(result.get('conversation_id'), str):
            summary.session_id = result['conversation_id']
        if isinstance(result.get('usage'), dict):
            summary.add_usage(result['usage'])
        response = result.get('response')
        if isinstance(response, str) and response:
            summary.answer = response
        denied = result.get('denied_actions')
        for item in denied if isinstance(denied, list) else []:
            tool = (item.get('display_name') or item.get('action')) if isinstance(item, dict) else None
            summary.deny(tool, json.dumps(item, ensure_ascii=False))
        status = result.get('status')
        if isinstance(status, str) and status.upper() != 'SUCCESS':
            summary.fail(response if isinstance(response, str) and response.strip()
                         else 'Native result status: ' + status)
            return
    if isinstance(event.get('response'), str):
        summary.answer += event['response']
    # Older stream variants have no terminal event; exit code 0 plus nonempty
    # text remains the fallback. A non-SUCCESS result is classified above.
    summary.finished = True
