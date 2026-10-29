"""Claude Code adapter: native permission modes over stream-json."""

import json
import re

QUOTA_LINE = re.compile(r'Claude AI usage limit reached\|\d')
# --shell selects the native permission mode; `none` keeps the mode-based
# plan/acceptEdits split. `none` also pins --permission-prompts none; auto/full
# drop it so the native classifier can auto-approve plainly safe commands
# without a prompt (verified live: git status and python --version run, while
# risky shapes like python -c or an outside rm -rf are denied headless).
PERMISSION_MODE = {'none': None, 'auto': 'auto', 'full': 'bypassPermissions'}


def permission_mode(mode, shell):
    return PERMISSION_MODE[shell] or ('plan' if mode == 'review' else 'acceptEdits')


def command(executable, repo, provider, model, effort, mode, shell, resume_session=None,
            git_check=True, override=False):
    if provider != 'anthropic':
        raise ValueError('Claude Code supports only provider anthropic (the native Anthropic connection)')
    argv = [executable, '--print', '--verbose', '--output-format', 'stream-json',
            '--permission-mode', permission_mode(mode, shell)]
    if shell == 'none':
        argv += ['--permission-prompts', 'none']
    argv += ['--model', model]
    if effort:
        argv += ['--effort', effort]
    if resume_session is not None:
        # Explicit resume only; --continue can pick up a foreign session.
        argv += ['--resume', resume_session]
    # The prompt travels on stdin; no positional placeholder and no bypass flags.
    return argv, {}


def stderr(summary, line):
    text = line.strip()
    if QUOTA_LINE.search(text):
        summary.fail(text)
    elif text.startswith('Error:') or text.startswith('ERROR:'):
        # Actual top-level CLI errors, never arbitrary diagnostic prose.
        summary.fail(text.split(':', 1)[1])


def _blocks(event):
    message = event.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def event(summary, event):
    kind = event.get('type')
    if kind == 'rate_limit_event':
        # Advisory only; its fields are not scanned as error markers.
        return
    if kind == 'system' and event.get('subtype') == 'init':
        if isinstance(event.get('session_id'), str):
            summary.session_id = event['session_id']
        if isinstance(event.get('model'), str):
            # Native-reported selection; assistant message models are ignored.
            summary.reported['model'] = event['model']
        return
    if kind in ('assistant', 'user'):
        for block in _blocks(event):
            if not isinstance(block, dict):
                continue
            block_type = block.get('type')
            if block_type == 'text' and isinstance(block.get('text'), str):
                summary.answer += block['text']
            elif block_type == 'tool_use' and isinstance(block.get('name'), str):
                summary.last_tool = block['name']
            elif block_type == 'tool_result':
                summary.completed_tools += 1
        return
    if kind == 'result':
        if isinstance(event.get('session_id'), str):
            summary.session_id = event['session_id']
        usage = event.get('usage')
        if isinstance(usage, dict):
            summary.add_usage(usage)
        cost = event.get('total_cost_usd')
        if type(cost) in (int, float):
            summary.add_usage({'cost': cost})
        denials = event.get('permission_denials')
        for item in denials if isinstance(denials, list) else []:
            if isinstance(item, dict):
                summary.deny(item.get('tool_name'), json.dumps(item.get('tool_input'), ensure_ascii=False))
        answer = event.get('result')
        if event.get('is_error'):
            summary.fail(answer if isinstance(answer, str) and answer
                         else str(event.get('subtype') or 'Native result error'))
            return
        summary.finished = event.get('subtype') == 'success'
        if isinstance(answer, str) and answer.strip():
            summary.answer = answer
