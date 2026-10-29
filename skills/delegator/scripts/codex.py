"""Codex exec adapter. Explicit resume only; no adapter retries or fallback
(the wrapper retries classified capacity failures)."""

import json
import re


# The sandbox tracks the --shell level; the review/implementation mode does
# not change it: none reads only, auto scopes writes to the working copy,
# full routes approvals through the native automatic reviewer. The sandbox
# bypass flag and `-s danger-full-access` still leave the headless approval
# gate in place for destructive commands on Windows (live check, 2026-10-24);
# --approve-for-me is the only working route past it.
SANDBOX_MODE = {'none': 'read-only', 'auto': 'workspace-write'}


def sandbox_args(repo, shell):
    # `exec resume` accepts neither -C nor -s: cwd comes from the process
    # and the sandbox travels through -c. It also rejects --approve-for-me,
    # so full passes its -c equivalent (verified live): the automatic
    # reviewer on the workspace-write sandbox.
    if shell == 'full':
        return ['-c', 'sandbox_mode="workspace-write"',
                '-c', 'approvals_reviewer="auto_review"',
                '-c', 'approval_policy="on-request"']
    args = ['-c', 'windows.sandbox=mxc']
    if shell == 'none':
        args += ['-c', 'sandbox_mode="read-only"']
    else:
        args += ['-c', 'sandbox_workspace_write.writable_roots=["' + repo.as_posix() + '"]']
    return args


def command(executable, repo, provider, model, effort, mode, shell, resume_session=None,
            git_check=True, override=False):
    if provider != 'openai':
        raise ValueError('Codex supports only provider openai (the native OpenAI connection)')
    if shell not in ('none', 'auto', 'full'):
        raise ValueError('Codex supports only --shell none, auto, or full')
    if resume_session is not None:
        argv = [executable, 'exec', 'resume', resume_session, '--json']
    else:
        argv = [executable, 'exec', '--json', '-C', str(repo)]
        # One-shot runs use a throwaway plain directory; the sandbox level
        # still applies, only the native Git working-copy check is waived.
        if not git_check:
            argv.append('--skip-git-repo-check')
        if shell == 'full':
            argv.append('--approve-for-me')
        else:
            argv += ['-s', SANDBOX_MODE[shell]]
    # Explicit on fresh and resumed calls: the provider slug pins the native
    # OpenAI connection instead of inheriting the Codex config's last selection.
    argv += ['-m', model, '-c', 'model_provider="openai"']
    if effort:
        argv += ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
    if resume_session is not None:
        argv += sandbox_args(repo, shell)
    return argv + ['-'], {}


def stderr(summary, line):
    match = re.match(r'^\s*ERROR:\s*(.+)$', line)
    if match:
        summary.fail(match[1])
    # A command the sandbox policy refused to run (for example an escalation
    # with no approver); the model is told and may continue.
    match = re.search(r'Rejected\((.*) rejected: ([^"\\]+)', line)
    if match:
        command = match[1]
        for _ in range(4):  # the router log nests Debug-escaped strings
            command = command.replace('\\\\', '\\').replace('\\"', '"')
        summary.deny('exec_command', match[2].strip() + ': ' + command.strip('"`'))


def event(summary, event):
    kind = event.get('type')
    if kind == 'thread.started':
        summary.session_id = event.get('thread_id')
    elif kind in ('error', 'turn.failed'):
        error = event.get('error') or event.get('message') or 'Native turn failed'
        summary.fail(error if isinstance(error, str) else json.dumps(error))
    elif kind == 'turn.completed':
        summary.finished = True
        summary.add_usage(event.get('usage', {}))
    item = event.get('item', {})
    if kind == 'item.completed' and item.get('type') == 'agent_message':
        summary.answer = item.get('text', '')
    if item.get('type') in ('command_execution', 'mcp_tool_call', 'file_change'):
        summary.last_tool = item.get('type')
        if kind == 'item.completed':
            summary.completed_tools += 1
    # Some versions emit metadata; absence remains unknown.
    for key in ('model', 'effort'):
        if isinstance(event.get(key), str):
            summary.reported[key] = event[key]
