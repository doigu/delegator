"""ZCode app-server adapter: JSON-RPC protocol in a per-run session.

The official CLI has no headless turn-event stream. This module keeps the same
selection/result contract as the other adapters while speaking the request /
response protocol through an optional runner session. No adapter retries or
fallback; the wrapper retries classified capacity failures; resume is explicit
only.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from native import redact

PLAN_ACCESS_TYPE = 'zhipu-account'
# Native session modes track --shell: guarded asks only on risk, yolo runs
# without confirmations; `none` keeps the mode split (plan reads only, edit
# applies workspace edits without shell commands).
SESSION_MODE = {'auto': 'guarded', 'full': 'yolo'}
INTERACTION_DENY = {'action': 'decline', 'reason': 'headless worker auto-deny'}
RUNTIME_PREFERENCES_REPLY = {
    'nativeSearchEnhancementsEnabled': False,
    'memoryEnabled': False,
    'askUserQuestionAutoResolutionEnabled': False,
}
ASSISTANT_TEXT_KEYS = frozenset({'text', 'content', 'delta', 'response'})
QUOTA_MARKER = re.compile(r'\b1301\b|"code"\s*:\s*1301')
MAX_STDERR = 64 * 1024
REQUEST_TIMEOUTS = {
    'provider/updateAccountConfig': 15.0,
    'session/create': 120.0,
    'session/resume': 60.0,
    'session/setMode': 30.0,
    'session/setModel': 30.0,
    'session/read': 60.0,
    'session/subscribe': 30.0,
    'session/send': 30.0,
    'session/messages': 60.0,
}


def session_mode(mode, shell):
    if shell in SESSION_MODE:
        return SESSION_MODE[shell]
    return 'plan' if mode == 'review' else 'edit'


def zcode_home():
    override = os.environ.get('ZCODE_HOME')
    return Path(override) if override else Path.home() / '.zcode'


def resolve():
    """Return the backend argv prefix, including node for a desktop .cjs bundle."""
    candidates = []
    explicit = os.environ.get('ZCODE_BIN')
    if explicit:
        candidates.append(explicit)
    on_path = shutil.which('zcode')
    if on_path:
        candidates.append(on_path)
    if os.name == 'nt':
        local = os.environ.get('LOCALAPPDATA', '')
        candidates += [
            os.path.join(local, 'Programs', 'ZCode', 'resources', 'glm', 'zcode.cjs'),
            r'C:\Program Files\ZCode\resources\glm\zcode.cjs',
        ]
    elif sys.platform == 'darwin':
        candidates.append('/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs')
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        if candidate.lower().endswith('.cjs'):
            node = shutil.which('node')
            if node:
                return [node, candidate]
            continue
        return [candidate]
    raise ValueError('zcode backend not found (checked ZCODE_BIN, PATH, desktop bundle)')


def builtin_table_paths():
    """Candidate builtin-table locations for offline readers; env override only."""
    explicit = os.environ.get('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE')
    if explicit:
        return [Path(explicit)]
    paths = []
    if os.name == 'nt':
        local = os.environ.get('LOCALAPPDATA', '')
        bundles = [os.path.join(local, 'Programs', 'ZCode', 'resources', 'glm', 'zcode.cjs')
                   if local else None,
                   r'C:\Program Files\ZCode\resources\glm\zcode.cjs']
    elif sys.platform == 'darwin':
        bundles = ['/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs']
    else:
        bundles = []
    for bundle in bundles:
        if bundle:
            # Same derivation as _builtin_table: the table sits next to the
            # bundle's own parent, e.g. resources\glm\zcode.cjs ->
            # resources\config\provider\zcode-builtin.json.
            paths.append(Path(bundle).resolve().parent.parent
                         / 'config' / 'provider' / 'zcode-builtin.json')
    return paths


def _parts(executable):
    if isinstance(executable, (list, tuple)):
        return [str(part) for part in executable]
    return [str(executable)]


def _builtin_table(executable):
    explicit = os.environ.get('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE')
    if explicit:
        path = Path(explicit)
    else:
        path = None
        for part in _parts(executable):
            if part.lower().endswith('.cjs'):
                path = Path(part).resolve().parent.parent / 'config' / 'provider' / 'zcode-builtin.json'
                break
        if path is None:
            raise ValueError('ZCode builtin provider table not found; set ZCODE_BUILTIN_PROVIDER_CONFIG_FILE')
    if not path.is_file():
        raise ValueError(f'ZCode builtin provider table not found: {path}')
    try:
        table = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'cannot read ZCode builtin provider table: {exc}') from None
    if not isinstance(table, dict):
        raise ValueError('ZCode builtin provider table is not an object')
    return path, table


def _rules(table):
    rules = ((table.get('config') or {}).get('providerConfigRules') or {}).get('providerRules') or []
    return rules if isinstance(rules, list) else []


def _plan_models(table, provider):
    for rule in _rules(table):
        if rule.get('providerId') != provider:
            continue
        models = (rule.get('config') or {}).get('builtinModelIds')
        return [str(model) for model in models] if isinstance(models, list) else []
    return []


def _reasoning_levels(table, model):
    rules = ((table.get('config') or {}).get('modelConfigRules') or {}).get('modelRules') or []
    levels = set()
    for rule in rules if isinstance(rules, list) else []:
        pattern = rule.get('modelMatch')
        if not isinstance(pattern, str):
            continue
        try:
            if re.fullmatch(pattern, model, re.IGNORECASE) is None:
                continue
        except re.error:
            continue
        values = ((((rule.get('config') or {}).get('optionSpecs') or {})
                   .get('reasoningLevel') or {}).get('values'))
        if isinstance(values, list):
            levels.update(str(value) for value in values)
    return levels


def _validate_selection(table, provider, model, effort, override=False):
    models = _plan_models(table, provider)
    if not models:
        raise ValueError(f'ZCode plan provider not found in builtin table: {provider}')
    if model not in models and not override:
        raise ValueError(f'model {model} is not offered by plan {provider}; offered: {", ".join(models)}')
    levels = _reasoning_levels(table, model)
    if levels and effort not in levels:
        raise ValueError(f'reasoning level {effort} is not allowed for model {model}; '
                         f'allowed: {", ".join(sorted(levels))}')


def _home_is_writable(home):
    probe = home / f'.write-probe-{os.getpid()}'
    try:
        probe.write_text('', encoding='utf-8')
        probe.unlink()
        return True
    except OSError:
        return False


def _plan_access_mode(table, provider):
    for rule in _rules(table):
        if rule.get('providerId') == provider:
            mode = ((rule.get('config') or {}).get('access') or {}).get('mode')
            return mode if isinstance(mode, str) else None
    return None


def _decrypt_credential(home, stored):
    credentials = home / 'v2' / 'credentials.json'
    if not stored.startswith('enc:v1:'):
        return stored
    node = shutil.which('node')
    if not node:
        raise ValueError('node is required to decrypt the ZCode credential')
    helper = Path(__file__).resolve().parent / 'decrypt_credential.js'
    try:
        proc = subprocess.run([node, str(helper), str(credentials)], input=stored,
                              capture_output=True, text=True, encoding='utf-8',
                              timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f'cannot decrypt ZCode credential: {exc}') from None
    key = proc.stdout.strip()
    if proc.returncode or not key:
        raise ValueError('cannot decrypt ZCode credential (cipher mismatch); '
                         'invoke outside a different OS account or set ZCODE_CREDENTIAL_SECRET')
    return key


def _read_credentials(home):
    credentials = home / 'v2' / 'credentials.json'
    try:
        return json.loads(credentials.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'cannot read ZCode credentials: {exc}') from None


def load_login_jwt(home):
    """Decrypted desktop login JWT (zcodejwttoken), or None when absent."""
    stored = _read_credentials(home).get('zcodejwttoken')
    if not isinstance(stored, str) or not stored.strip():
        return None
    return _decrypt_credential(home, stored.strip())


def _load_plan_key(home, provider, plan_mode=None):
    data = _read_credentials(home)
    stored = None
    if plan_mode == 'start-plan':
        # The desktop authorizes start-plan turns with the login JWT from the
        # same store; OAuth-activated plans have no per-plan api-key entry.
        oauth = data.get('zcodejwttoken')
        if isinstance(oauth, str) and oauth.strip():
            stored = oauth.strip()
    if stored is None:
        for name, value in data.items():
            if (isinstance(name, str) and name.startswith('account-provider:')
                    and provider in name and name.endswith(':api-key')
                    and isinstance(value, str) and value.strip()):
                stored = value.strip()
                break
    if stored is None:
        raise ValueError(f'ZCode plan key unavailable: provider={provider}')
    return _decrypt_credential(home, stored)


def _provider_env(table_path):
    env = {}
    if not os.environ.get('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE'):
        env['ZCODE_BUILTIN_PROVIDER_CONFIG_FILE'] = str(table_path)
    if not os.environ.get('ZCODE_PERSONAL_PROVIDER_CONFIG_FILE'):
        personal = zcode_home() / 'v2' / 'provider_config.json'
        if personal.is_file():
            env['ZCODE_PERSONAL_PROVIDER_CONFIG_FILE'] = str(personal)
    return env


def _account_payload(table_path, provider, extra_model=None):
    try:
        table = json.loads(Path(table_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    based_on = 'zcode-builtin:%s:%s' % (
        table.get('revision', 0),
        hashlib.sha256(str(Path(table_path).resolve()).encode('utf-8')).hexdigest())
    rules = [rule for rule in _rules(table)
             if ((rule.get('config') or {}).get('access') or {}).get('type') == PLAN_ACCESS_TYPE]
    if not rules:
        return None
    providers, states = {}, {}
    for rule in rules:
        pid = rule['providerId']
        ok = pid == provider
        models = (rule.get('config') or {}).get('builtinModelIds')
        if ok and extra_model is not None:
            # A registry override for a model the local table does not list
            # yet: the headless provider registry is built from this push, so
            # the selected model must be offered by it (verified live: without
            # it setModel refuses with "Provider Registry 中不存在 Model").
            models = [str(item) for item in models] if isinstance(models, list) else []
            if extra_model not in models:
                models.append(extra_model)
        providers[pid] = {
            'builtinModelIds': models,
            'access': {'type': PLAN_ACCESS_TYPE, 'entitled': ok},
        }
        states[pid] = {'availability': 'available' if ok else 'unavailable',
                       'entitled': ok, 'current': ok}
    return {
        'revision': 'account:delegator-zcode:%d' % int(time.time() * 1000),
        'basedOnZCodeBuiltinRevision': based_on,
        'providers': providers,
        'states': states,
    }


def _extract_text(terminal_payload, messages, deltas):
    if isinstance(terminal_payload, dict):
        response = terminal_payload.get('response')
        if isinstance(response, str) and response.strip():
            return response
    texts = []

    def walk(node, in_assistant=False):
        if isinstance(node, dict):
            role = node.get('info', {}).get('role') if isinstance(node.get('info'), dict) else None
            here = in_assistant or role == 'assistant'
            for key, value in node.items():
                if (here and key.lower() in ASSISTANT_TEXT_KEYS
                        and isinstance(value, str) and value.strip()):
                    texts.append(value)
                else:
                    walk(value, here)
        elif isinstance(node, list):
            for item in node:
                walk(item, in_assistant)

    walk(messages)
    joined = '\n'.join(text for text in texts if text.strip()).strip()
    if joined:
        return joined
    return ''.join(deltas).strip() or None


class ProtocolSession:
    """Drive one ZCode app-server turn through values returned to runner."""

    def __init__(self, provider, model, effort, repo, table_path, plan_key, table,
                 resume_session=None, mode='yolo', override=False):
        self.provider = provider
        self.model = model
        self.effort = effort
        self.repo = str(repo)
        self.table_path = str(table_path)
        self.plan_key = plan_key
        self.table = table
        self.resume_session = resume_session
        self.mode = mode
        self.override = override
        self.done = False
        self.prompt = None
        self.session_id = None
        self.phase = 'start'
        self._next_id = 0
        self._pending = {}
        self._deadlines = {}
        self._stderr = []
        self._deltas = []
        self._terminal = None
        self._interactions = 0
        self._observed = {}
        self._finished_tool_ids = set()

    def _request(self, method, params):
        self._next_id += 1
        self._pending[self._next_id] = method
        self._deadlines[self._next_id] = time.monotonic() + REQUEST_TIMEOUTS[method]
        return {'id': self._next_id, 'method': method, 'params': params}

    def initial(self, summary, prompt):
        self.prompt = prompt
        payload = _account_payload(self.table_path, self.provider,
                                   self.model if self.override else None)
        if payload is None:
            self._fail(summary, 'ZCode account push payload unavailable')
            return []
        return [self._request('provider/updateAccountConfig', payload)]

    def event(self, summary, event):
        if not isinstance(event, dict):
            return []
        request_id = event.get('id')
        method = event.get('method')
        if request_id is not None and method is None:
            return self._response(summary, request_id, event)
        if request_id is not None and isinstance(method, str):
            if method == 'interaction/requestPermission':
                # Declined like any interaction, but the turn goes on; the runner
                # classifies the denial by whether a terminal answer followed.
                params = event.get('params') if isinstance(event.get('params'), dict) else {}
                detail = {key: params.get(key) for key in ('input', 'reason', 'riskLevel')}
                summary.deny(params.get('toolName'), json.dumps(detail, ensure_ascii=False))
            reply = self._host_reply(request_id, method)
            # Official MCP plugin auth is declined (-32601); the backend then
            # proceeds without it — verified live for create and resume.
            if method not in ('session/requestRuntimePreferences',
                              'interaction/requestPermission',
                              'interaction/requestUserInput',
                              'interaction/requestProviderRuntimeHeaders',
                              'interaction/requestOfficialMcpAuthHeaders'):
                self._fail(summary, f'ZCode unsupported host request: {method}')
            return [reply]
        if method == 'session/event':
            params = event.get('params')
            return self._session_event(summary, params if isinstance(params, dict) else {})
        return []

    def poll(self, summary):
        now = time.monotonic()
        for request_id, deadline in list(self._deadlines.items()):
            if now <= deadline:
                continue
            method = self._pending.pop(request_id, 'request')
            self._deadlines.pop(request_id, None)
            self._fail(summary, f'ZCode backend timeout: {method}')
            return []
        return []

    def stderr(self, summary, line):
        self._stderr.append(line)
        joined = ''.join(self._stderr)
        if len(joined) > MAX_STDERR:
            self._stderr = [joined[-MAX_STDERR:]]

    def _fail(self, summary, text):
        text = str(text)
        if QUOTA_MARKER.search(text):
            summary.fail('quota exceeded (zhipu 1301): ' + text)
        else:
            summary.fail(text)
        self.done = True

    def _error_text(self, event):
        error = event.get('error')
        if isinstance(error, dict):
            return str(error.get('message') or json.dumps(error, ensure_ascii=False))
        return str(error or 'ZCode backend error')

    def _response(self, summary, request_id, event):
        method = self._pending.pop(request_id, None)
        self._deadlines.pop(request_id, None)
        if method is None:
            return []
        if event.get('error'):
            if method == 'session/resume':
                # Contract for a foreign or dead id: failed as
                # resume_session_unconfirmed; the native text stays in the
                # reason and never blocks the provider.
                summary.error = redact(
                    f'session/resume refused: {self._error_text(event)}')[:500]
                self.done = True
                return []
            self._fail(summary, f'{method} refused: {self._error_text(event)}')
            return []
        result = event.get('result') or {}
        if not isinstance(result, dict):
            result = {}
        if method == 'provider/updateAccountConfig':
            if self.resume_session is not None:
                return [self._request('session/resume', {'sessionId': self.resume_session})]
            return [self._request('session/create', {
                'workspace': {'workspacePath': self.repo, 'workspaceKey': self.repo},
                'mode': self.mode})]
        if method == 'session/resume':
            # The result echoes the resumed session; anything but the exact
            # requested id means the wrong dialogue was attached.
            session = (result.get('session') or {}) if isinstance(result.get('session'), dict) else {}
            resumed = session.get('sessionId')
            if resumed != self.resume_session:
                if isinstance(resumed, str) and resumed:
                    summary.session_id = resumed
                self.done = True
                return []
            self.session_id = self.resume_session
            summary.session_id = self.session_id
            # resume takes no mode; setMode re-pins the session to the
            # requested --shell level (same value on a plain resume, a higher
            # one on an escalation).
            return [self._request('session/setMode',
                                  {'sessionId': self.session_id, 'mode': self.mode})]
        if method == 'session/setMode':
            return [self._request('session/setModel', self._set_model_params())]
        if method == 'session/create':
            session = (result.get('session') or {}) if isinstance(result.get('session'), dict) else {}
            self.session_id = session.get('sessionId')
            if not self.session_id:
                self._fail(summary, 'ZCode session create failed')
                return []
            summary.session_id = self.session_id
            return [self._request('session/setModel', self._set_model_params())]
        if method == 'session/setModel':
            self.phase = 'pre_read'
            return [self._request('session/read', {'sessionId': self.session_id})]
        if method == 'session/read':
            if not self._verify_model(summary, result, self.phase):
                return []
            if self.phase == 'pre_read':
                return [self._request('session/subscribe', {
                    'sessionId': self.session_id,
                    'deliveryKind': 'desktop-continuous',
                    'includeSnapshot': True,
                    'afterSeq': 0})]
            self.done = True
            return []
        if method == 'session/subscribe':
            return [self._request('session/send', {
                'sessionId': self.session_id, 'content': self.prompt})]
        if method == 'session/send':
            return []
        if method == 'session/messages':
            messages = result.get('messages')
            text = _extract_text(self._terminal or {}, messages, self._deltas)
            if text:
                summary.answer = text
            self.phase = 'post_read'
            return [self._request('session/read', {'sessionId': self.session_id})]
        return []

    def _set_model_params(self):
        return {'sessionId': self.session_id,
                'model': {'providerId': self.provider, 'modelId': self.model,
                          'options': {'reasoningLevel': self.effort}},
                'persistAsWorkspaceLastUsed': False}

    def _verify_model(self, summary, result, phase):
        settings = result.get('settings')
        if not isinstance(settings, dict):
            settings = {}
        model_settings = settings.get('model')
        if not isinstance(model_settings, dict):
            model_settings = {}
        current = model_settings.get('current')
        if not isinstance(current, dict):
            current = {}
        model = current.get('modelId')
        provider = current.get('providerId')
        if isinstance(model, str):
            self._observed['model'] = model
            summary.reported['model'] = model
        if isinstance(provider, str):
            self._observed['provider'] = provider
            summary.reported['provider'] = provider
        level = current.get('reasoningLevel')
        if not isinstance(level, str):
            level = (current.get('options') or {}).get('reasoningLevel')
        if isinstance(level, str):
            summary.reported['effort'] = level
        if model == self.model and provider == self.provider:
            return True
        if model:
            if phase != 'pre_read':
                self._fail(summary, 'ZCode reported a different model after the turn')
            else:
                self.done = True
            return False
        self._fail(summary, 'ZCode native model report unavailable')
        return False

    def _session_event(self, summary, params):
        kind = params.get('type')
        payload = params.get('payload')
        if not isinstance(payload, dict):
            payload = {}
        if kind == 'tool.updated':
            name = payload.get('toolName')
            if isinstance(name, str) and name:
                summary.last_tool = name
                summary.tools_observed = True
            if payload.get('kind') == 'result' and isinstance(payload.get('toolCallId'), str):
                self._finished_tool_ids.add(payload['toolCallId'])
                summary.tools_observed = True
            summary.completed_tools = len(self._finished_tool_ids)
            return []
        if kind == 'model.streaming':
            delta = payload.get('delta')
            if isinstance(delta, str):
                self._deltas.append(delta)
            return []
        if kind == 'turn.completed':
            self._terminal = payload if isinstance(payload, dict) else {}
            usage = self._terminal.get('usage')
            if isinstance(usage, dict):
                summary.add_usage(usage)
            summary.finished = True
            if self._interactions:
                self._fail(summary, 'ZCode user input requested; no user input available')
                return []
            if self.resume_session is not None:
                # A resumed transcript still holds prior answers, so the
                # messages walk is unusable here; the terminal payload and
                # this run's deltas cannot replay old text.
                text = _extract_text(self._terminal, None, self._deltas)
                if text:
                    summary.answer = text
                self.phase = 'post_read'
                return [self._request('session/read', {'sessionId': self.session_id})]
            return [self._request('session/messages', {'sessionId': self.session_id})]
        if kind == 'turn.failed':
            error = payload.get('error') if isinstance(payload, dict) else None
            detail = json.dumps(error, ensure_ascii=False) if error is not None else 'turn failed'
            detail += '\n' + ''.join(self._stderr)
            self._fail(summary, detail)
            return []
        return []

    def _host_reply(self, request_id, method):
        if method == 'session/requestRuntimePreferences':
            return {'id': request_id, 'result': dict(RUNTIME_PREFERENCES_REPLY)}
        if method in ('interaction/requestPermission', 'interaction/requestUserInput'):
            if method == 'interaction/requestUserInput':
                self._interactions += 1
            return {'id': request_id, 'result': dict(INTERACTION_DENY)}
        if method == 'interaction/requestProviderRuntimeHeaders':
            if self.plan_key:
                return {'id': request_id, 'result': {
                    'headersApplied': True, 'requestAuth': {'apiKey': self.plan_key}}}
            return {'id': request_id, 'result': {
                'headersApplied': False, 'errorMessage': 'headless worker: no plan key'}}
        return {'id': request_id, 'error': {'code': -32601, 'message': 'headless worker: unsupported'}}


def command(executable, repo, provider, model, effort, mode, shell, resume_session=None,
            git_check=True, override=False):
    if not provider or not model:
        raise ValueError('ZCode requires a plan provider and model')
    if effort is None:
        raise ValueError('ZCode requires an explicit effort (reasoning level)')
    table_path, table = _builtin_table(executable)
    _validate_selection(table, provider, model, effort, override)
    home = zcode_home()
    if not _home_is_writable(home):
        raise ValueError(f'ZCode home is not writable: {home}; invoke outside a restricted sandbox')
    plan_key = _load_plan_key(home, provider, _plan_access_mode(table, provider))
    session = ProtocolSession(provider, model, effort, repo, table_path, plan_key, table,
                              resume_session, session_mode(mode, shell), override)
    return ([*_parts(executable), 'app-server', '--stdio'], _provider_env(table_path), session)
