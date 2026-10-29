"""OpenCode JSON adapter, with native tool permissions rather than --auto."""

import json
import os

from native import redact


# Short deny list for `auto`, from lead/worker command history: these shapes
# never occurred in legitimate cleanup, so they are denied regardless of
# directory; path protection itself is external_directory.
SHELL_DENY_PATTERNS = ('git push*', 'git clean*', 'format *', 'format.com*', 'Format-Volume*', 'shutdown*')


def permission_map(mode, shell):
    permission = {'*': 'deny', 'read': 'allow', 'glob': 'allow', 'grep': 'allow', 'list': 'allow'}
    if mode == 'implementation':
        # A headless `ask` is an auto-reject that ends the turn, so unlisted tools are
        # hidden with `deny` instead; `todowrite` only stores the session's todo list.
        permission = {'*': 'deny', 'read': 'allow', 'glob': 'allow', 'grep': 'allow',
                      'list': 'allow', 'edit': 'allow', 'todowrite': 'allow',
                      'task': 'deny', 'external_directory': 'deny'}
    if shell == 'auto':
        # Object rules are ordered and the last matching command rule wins,
        # so the deny patterns must follow the broad allow.
        permission['bash'] = {'*': 'allow', **{pattern: 'deny' for pattern in SHELL_DENY_PATTERNS}}
        permission['external_directory'] = 'deny'
    elif shell == 'full':
        permission = {'*': 'allow'}
    return permission


def command(executable, repo, provider, model, effort, mode, shell, resume_session=None,
            git_check=True, override=False):
    argv = [executable, 'run', '--dir', str(repo), '--format', 'json', '--model', provider + '/' + model]
    if effort:
        argv += ['--variant', effort]
    if resume_session is not None:
        # Never --auto and never --continue: an explicit session only.
        argv += ['--session', resume_session]
    try:
        config = json.loads(os.environ.get('OPENCODE_CONFIG_CONTENT') or '{}')
    except ValueError:
        raise ValueError('Existing OPENCODE_CONFIG_CONTENT must contain valid JSON; it was not replaced') from None
    if not isinstance(config, dict):
        raise ValueError('Existing OPENCODE_CONFIG_CONTENT must be a JSON object')
    config['permission'] = permission_map(mode, shell)
    return argv, {'OPENCODE_CONFIG_CONTENT': json.dumps(config)}


_error_cause_pending = False


def stderr(summary, line):
    # Actual top-level CLI errors, never arbitrary diagnostic prose. The CLI's
    # top-level handler prints "Error: <generic>" and then the stringify of the
    # real exception on the next line (opencode 1.19.4), so the single nonempty
    # line that follows refines summary.error while stop_reason stays as
    # classified from the generic text (issue #112). The pending flag resets on
    # the cause line, survives blank lines only, and captures nothing when this
    # attempt has not reported the generic error itself.
    global _error_cause_pending
    if line.startswith('Error:') or line.startswith('ERROR:'):
        summary.fail(line.split(':', 1)[1])
        _error_cause_pending = True
        return
    if _error_cause_pending and line.strip():
        _error_cause_pending = False
        if summary.error:
            summary.error = redact(line.strip())[:500]


def event(summary, event):
    if event.get('sessionID'):
        summary.session_id = event['sessionID']
    kind = event.get('type')
    part = event.get('part', {})
    if isinstance(kind, str) and (kind == 'error' or kind.endswith('_error') or kind.endswith('.error')):
        error = event.get('error')
        if error is None and isinstance(part, dict):
            error = part.get('error')
        if error is None:
            error = event.get('message', 'Native error event')
        summary.fail(error if isinstance(error, str) else json.dumps(error, ensure_ascii=False))
    elif kind == 'text':
        summary.answer = part.get('text', '')
    elif kind == 'step_finish':
        reason = part.get('reason')
        summary.finished = reason == 'stop'
        summary.limited = reason == 'length'
        tokens = part.get('tokens', {})
        values = {k: v for k, v in tokens.items() if type(v) in (int, float)}
        values.update({'cache_' + k: v for k, v in tokens.get('cache', {}).items()})
        values['cost'] = part.get('cost', 0)
        summary.add_usage(values)
    elif kind == 'tool_use':
        summary.last_tool = part.get('tool')
        summary.tools_observed = True
        state = part.get('state', {})
        if state.get('status') in ('completed', 'error'):
            summary.completed_tools += 1
        if state.get('status') == 'error':
            # A tool failure does not establish provider quota exhaustion.
            message = str(state.get('error', 'Tool failed'))
            if any(p in message.lower() for p in ('permission', 'approval', 'rejected')):
                summary.deny(part.get('tool'), message)
    info = event.get('info', {})
    for key, native in (('model', 'modelID'), ('provider', 'providerID'), ('effort', 'variant')):
        if isinstance(info.get(native), str):
            summary.reported[key] = info[native]
