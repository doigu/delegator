"""Quota collectors for harness accounts. Read-only HTTP/JSON-RPC, no LLM calls;
the only exceptions are two credential-renewal probes whose only purpose is
making the harness CLI renew its own stored OAuth token: a headless
`claude -p --model haiku` run for claude and an `agy models` run for agy.

Each collector returns one snapshot source entry and may raise; collect() turns
any exception, HTTP failure, or exceeded wall-clock budget into an `error`
source entry without touching the other sources. Secrets (keys, tokens,
headers) never enter the snapshot, errors, or rendered output.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from native import redact

try:
    import zcode as zcode_module
except ImportError:
    zcode_module = None

SOURCES = ('codex', 'zcode', 'claude', 'agy', 'openrouter', 'commandcode', 'opencode-go')
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')
IS_WINDOWS = sys.platform == 'win32'

# Antigravity exposes quota only through internal daily-cloudcode-pa endpoints. The
# OAuth blob lives in the Windows Credential Manager (written by the agy login).
# An expired access token is not renewed here: the `agy models` probe makes the
# agy CLI refresh its own stored credential with its own OAuth client material,
# and quota re-reads the blob — no OAuth client material lives in this repository.
AGY_CRED_TARGET = 'gemini:antigravity'
AGY_API_HOST = 'https://daily-cloudcode-pa.googleapis.com'
# x-goog-api-client is required: without it the endpoints answer a misleading
# 403 SUBSCRIPTION_REQUIRED even for a valid consumer account.
AGY_API_HEADERS = {'x-goog-api-client': 'antigravity-cli/1.0', 'User-Agent': 'antigravity-cli'}


class SourceError(Exception):
    """A classified collector failure; the message is safe for output."""

    def __init__(self, message, kind='source_error'):
        super().__init__(message)
        self.kind = kind


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _iso_from_epoch(seconds):
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec='seconds')


def _http_json(url, headers, timeout, data=None):
    # data (bytes) turns the request into a POST; GET endpoints omit it.
    request = urllib.request.Request(url, data=data,
                                     headers=dict(headers, Accept='application/json'))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        kind = 'auth_expired' if exc.code in (401, 403) else 'http_error'
        raise SourceError(f'HTTP {exc.code} from {url}: {_brief_detail(exc)}', kind) from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SourceError(f'{url} unreachable: {redact(str(exc))}', 'network') from None
    try:
        return json.loads(body)
    except ValueError:
        raise SourceError(f'{url} returned non-JSON body', 'parse_error') from None


def _brief_detail(exc):
    """Classified short reason from an HTTPError body; never the raw body."""
    try:
        payload = json.loads(exc.read(4096).decode('utf-8', 'replace'))
    except (ValueError, OSError):
        return 'reason unavailable'
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict) and (error.get('code') or error.get('message')):
            return redact(str(error.get('code') or error.get('message')))
        if isinstance(payload.get('msg'), str):
            return redact(payload['msg'])
    return 'reason unavailable'


def _window(label, used_percent=None, used=None, limit=None, unit=None,
            window_minutes=None, resets_at=None):
    return {'label': label, 'used_percent': used_percent, 'used': used, 'limit': limit,
            'unit': unit, 'window_minutes': window_minutes, 'resets_at': resets_at}


def _source_entry(name, windows, extras):
    return {'source': name, 'status': 'ok', 'fetched_at': now_iso(), 'error': None,
            'windows': windows, 'extras': extras}


def _error_entry(name, exc):
    if isinstance(exc, SourceError):
        message, kind = str(exc), exc.kind
    else:
        message, kind = f'{type(exc).__name__}: {redact(str(exc))}', 'unexpected_error'
    return {'source': name, 'status': 'error', 'fetched_at': now_iso(),
            'error': f'{kind}: {message}', 'windows': [], 'extras': {}}


# ---------------------------------------------------------------- keys


def _zcode_api_key():
    """Coding-plan api-key credential, or None when the store has no entry.

    More than one matching entry is a configuration error: the monitor
    endpoint cannot be attributed to a specific plan.
    """
    if zcode_module is None:
        raise SourceError('zcode module unavailable', 'config')
    home = zcode_module.zcode_home()
    credentials = home / 'v2' / 'credentials.json'
    try:
        data = json.loads(credentials.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SourceError(f'cannot read ZCode credentials: {redact(str(exc))}', 'config') from None
    names = [name for name in data
             if isinstance(name, str) and name.startswith('account-provider:')
             and 'coding-plan' in name and name.endswith(':api-key')]
    if len(names) > 1:
        raise SourceError(f'expected exactly one ZCode coding-plan key, found {len(names)}', 'config')
    if not names:
        return None
    try:
        return zcode_module._load_plan_key(home, 'coding-plan')
    except (ValueError, OSError) as exc:
        raise SourceError(redact(str(exc)), 'config') from None


def _zcode_login_jwt():
    """Decrypted desktop login JWT (zcodejwttoken), or None when absent."""
    if zcode_module is None:
        raise SourceError('zcode module unavailable', 'config')
    try:
        return zcode_module.load_login_jwt(zcode_module.zcode_home())
    except (ValueError, OSError) as exc:
        raise SourceError(redact(str(exc)), 'config') from None


def _opencode_auth():
    candidates = []
    data_home = os.environ.get('XDG_DATA_HOME')
    if data_home:
        candidates.append(Path(data_home).expanduser() / 'opencode' / 'auth.json')
    candidates.append(Path.home() / '.local' / 'share' / 'opencode' / 'auth.json')
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            raise SourceError(f'cannot read OpenCode auth: {redact(str(exc))}', 'config') from None
        return {name: (entry or {}).get('key') for name, entry in raw.items()
                if isinstance(entry, dict)}
    raise SourceError('OpenCode auth.json not found', 'config')


def _require_key(provider):
    key = _opencode_auth().get(provider)
    if not key:
        raise SourceError(f'{provider} key not configured in OpenCode auth', 'config')
    return key


# ---------------------------------------------------------------- codex


def _codex_rpc(timeout):
    binary = shutil.which('codex')
    if not binary:
        raise SourceError('codex CLI not found on PATH', 'config')
    proc = subprocess.Popen([binary, 'app-server', '--stdio'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding='utf-8', errors='replace')
    lines = queue.Queue()

    def pump():
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    try:
        threading.Thread(target=pump, daemon=True).start()
        deadline = time.monotonic() + timeout

        def send(request_id, method, params):
            proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': request_id,
                                         'method': method, 'params': params}) + '\n')
            proc.stdin.flush()

        send(1, 'initialize', {'clientInfo': {'name': 'delegator-quota', 'version': '1.0'}})
        send(2, 'account/rateLimits/read', {'excludeResetCreditDetails': True})
        send(3, 'account/usage/read', {})
        wanted = {2, 3}
        responses = {}
        while wanted and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get('id') in wanted:
                if event.get('error'):
                    raise SourceError(f'codex app-server refused id {event["id"]}: '
                                      f'{redact(str(event["error"].get("message")))[:200]}', 'parse_error')
                responses[event['id']] = event.get('result')
                wanted.discard(event['id'])
        if wanted:
            raise SourceError(f'codex app-server did not answer {sorted(wanted)} within budget', 'timeout')
        return responses[2], responses[3]
    finally:
        proc.kill()


def parse_codex(rate_limits, usage):
    snapshot = (rate_limits or {}).get('rateLimits') if isinstance(rate_limits, dict) else None
    if not isinstance(snapshot, dict):
        raise SourceError('codex response has no rateLimits block', 'parse_error')
    windows = []
    for label in ('primary', 'secondary'):
        block = snapshot.get(label)
        if not isinstance(block, dict):
            continue
        windows.append(_window(label, used_percent=block.get('usedPercent'),
                               window_minutes=block.get('windowDurationMins'),
                               resets_at=_iso_from_epoch(block.get('resetsAt'))))
    extras = {}
    if isinstance(snapshot.get('planType'), str):
        extras['plan'] = snapshot['planType']
    credits = snapshot.get('credits')
    if isinstance(credits, dict):
        extras['credits_balance'] = credits.get('balance')
    summary = (usage or {}).get('summary') if isinstance(usage, dict) else None
    if isinstance(summary, dict):
        for key in ('lifetimeTokens', 'currentStreakDays', 'peakDailyTokens'):
            if isinstance(summary.get(key), (int, float)):
                extras[key] = summary[key]
    return _source_entry('codex', windows, extras)


def collect_codex(timeout):
    rate_limits, usage = _codex_rpc(timeout)
    return parse_codex(rate_limits, usage)


# ---------------------------------------------------------------- zcode


def parse_zcode(data):
    payload = data.get('data') if isinstance(data, dict) else None
    limits = payload.get('limits') if isinstance(payload, dict) else None
    if not isinstance(limits, list):
        raise SourceError('zcode response has no limits list', 'parse_error')

    def label(block):
        unit, number = block.get('unit'), block.get('number')
        if unit == 3:
            return f'{number}h'
        if unit == 6:
            return f'{number}w'
        return f'unit-{unit}x{number}'

    windows = []
    for block in limits:
        if not isinstance(block, dict) or block.get('type') != 'CREDIT_LIMIT':
            continue
        resets_at = _iso_from_epoch(block.get('nextResetTime') / 1000
                                    if isinstance(block.get('nextResetTime'), (int, float))
                                    else None)
        windows.append(_window(label(block), used_percent=block.get('percentage'),
                               used=block.get('currentValue'), limit=block.get('usage'),
                               unit='credits', resets_at=resets_at))
    extras = {'level': payload.get('level')} if isinstance(payload, dict) and payload.get('level') else {}
    return _source_entry('zcode', windows, extras)


def parse_zcode_start(data):
    """Start-plan token buckets (billing/balance): one window per balance."""
    payload = data.get('data') if isinstance(data, dict) else None
    balances = payload.get('balances') if isinstance(payload, dict) else None
    if not isinstance(balances, list):
        raise SourceError('zcode start-plan response has no balances list', 'parse_error')
    windows = []
    for bucket in balances:
        if not isinstance(bucket, dict):
            continue
        total, used = bucket.get('total_units'), bucket.get('used_units')
        if not isinstance(total, (int, float)) or not isinstance(used, (int, float)) or total <= 0:
            continue
        label = bucket.get('show_name')
        windows.append(_window(label if isinstance(label, str) and label.strip() else 'start-plan',
                               used_percent=round(used / total * 100, 1),
                               used=used, limit=total, unit='tokens'))
    expires = [bucket.get('expires_at') for bucket in balances
               if isinstance(bucket, dict) and isinstance(bucket.get('expires_at'), (int, float))
               and bucket.get('expires_at') > 0]
    extras = {}
    if expires:
        # Packages are one_time grants; expires_at burns down the remaining
        # units, it does not reset them, so it stays out of the windows.
        extras['start_plan_expires_at'] = _iso_from_epoch(min(expires))
    return _source_entry('zcode', windows, extras)


ZCODE_PLAN_APP_VERSION = '3.16.3'


def collect_zcode(timeout):
    api_key = _zcode_api_key()
    login_jwt = _zcode_login_jwt()
    if api_key is None and login_jwt is None:
        raise SourceError('no ZCode coding-plan api-key or login JWT (zcodejwttoken) '
                          'in ~/.zcode/v2/credentials.json', 'config')
    windows, extras = [], {}
    if api_key is not None:
        data = _http_json('https://api.z.ai/api/monitor/usage/quota/limit',
                          {'Authorization': f'Bearer {api_key}'}, timeout)
        entry = parse_zcode(data)
        windows += entry['windows']
        extras.update(entry['extras'])
    if login_jwt is not None:
        data = _http_json('https://zcode.z.ai/api/v1/zcode-plan/billing/balance'
                          f'?app_version={ZCODE_PLAN_APP_VERSION}',
                          {'Authorization': f'Bearer {login_jwt}',
                           'X-Device-Mid': str(uuid.uuid4())}, timeout)
        entry = parse_zcode_start(data)
        windows += entry['windows']
        extras.update(entry['extras'])
    return _source_entry('zcode', windows, extras)


# ---------------------------------------------------------------- claude

# The stored OAuth access token lives ~8h and is renewed by the claude CLI
# whenever it runs; quota treats a token as stale slightly before that so the
# refresh probe is not racing the usage request.
CLAUDE_REFRESH_MARGIN_S = 60


def _claude_oauth():
    path = Path.home() / '.claude' / '.credentials.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SourceError(f'cannot read Claude credentials: {redact(str(exc))}', 'config') from None
    oauth = (data.get('claudeAiOauth') or {}) if isinstance(data, dict) else {}
    if not isinstance(oauth, dict) or not oauth.get('accessToken'):
        raise SourceError('Claude access token unavailable', 'config')
    return oauth


def _claude_expired(oauth):
    # expiresAt is epoch milliseconds; missing or malformed means 'unknown' and
    # goes out as-is — a revoked token still surfaces through the 401 backstop.
    expires = oauth.get('expiresAt')
    if not isinstance(expires, (int, float)) or expires <= 0:
        return False
    return expires / 1000 <= time.time() + CLAUDE_REFRESH_MARGIN_S


def _claude_refresh_probe(timeout):
    """Headless haiku run that makes the claude CLI renew its stored OAuth
    token (`--model haiku`: alias for the latest of the family, so no version
    hardcode). Output and exit code carry no signal — the CLI refreshes before
    the model call; the caller judges success by re-reading the credentials
    file. Runs from a temp cwd so no project history is attached to the caller.
    """
    executable = shutil.which('claude')
    if not executable:
        return
    with tempfile.TemporaryDirectory() as cwd:
        try:
            subprocess.run([executable, '-p', '--model', 'haiku'],
                           input='Reply with the single word OK', text=True,
                           capture_output=True, timeout=timeout, cwd=cwd)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass


def _claude_token(timeout):
    oauth = _claude_oauth()
    if not _claude_expired(oauth):
        return oauth['accessToken']
    if not oauth.get('refreshToken'):
        raise SourceError('Claude access token expired and the stored credentials have no '
                          'refresh token; run claude interactively to re-authenticate',
                          'auth_expired')
    _claude_refresh_probe(timeout)
    oauth = _claude_oauth()
    if _claude_expired(oauth):
        raise SourceError('Claude access token expired and a headless claude run did not '
                          'refresh it; run claude interactively to re-authenticate',
                          'auth_expired')
    return oauth['accessToken']


def parse_claude(data):
    known = ('five_hour', 'seven_day')
    windows = []
    for key, block in data.items():
        if not isinstance(block, dict) or 'utilization' not in block:
            continue
        label = key.replace('_', '-') if key in known else f'model:{key}'
        dollars = block.get('limit_dollars') is not None
        windows.append(_window(label, used_percent=block.get('utilization'),
                               used=block.get('used_dollars') if dollars else None,
                               limit=block.get('limit_dollars') if dollars else None,
                               unit='usd' if dollars else None,
                               resets_at=block.get('resets_at')))
    extras = {}
    ember = data.get('cedar_ember')
    props = ember.get('event_props') if isinstance(ember, dict) else None
    if isinstance(props, dict) and props.get('tier'):
        extras['tier'] = props['tier']
    return _source_entry('claude', windows, extras)


def collect_claude(timeout):
    token = _claude_token(timeout)
    data = _http_json('https://api.anthropic.com/api/oauth/usage?cedar_ember=1', {
        'Authorization': f'Bearer {token}',
        'anthropic-beta': 'oauth-2025-04-20',
        'User-Agent': 'claude-cli/2.1.301 (external, cli)',
    }, timeout)
    if not isinstance(data, dict):
        raise SourceError('claude usage response is not an object', 'parse_error')
    return parse_claude(data)


# ---------------------------------------------------------------- agy


class _CredentialBlob(ctypes.Structure):
    _fields_ = [('Flags', ctypes.c_ulong), ('Type', ctypes.c_ulong),
                ('TargetName', ctypes.c_wchar_p), ('Comment', ctypes.c_wchar_p),
                ('LastWritten', ctypes.c_ulong * 2),
                ('CredentialBlobSize', ctypes.c_ulong),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)),
                ('Persist', ctypes.c_ulong), ('AttributeCount', ctypes.c_ulong),
                ('Attributes', ctypes.c_void_p), ('TargetAlias', ctypes.c_wchar_p),
                ('UserName', ctypes.c_wchar_p)]


def _agy_credential():
    """Antigravity OAuth token block from the Windows Credential Manager, read-only."""
    if not IS_WINDOWS:
        raise SourceError('agy quota reads the Windows Credential Manager (Windows only)', 'config')
    advapi = ctypes.windll.advapi32
    advapi.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                 ctypes.POINTER(ctypes.POINTER(_CredentialBlob))]
    pointer = ctypes.POINTER(_CredentialBlob)()
    # 1 = CRED_TYPE_GENERIC
    if not advapi.CredReadW(AGY_CRED_TARGET, 1, 0, ctypes.byref(pointer)):
        raise SourceError('no gemini:antigravity credential in the Windows Credential Manager '
                          '(log in with the agy CLI once)', 'config')
    try:
        blob = ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
    finally:
        advapi.CredFree(pointer)
    try:
        token = json.loads(blob.decode('utf-8', 'replace'))['token']
    except (KeyError, TypeError, ValueError):
        raise SourceError('gemini:antigravity credential blob is not the expected JSON',
                          'config') from None
    if not isinstance(token, dict):
        raise SourceError('gemini:antigravity credential token block is malformed', 'config')
    return token


def _agy_expiry(text):
    if not isinstance(text, str) or not text:
        return None
    match = re.match(r'^(.*\d\d:\d\d:\d\d)\.(\d+)(.*)$', text)
    if match:  # fromisoformat accepts at most 6 fractional digits
        text = f'{match.group(1)}.{match.group(2)[:6].ljust(6, "0")}{match.group(3)}'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _agy_refresh_probe(timeout):
    """Run `agy models` so the agy CLI renews its own expired credential with
    its own OAuth client material. Output and exit code carry no auth signal —
    the caller judges success by re-reading the keyring blob; the returned note
    (None, or a short non-secret reason) only shapes the final error text.
    stdin is closed so an unexpected interactive prompt cannot hang the probe."""
    executable = shutil.which('agy')
    if not executable:
        raise SourceError('agy access token expired and the agy CLI is not on PATH '
                          '(install it or log in with the agy CLI again)', 'config')
    try:
        result = subprocess.run([executable, 'models'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SourceError('`agy models` refresh probe timed out', 'timeout') from None
    except (OSError, ValueError) as exc:
        raise SourceError(f'`agy models` refresh probe could not run '
                          f'({type(exc).__name__})', 'config') from None
    if result.returncode:
        return f'`agy models` exited with code {result.returncode}'
    return None


def parse_agy(summary, models):
    groups = summary.get('groups') if isinstance(summary, dict) else None
    if not isinstance(groups, list):
        raise SourceError('agy quota summary has no groups list', 'parse_error')
    windows = []
    for group in groups:
        buckets = group.get('buckets') if isinstance(group, dict) else None
        for bucket in buckets if isinstance(buckets, list) else []:
            if not isinstance(bucket, dict) \
                    or not isinstance(bucket.get('remainingFraction'), (int, float)):
                continue
            # bucketId scopes the window to its model group (e.g. 3p-* = Claude and
            # GPT); the server may omit groups that live only in per-model buckets.
            label = bucket.get('bucketId') or bucket.get('window')
            resets_at = bucket.get('resetTime')
            windows.append(_window(label if isinstance(label, str) and label else 'bucket',
                                   used_percent=round((1 - bucket['remainingFraction']) * 100, 1),
                                   resets_at=resets_at if isinstance(resets_at, str) else None))
    gemini, others = _agy_family_windows(models)
    # gemini is the account's primary model and no summary group covers it, so its
    # rows lead the card; other per-model families trail the summary windows.
    windows = gemini + windows + others
    return _source_entry('agy', windows, _agy_model_extras(models))


def _agy_families(models, gemini):
    """Per-model buckets grouped by shared state (fraction + reset): equal state is
    one server-side bucket, labeled with the models' common prefix. Gemini buckets
    group separately (gemini=True) from the rest, so the primary model family never
    merges into a claude/gpt-labeled pool."""
    buckets = models.get('buckets') if isinstance(models, dict) else None
    if not isinstance(buckets, list):
        return []
    states = {}
    for bucket in buckets:
        if not isinstance(bucket, dict) \
                or not isinstance(bucket.get('remainingFraction'), (int, float)):
            continue
        model = bucket.get('modelId')
        if not isinstance(model, str) or model.startswith('gemini') != gemini:
            continue
        reset = bucket.get('resetTime')
        states.setdefault((bucket['remainingFraction'], reset if isinstance(reset, str) else None),
                          []).append(model)
    families = []
    for (fraction, reset), names in states.items():
        family = os.path.commonprefix(names).rstrip('-_') or sorted(names)[0]
        families.append((family, fraction, reset))
    return families


def _agy_family_windows(models):
    # The summary's groups can omit whole families (gemini at the time of writing),
    # while the IDE surfaces them from per-model buckets as weekly windows. Gemini
    # rows are emitted even when untouched (fraction 1): no summary group covers
    # them. A fully untouched other family (fraction 1) adds only a zero row already
    # covered by its summary group, so it stays in extras.
    def rows(families, include_untouched):
        return [_window(f'{family}-weekly', used_percent=round((1 - fraction) * 100, 1),
                        resets_at=reset)
                for family, fraction, reset in families
                if include_untouched or fraction < 1]
    return (rows(_agy_families(models, True), True),
            rows(_agy_families(models, False), False))


def _agy_model_extras(models):
    extras = {}
    for family, fraction, reset in _agy_families(models, True) + _agy_families(models, False):
        extras[f'{family}_remaining'] = round(fraction, 4)
        if reset:
            extras[f'{family}_resets_at'] = reset
    return extras


def _agy_stale(token):
    """True unless the token block carries an unexpired access token; a missing
    or malformed expiry counts as stale — a token stale in a way the blob does
    not show still surfaces through the 401 backstop of the quota requests."""
    access = token.get('access_token')
    if not isinstance(access, str) or not access:
        return True
    expiry = _agy_expiry(token.get('expiry'))
    return expiry is None or expiry <= datetime.now(timezone.utc)


def collect_agy(timeout):
    deadline = time.monotonic() + timeout

    def remaining():
        # the probe and both requests draw from the source's single wall-clock
        # budget; a later stage never gets a fresh full allowance
        return max(1.0, deadline - time.monotonic())

    token = _agy_credential()
    if _agy_stale(token):
        if not token.get('refresh_token'):
            raise SourceError('gemini:antigravity credential has no refresh token '
                              '(log in with the agy CLI again)', 'config')
        # The renewed token is written by the agy CLI into its own keyring blob;
        # the probe's exit code alone is no verdict, the re-read below decides.
        note = _agy_refresh_probe(remaining())
        token = _agy_credential()
        if _agy_stale(token):
            detail = f' ({note})' if note else ''
            raise SourceError('agy access token is expired and an `agy models` probe did '
                              f'not refresh the stored credential{detail}; run agy '
                              'interactively to re-authenticate', 'auth_expired')
    headers = {'Authorization': f"Bearer {token['access_token']}",
               'Content-Type': 'application/json', **AGY_API_HEADERS}
    # The summary covers the 5h/weekly group windows, while per-model buckets carry
    # families the summary omits (gemini at the time of writing). Both endpoints are
    # POST-with-empty-JSON-body; a GET is answered with an unspecific 404.
    summary = _http_json(f'{AGY_API_HOST}/v1internal:retrieveUserQuotaSummary', headers,
                         remaining(), data=b'{}')
    models = _http_json(f'{AGY_API_HOST}/v1internal:retrieveUserQuota', headers,
                        remaining(), data=b'{}')
    return parse_agy(summary, models)


# ---------------------------------------------------------------- openrouter


def parse_openrouter(data):
    payload = data.get('data') if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        raise SourceError('openrouter response has no data block', 'parse_error')
    windows = []
    limit, usage = payload.get('limit'), payload.get('usage')
    budget = payload.get('effective_budget') if isinstance(payload.get('effective_budget'), dict) else {}
    if isinstance(limit, (int, float)) and isinstance(usage, (int, float)):
        windows.append(_window('key', used_percent=round(usage / limit * 100, 1),
                               used=usage, limit=limit, unit='usd',
                               resets_at=budget.get('resets_at')))
    extras = {key: payload[key] for key in ('usage_daily', 'usage_weekly', 'usage_monthly',
                                            'is_free_tier', 'expires_at')
              if payload.get(key) is not None}
    return _source_entry('openrouter', windows, extras)


def collect_openrouter(timeout):
    key = _require_key('openrouter')
    data = _http_json('https://openrouter.ai/api/v1/key',
                      {'Authorization': f'Bearer {key}', 'User-Agent': BROWSER_UA}, timeout)
    return parse_openrouter(data)


# ---------------------------------------------------------------- commandcode


def parse_commandcode(credits, subscriptions):
    if not isinstance(credits, dict):
        raise SourceError('commandcode credits response is not an object', 'parse_error')
    limits = credits.get('windowLimits') if isinstance(credits.get('windowLimits'), dict) else {}
    windows = []
    for key, label in (('fiveHour', '5h'), ('weekly', 'weekly')):
        block = limits.get(key)
        if not isinstance(block, dict) or not isinstance(block.get('cap'), (int, float)):
            continue
        used = block.get('used') if isinstance(block.get('used'), (int, float)) else 0
        resets_at = _iso_from_epoch(block.get('resetAt') / 1000
                                    if isinstance(block.get('resetAt'), (int, float)) else None)
        windows.append(_window(label, used_percent=round(used / block['cap'] * 100, 1),
                               used=used, limit=block['cap'], unit='credits', resets_at=resets_at))
    credits_block = credits.get('credits') if isinstance(credits.get('credits'), dict) else {}
    extras = {}
    if isinstance(credits_block.get('monthlyCredits'), (int, float)):
        extras['monthly_credits_remaining'] = credits_block['monthlyCredits']
    sub = subscriptions.get('data') if isinstance(subscriptions, dict) else None
    if isinstance(sub, dict):
        for key, name in (('planId', 'plan'), ('currentPeriodEnd', 'period_end'), ('status', 'status')):
            if sub.get(key):
                extras[name] = sub[key]
    return _source_entry('commandcode', windows, extras)


def collect_commandcode(timeout):
    key = _require_key('commandcode')
    headers = {'Authorization': f'Bearer {key}', 'User-Agent': BROWSER_UA}
    credits = _http_json('https://api.commandcode.ai/alpha/billing/credits', headers, timeout)
    subscriptions = _http_json('https://api.commandcode.ai/alpha/billing/subscriptions', headers, timeout)
    return parse_commandcode(credits, subscriptions)


# ---------------------------------------------------------------- opencode-go


def parse_opencode_go(data):
    if not isinstance(data, dict):
        raise SourceError('opencode-go usage response is not an object', 'parse_error')
    windows, extras = [], {}
    for key, block in data.items():
        if isinstance(block, dict) and isinstance(block.get('limit'), (int, float)):
            used = block.get('used') if isinstance(block.get('used'), (int, float)) else 0
            windows.append(_window(key, used_percent=round(used / block['limit'] * 100, 1),
                                   used=used, limit=block['limit'],
                                   unit=block.get('unit') if isinstance(block.get('unit'), str) else None,
                                   resets_at=block.get('resets_at') if isinstance(block.get('resets_at'), str) else None))
        elif not isinstance(block, (dict, list)):
            extras[key] = block
    return _source_entry('opencode-go', windows, extras)


def collect_opencode_go(timeout):
    key = _require_key('opencode-go')
    data = _http_json('https://opencode.ai/zen/go/v1/usage',
                      {'Authorization': f'Bearer {key}', 'User-Agent': BROWSER_UA}, timeout)
    return parse_opencode_go(data)


# ------------------------------------------------------- active detection


def _codex_logged_in() -> bool:
    home = os.environ.get('CODEX_HOME')
    base = Path(home).expanduser() if home else Path.home() / '.codex'
    try:
        raw = json.loads((base / 'auth.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return bool(raw) if isinstance(raw, dict) else False


def _zcode_plan_key_present() -> bool:
    if zcode_module is None:
        return False
    try:
        data = json.loads((zcode_module.zcode_home() / 'v2' / 'credentials.json')
                          .read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if any(isinstance(name, str) and name.startswith('account-provider:')
           and 'coding-plan' in name and name.endswith(':api-key')
           and isinstance(value, str) and value.strip()
           for name, value in data.items()):
        return True
    jwt = data.get('zcodejwttoken')
    return isinstance(jwt, str) and bool(jwt.strip())


def _claude_token_present() -> bool:
    try:
        data = json.loads((Path.home() / '.claude' / '.credentials.json')
                          .read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    oauth = data.get('claudeAiOauth') if isinstance(data, dict) else None
    return bool(oauth.get('accessToken')) if isinstance(oauth, dict) else False


def _opencode_key_present(provider) -> bool:
    try:
        return bool(_opencode_auth().get(provider))
    except SourceError:
        return False


def _agy_credential_present() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        token = _agy_credential()
    except SourceError:
        return False
    return bool(token.get('refresh_token'))


ACTIVE_CHECKS = {
    'codex': lambda: shutil.which('codex') is not None and _codex_logged_in(),
    'zcode': _zcode_plan_key_present,
    'claude': _claude_token_present,
    'agy': lambda: _agy_credential_present(),
    'openrouter': lambda: _opencode_key_present('openrouter'),
    'commandcode': lambda: _opencode_key_present('commandcode'),
    'opencode-go': lambda: _opencode_key_present('opencode-go'),
}


def active_sources(selected) -> list:
    """Sources with local credentials present; the rest are skipped entirely.

    Offline presence checks only — no decryption, no network, no CLI start
    (beyond a PATH lookup for codex). Applied when no --only is given;
    an explicit --only always collects the named sources for diagnosis.
    """
    return [name for name in selected if ACTIVE_CHECKS[name]()]


# ---------------------------------------------------------------- collection

COLLECTORS = {'codex': collect_codex, 'zcode': collect_zcode, 'claude': collect_claude,
              'agy': collect_agy,
              'openrouter': collect_openrouter, 'commandcode': collect_commandcode,
              'opencode-go': collect_opencode_go}


def _run_one(name, timeout):
    try:
        return COLLECTORS[name](timeout)
    except Exception as exc:  # isolation: a broken collector is data, not a crash
        return _error_entry(name, exc)


def collect(selected, timeout):
    """Collect the selected sources; each gets its own wall-clock budget."""
    entries = [None] * len(selected)
    threads = []

    def worker(index, name):
        entries[index] = _run_one(name, timeout)

    for index, name in enumerate(selected):
        thread = threading.Thread(target=worker, args=(index, name), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(timeout)
    for index, name in enumerate(selected):
        if entries[index] is None:
            entries[index] = _error_entry(name, SourceError(
                f'collection exceeded {timeout}s budget', 'timeout'))
    return {'version': 1, 'generated_at': now_iso(), 'sources': entries}
