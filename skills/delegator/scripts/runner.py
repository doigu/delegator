"""One foreground native invocation, streamed log, in-memory observations."""

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from native import Summary, redact
from processes import ProcessTree, RepoLock, spawn


DONE_GRACE_SECONDS = 5.0
OWNER_INSTRUCTION = ('\nIf an owner decision is required, stop and start the final answer with '
                     'NEEDS_USER_INPUT followed by the question; this is a normal hand-back, '
                     'not a failure. Do not delegate or change the selected model.')


def status(stream, message):
    try:
        print(message, file=stream, flush=True)
    except (OSError, ValueError):
        pass


def status_line(elapsed, last_event, summary, target):
    age = 'unknown' if last_event is None else f'{int(elapsed - last_event)}s'
    tools = str(summary.completed_tools) if summary.tools_observed else 'unknown'
    if summary.tools_observed:
        activity = f'no new events for {age}; process alive; {summary.completed_tools} tools completed'
    else:
        activity = f'no new events for {age}; process alive; progress unknown'
    return (f'running | elapsed={int(elapsed)}s | target={target} | last_event={age} | '
            f'last_tool={summary.last_tool or "unknown"} | completed_tools={tools} | {activity}')


def git_paths(repo):
    result = subprocess.run(['git', '-c', 'core.quotepath=false', 'status', '--porcelain=v1', '-z',
                             '--untracked-files=all'], cwd=repo, capture_output=True)
    if result.returncode:
        raise ValueError('Cannot read Git status for the working copy')
    entries, paths, index = result.stdout.decode('utf-8', 'replace').split('\0'), set(), 0
    while index < len(entries):
        entry = entries[index]
        if entry:
            paths.add(entry[3:])
            if entry[0] in 'RC' or entry[1] in 'RC':
                index += 1
                paths.add(entries[index])
        index += 1
    return paths


class CodedError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def permanent_log_root():
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))
    else:
        base = os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state'))
    return Path(base) / 'delegator'


def _inside_git_repo(path):
    probe = Path(path)
    while not probe.exists():
        probe = probe.parent
    check = subprocess.run(['git', '-C', str(probe), 'rev-parse', '--show-toplevel'],
                           capture_output=True)
    return check.returncode == 0


def log_file(directory=None):
    if directory:
        candidates = [Path(directory).expanduser().resolve()]
    else:
        candidates = [Path(tempfile.gettempdir()) / 'delegator', permanent_log_root()]
    rejected = []
    for root in candidates:
        if _inside_git_repo(root):
            rejected.append(f'{root}: inside a Git repository')
            continue
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            rejected.append(f'{root}: {exc}')
            continue
        handle = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root,
                                             prefix='run-', suffix='.jsonl', delete=False)
        return handle, str(Path(handle.name).resolve())
    raise CodedError('log_directory_inside_git',
                     'Log directory candidates are inside Git repositories or unusable: '
                     + '; '.join(rejected) + '; pass --log-dir outside every Git repository')


def run(adapter, command, env, repo, prompt, target, interval=1800, runtime=None, log_dir=None,
        resume_session=None, session=None, track=True):
    started = time.monotonic()
    summary, warnings = Summary(), []
    log, log_path = log_file(log_dir)
    worker, tree = None, None
    result_status, stop_reason = None, None
    resumed = resume_session is not None
    protocol_done, done_at, stdin_closed = False, None, False
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    before, after = set(), set()
    lock = RepoLock(repo) if track else None
    def cancel(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, cancel)
    try:
        if lock is not None:
            lock.__enter__()
        if track:
            before = git_paths(repo)
        status(sys.stderr, f'starting | target={target} | log={log_path}')
        worker = spawn(command, repo, {**os.environ, **env, 'PYTHONIOENCODING': 'utf-8'})
        try:
            tree = ProcessTree(worker)
        except Exception:
            worker.kill()
            worker.wait()
            raise
        events = queue.Queue()
        def read(channel, stream):
            try:
                for line in stream:
                    events.put((channel, line))
            finally:
                stream.close()
                events.put((channel, None))
        def write_prompt():
            try:
                worker.stdin.write(prompt + OWNER_INSTRUCTION)
                worker.stdin.close()
            except (OSError, ValueError):
                pass
        def close_stdin():
            nonlocal stdin_closed
            if stdin_closed:
                return
            stdin_closed = True
            try:
                if worker.stdin is not None:
                    worker.stdin.close()
            except (OSError, ValueError):
                pass
        def write_outbound(messages):
            if not messages or stdin_closed or worker is None or worker.stdin is None:
                return
            if isinstance(messages, dict):
                messages = [messages]
            try:
                for message in messages:
                    worker.stdin.write(json.dumps(message) + '\n')
                worker.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                summary.fail('backend pipe broken')
                close_stdin()
        def finish_if_done():
            nonlocal protocol_done, done_at
            if session is not None and getattr(session, 'done', False) and not protocol_done:
                protocol_done = True
                done_at = time.monotonic()
                close_stdin()
        readers = [threading.Thread(target=read, args=(name, stream), daemon=True)
                   for name, stream in [('stdout', worker.stdout), ('stderr', worker.stderr)]]
        for thread in readers:
            thread.start()
        if session is None:
            threading.Thread(target=write_prompt, daemon=True).start()
        else:
            try:
                write_outbound(session.initial(summary, prompt + OWNER_INSTRUCTION))
            except (TypeError, ValueError, AttributeError, KeyError) as exc:
                summary.fail('Protocol session start failed: ' + str(exc))
                close_stdin()
            finish_if_done()
        closed, last_event, next_status, exited_at, stopped_at = set(), None, interval, None, None
        while True:
            elapsed = time.monotonic() - started
            if runtime is not None and elapsed >= runtime and worker.poll() is None:
                tree.stop()
                result_status, stop_reason = 'timed_out', 'max_runtime_exceeded'
            if interval and elapsed >= next_status and worker.poll() is None:
                status(sys.stderr, status_line(elapsed, last_event, summary, target))
                next_status = elapsed + interval
            try:
                channel, line = events.get(timeout=0.1)
                if line is None:
                    closed.add(channel)
                else:
                    last_event = elapsed
                    if log is not None:
                        try:
                            log.write(json.dumps({'channel': channel, 'line': redact(line)}, ensure_ascii=False) + '\n')
                            log.flush()
                        except (OSError, ValueError) as exc:
                            warnings.append('Full log write failed: ' + str(exc))
                            try:
                                log.close()
                            except OSError:
                                pass
                            log = None
                    try:
                        if session is None:
                            summary.consume(adapter, channel, line)
                        else:
                            write_outbound(summary.consume(adapter, channel, line, session=session))
                            finish_if_done()
                    except (TypeError, ValueError, AttributeError, KeyError):
                        warning = 'A native event could not be parsed; inspect the full log'
                        if warning not in warnings:
                            warnings.append(warning)
            except queue.Empty:
                pass
            if session is not None:
                try:
                    write_outbound(session.poll(summary))
                except (TypeError, ValueError, AttributeError, KeyError) as exc:
                    summary.fail('Protocol session failed: ' + str(exc))
                    close_stdin()
                finish_if_done()
            now = time.monotonic()
            if protocol_done and done_at is not None and worker.poll() is None \
                    and now - done_at > DONE_GRACE_SECONDS:
                try:
                    tree.stop()
                except (OSError, subprocess.SubprocessError):
                    if worker.poll() is None:
                        worker.kill()
            if worker.poll() is not None:
                now = time.monotonic()
                if exited_at is None:
                    exited_at = now
                if stopped_at is None and now - exited_at > 2:
                    # End inherited producers even when buffered output is still pending.
                    tree.stop()
                    stopped_at = time.monotonic()
                pipes_expired = stopped_at is not None and now - stopped_at > 2
                if events.empty() and (len(closed) == 2 or pipes_expired):
                    break
        tree.close()  # Also closes descendants that inherited pipes after a native exit.
        for thread in readers:
            thread.join(timeout=2)
        if track:
            after = git_paths(repo)
    except KeyboardInterrupt:
        result_status, stop_reason = 'cancelled', 'cancelled'
        if tree is not None:
            tree.stop()
        if track:
            after = git_paths(repo)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        if tree is not None:
            tree.close()
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)
        if log is not None:
            try:
                log.close()
            except OSError as exc:
                warnings.append('Full log close failed: ' + str(exc))
        if lock is not None:
            lock.__exit__(None, None, None)
    worker_question = summary.answer.lstrip().startswith('NEEDS_USER_INPUT')
    if result_status is None and resumed:
        # A resume must continue the requested session: an absent or foreign
        # session id is a failure, never a silent new dialogue.
        if not summary.session_id:
            result_status, stop_reason = 'failed', 'resume_session_unconfirmed'
        elif summary.session_id != resume_session:
            result_status, stop_reason = 'failed', 'resume_session_mismatch'
    if result_status is None and worker_question:
        # A marker with a terminal answer is the worker's own question. It keeps
        # today's precedence over classified stderr failures, including quota.
        result_status, stop_reason = 'needs_input', 'worker_question'
        summary.error = 'Worker requested an owner decision'
    if result_status is None:
        if summary.reason:
            result_status, stop_reason = 'failed', summary.reason
        elif worker.returncode and not protocol_done:
            result_status, stop_reason = 'failed', 'native_exit_nonzero'
        elif summary.limited and summary.answer.strip():
            result_status, stop_reason = 'failed', 'output_limited'
        elif summary.denied_actions and (not summary.finished or not summary.answer.strip()):
            # A denial that stopped the work; an overcome denial stays completed.
            result_status, stop_reason = 'failed', 'needs_user_input'
            summary.error = summary.error or 'Native tool permission denied'
        elif not summary.finished or not summary.answer.strip():
            result_status, stop_reason = 'failed', 'no_terminal_answer'
        else:
            result_status, stop_reason = 'completed', 'completed'
    full_answer = redact(summary.answer)
    truncated = len(full_answer) > 2000
    answer_path = None
    if truncated:
        answer_path = str(Path(log_path).with_suffix('')) + '.answer.txt'
        try:
            Path(answer_path).write_text(full_answer, encoding='utf-8')
        except OSError as exc:
            warnings.append('Full answer write failed: ' + str(exc))
            answer_path = None
    return {'status': result_status, 'stop_reason': stop_reason, 'answer': full_answer[:2000],
            'answer_truncated': truncated, 'answer_path': answer_path,
            'answer_length': len(full_answer), 'reason': summary.error,
            'reported': summary.reported, 'changed_paths': sorted(after),
            'preexisting_changed_paths': sorted(before), 'log_path': log_path,
            'session_id': summary.session_id, 'resumed': resumed,
            'usage': summary.usage or None,
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'warnings': warnings + summary.warnings,
            'quota_reset_at': summary.reset, 'denied_actions': summary.denied_actions,
            'native_exit_code': worker.returncode if worker is not None else None}
