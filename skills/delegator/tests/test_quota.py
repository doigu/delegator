import importlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
quota = importlib.import_module('quota')
quota_sources = importlib.import_module('quota_sources')
native = importlib.import_module('native')
cli = importlib.import_module('delegator')

ZCODE_RESPONSE = {
    'code': 200, 'msg': 'Operation successful', 'success': True,
    'data': {'limits': [
        {'type': 'CREDIT_LIMIT', 'unit': 3, 'number': 5, 'usage': 2000, 'currentValue': 1513,
         'remaining': 486, 'percentage': 75, 'nextResetTime': 1792775906743},
        {'type': 'CREDIT_LIMIT', 'unit': 6, 'number': 1, 'usage': 10000, 'currentValue': 5418,
         'remaining': 4581, 'percentage': 54, 'nextResetTime': 1793220599999},
    ], 'level': 'lite'},
}

ZCODE_START_RESPONSE = {
    'code': 0, 'msg': 'ok', 'success': True,
    'data': {'server_time': 1792923537,
             'balances': [
                 {'bucket_id': 'bucket_1', 'show_name': 'GLM-5.3-Flash',
                  'capabilities': ['model:glm-5.3-flash'], 'unit_type': 'token',
                  'total_units': 300000000, 'used_units': 126745836,
                  'remaining_units': 173254164, 'expires_at': 1792976400,
                  'period_start': None, 'period_end': None},
                 {'bucket_id': 'bucket_2', 'show_name': 'GLM-5.3',
                  'capabilities': ['model:glm-5.3'], 'unit_type': 'token',
                  'total_units': 200000000, 'used_units': 162542872,
                  'remaining_units': 37457128, 'expires_at': 1792976400,
                  'period_start': None, 'period_end': None},
                 {'bucket_id': 'bucket_3', 'show_name': 'Broken',
                  'capabilities': [], 'unit_type': 'token',
                  'total_units': None, 'used_units': 5, 'remaining_units': 0,
                  'expires_at': None, 'period_start': None, 'period_end': None},
             ],
             'plans': [{'plan_id': 'zcode-v3-start-plan-1024', 'status': 'active'}]},
}

CODEX_RATE_LIMITS = {
    'rateLimits': {
        'limitId': 'codex', 'primary': {'usedPercent': 75, 'windowDurationMins': 10080,
                                        'resetsAt': 1793294284},
        'secondary': None, 'credits': {'hasCredits': False, 'unlimited': False, 'balance': '0'},
        'planType': 'prolite',
    },
}
CODEX_USAGE = {'summary': {'lifetimeTokens': 1000, 'currentStreakDays': 3, 'peakDailyTokens': 900}}

CLAUDE_RESPONSE = {
    'five_hour': {'utilization': 42.0, 'resets_at': '2026-10-23T16:19:59+00:00',
                  'limit_dollars': None, 'used_dollars': None},
    'seven_day': {'utilization': 56.0, 'resets_at': '2026-10-28T07:59:59+00:00',
                  'limit_dollars': None, 'used_dollars': None},
    'iguana_necktie': {'utilization': 1.1, 'resets_at': '2026-11-05T07:59:00+00:00',
                       'limit_dollars': 100, 'used_dollars': 1.1, 'remaining_dollars': 98.9},
    'cedar_ember': {'eligible': False, 'grants': [],
                    'event_props': {'tier': 'claude_pro', 'surface': 'claude_code_cli'}},
}

OPENROUTER_RESPONSE = {
    'data': {'label': 'sk-or-v1-masked', 'limit': 10, 'usage': 1.9, 'limit_remaining': 8.1,
             'usage_daily': 0, 'usage_weekly': 0.3, 'usage_monthly': 0.3, 'is_free_tier': False,
             'expires_at': None,
             'effective_budget': {'limit_usd': 10, 'spend_usd': 1.9, 'resets_at': None}},
}

COMMANDCODE_CREDITS = {
    'credits': {'belowThreshold': False, 'creditThreshold': 0, 'monthlyCredits': 51.7,
                'purchasedCredits': 0, 'freeCredits': 0},
    'windowLimits': {'limited': True, 'exceeded': None,
                     'fiveHour': {'used': 0.78, 'cap': 14, 'exceeded': False,
                                  'resetAt': 1792772618098},
                     'weekly': {'used': 18.3, 'cap': 35, 'exceeded': False,
                                'resetAt': 1793039711083}},
}
COMMANDCODE_SUBS = {'success': True, 'data': {'planId': 'individual-goat', 'status': 'active',
                                              'currentPeriodEnd': '2026-11-19T18:27:40.000Z'}}

AGY_SUMMARY = {
    'groups': [
        {'buckets': [
            {'bucketId': '3p-weekly', 'displayName': 'Weekly Limit Remaining', 'window': 'weekly',
             'resetTime': '2026-11-01T07:54:59Z', 'remainingFraction': 1},
            {'bucketId': '3p-5h', 'displayName': 'Five Hour Limit Remaining', 'window': '5h',
             'resetTime': '2026-10-25T12:54:59Z', 'remainingFraction': 0.75},
        ], 'displayName': 'Claude and GPT models',
           'description': 'Models within this group: Claude Opus, Claude Sonnet, GPT-OSS'},
    ],
    'description': 'Within each group, models share a weekly limit and a 5-hour limit.',
}
AGY_MODELS = {
    'buckets': [
        {'tokenType': 'WTUS', 'modelId': 'claude-opus-4-6-thinking', 'remainingFraction': 1,
         'resetTime': '2026-10-25T12:55:00Z'},
        {'tokenType': 'WTUS', 'modelId': 'claude-sonnet-4-6', 'remainingFraction': 1,
         'resetTime': '2026-10-25T12:55:00Z'},
        {'tokenType': 'WTUS', 'modelId': 'gemini-2.5-flash', 'remainingFraction': 0.0013318285,
         'resetTime': '2026-10-26T08:30:40Z'},
        {'tokenType': 'WTUS', 'modelId': 'gemini-3.8-flash-tiered', 'remainingFraction': 0.0013318285,
         'resetTime': '2026-10-26T08:30:40Z'},
        {'tokenType': 'WTUS', 'modelId': 'gpt-oss-120b-medium', 'remainingFraction': 1},
    ],
}
AGY_TOKEN_FRESH = {'access_token': 'a', 'refresh_token': 'r',
                   'expiry': '2999-01-01T00:00:00+00:00', 'token_type': 'Bearer'}
AGY_TOKEN_EXPIRED = {'access_token': 'stale', 'refresh_token': 'r',
                     'expiry': '2020-01-01T00:00:00+00:00', 'token_type': 'Bearer'}


def labels(entry):
    return [window['label'] for window in entry['windows']]


# ---------------------------------------------------------------- parsers


def test_parse_zcode_windows():
    entry = quota_sources.parse_zcode(ZCODE_RESPONSE)
    assert entry['status'] == 'ok' and labels(entry) == ['5h', '1w']
    window = entry['windows'][0]
    assert window['used_percent'] == 75 and window['used'] == 1513 and window['limit'] == 2000
    assert window['unit'] == 'credits' and window['resets_at'].startswith('2026-10-23T')
    assert entry['extras'] == {'level': 'lite'}


def test_parse_zcode_start_windows():
    entry = quota_sources.parse_zcode_start(ZCODE_START_RESPONSE)
    assert entry['status'] == 'ok' and labels(entry) == ['GLM-5.3-Flash', 'GLM-5.3']
    window = entry['windows'][0]
    assert window['used_percent'] == round(126745836 / 300000000 * 100, 1)
    assert window['used'] == 126745836 and window['limit'] == 300000000
    assert window['unit'] == 'tokens'
    assert window['window_minutes'] is None and window['resets_at'] is None
    assert entry['extras'] == {'start_plan_expires_at': '2026-10-26T01:00:00+00:00'}


def test_parse_zcode_start_rejects_malformed_and_empty():
    with pytest.raises(quota_sources.SourceError):
        quota_sources.parse_zcode_start({'data': {}})
    empty = quota_sources.parse_zcode_start({'code': 0, 'data': {'balances': []}})
    assert empty['windows'] == [] and empty['extras'] == {}
    no_expiry = quota_sources.parse_zcode_start({
        'code': 0, 'data': {'balances': [{'show_name': 'M', 'total_units': 10, 'used_units': 1,
                                          'expires_at': None}]}})
    assert labels(no_expiry) == ['M'] and no_expiry['extras'] == {}


def test_collect_zcode_merges_api_key_and_start_plan(monkeypatch):
    seen = {}

    def http_json(url, headers, timeout, data=None):
        seen.setdefault('urls', []).append(url)
        seen.setdefault('headers', []).append(headers)
        return ZCODE_RESPONSE if 'monitor/usage' in url else ZCODE_START_RESPONSE

    monkeypatch.setattr(quota_sources, '_zcode_api_key', lambda: 'stored-key')
    monkeypatch.setattr(quota_sources, '_zcode_login_jwt', lambda: 'login-jwt')
    monkeypatch.setattr(quota_sources, '_http_json', http_json)
    entry = quota_sources.collect_zcode(10)
    assert entry['status'] == 'ok'
    assert labels(entry) == ['5h', '1w', 'GLM-5.3-Flash', 'GLM-5.3']
    assert entry['extras'] == {'level': 'lite', 'start_plan_expires_at': '2026-10-26T01:00:00+00:00'}
    assert [url.split('//')[1].split('/')[0] for url in seen['urls']] == ['api.z.ai', 'zcode.z.ai']
    assert seen['headers'][0]['Authorization'] == 'Bearer stored-key'
    assert seen['headers'][1]['Authorization'] == 'Bearer login-jwt'
    import uuid as uuid_module
    uuid_module.UUID(seen['headers'][1]['X-Device-Mid'])
    assert 'login-jwt' not in json.dumps(entry) and 'stored-key' not in json.dumps(entry)


def test_collect_zcode_start_plan_only(monkeypatch):
    monkeypatch.setattr(quota_sources, '_zcode_api_key', lambda: None)
    monkeypatch.setattr(quota_sources, '_zcode_login_jwt', lambda: 'login-jwt')
    monkeypatch.setattr(quota_sources, '_http_json',
                        lambda url, headers, timeout, data=None: ZCODE_START_RESPONSE)
    entry = quota_sources.collect_zcode(10)
    assert entry['status'] == 'ok'
    assert labels(entry) == ['GLM-5.3-Flash', 'GLM-5.3']
    assert 'level' not in entry['extras']


def test_collect_zcode_api_key_only(monkeypatch):
    monkeypatch.setattr(quota_sources, '_zcode_api_key', lambda: 'stored-key')
    monkeypatch.setattr(quota_sources, '_zcode_login_jwt', lambda: None)
    monkeypatch.setattr(quota_sources, '_http_json',
                        lambda url, headers, timeout, data=None: ZCODE_RESPONSE)
    entry = quota_sources.collect_zcode(10)
    assert entry['status'] == 'ok'
    assert labels(entry) == ['5h', '1w']
    assert entry['extras'] == {'level': 'lite'}


def test_collect_zcode_without_any_credential_is_config_error(monkeypatch):
    monkeypatch.setattr(quota_sources, '_zcode_api_key', lambda: None)
    monkeypatch.setattr(quota_sources, '_zcode_login_jwt', lambda: None)
    snapshot = quota_sources.collect(['zcode'], 10)
    entry = snapshot['sources'][0]
    assert entry['status'] == 'error' and entry['error'].startswith('config:')


def test_parse_codex_windows():
    entry = quota_sources.parse_codex(CODEX_RATE_LIMITS, CODEX_USAGE)
    assert labels(entry) == ['primary']
    window = entry['windows'][0]
    assert window['used_percent'] == 75 and window['window_minutes'] == 10080
    assert window['resets_at'].startswith('2026-')
    assert entry['extras']['plan'] == 'prolite' and entry['extras']['lifetimeTokens'] == 1000


def test_parse_claude_windows():
    entry = quota_sources.parse_claude(CLAUDE_RESPONSE)
    assert labels(entry) == ['five-hour', 'seven-day', 'model:iguana_necktie']
    model_window = entry['windows'][2]
    assert model_window['unit'] == 'usd' and model_window['limit'] == 100
    assert 'cedar_ember' not in str(labels(entry))
    assert entry['extras'] == {'tier': 'claude_pro'}


def write_claude_creds(tmp_path, **oauth):
    write_json(tmp_path / '.claude' / '.credentials.json', {'claudeAiOauth': oauth})


def make_claude_env(tmp_path, monkeypatch, seen):
    monkeypatch.setattr(Path, 'home', staticmethod(lambda: tmp_path))

    def fake_http(url, headers, timeout, data=None):
        seen['auth'] = headers.get('Authorization')
        return dict(CLAUDE_RESPONSE)

    monkeypatch.setattr(quota_sources, '_http_json', fake_http)


def test_claude_expired_margin():
    now_ms = time.time() * 1000
    assert quota_sources._claude_expired({'expiresAt': now_ms - 1000})
    assert quota_sources._claude_expired({'expiresAt': now_ms + 30_000})
    assert not quota_sources._claude_expired({'expiresAt': now_ms + 120_000})
    assert not quota_sources._claude_expired({'expiresAt': 'garbage'})
    assert not quota_sources._claude_expired({})


def test_collect_claude_fresh_token_skips_probe(tmp_path, monkeypatch):
    seen = {}
    make_claude_env(tmp_path, monkeypatch, seen)
    write_claude_creds(tmp_path, accessToken='fresh',
                       expiresAt=(time.time() + 3600) * 1000, refreshToken='r')
    calls = []
    monkeypatch.setattr(quota_sources, '_claude_refresh_probe',
                        lambda timeout: calls.append(timeout))
    entry = quota_sources.collect_claude(30)
    assert entry['status'] == 'ok'
    assert calls == [] and seen['auth'] == 'Bearer fresh'


def test_collect_claude_expired_token_refreshes_via_probe(tmp_path, monkeypatch):
    seen = {}
    make_claude_env(tmp_path, monkeypatch, seen)
    write_claude_creds(tmp_path, accessToken='stale',
                       expiresAt=(time.time() - 60) * 1000, refreshToken='r')
    budgets = []

    def fake_probe(timeout):
        budgets.append(timeout)
        write_claude_creds(tmp_path, accessToken='renewed',
                           expiresAt=(time.time() + 3600) * 1000, refreshToken='r')

    monkeypatch.setattr(quota_sources, '_claude_refresh_probe', fake_probe)
    entry = quota_sources.collect_claude(30)
    assert entry['status'] == 'ok'
    assert budgets == [30] and seen['auth'] == 'Bearer renewed'


def test_collect_claude_probe_failure_is_auth_expired_with_hint(tmp_path, monkeypatch):
    seen = {}
    make_claude_env(tmp_path, monkeypatch, seen)
    write_claude_creds(tmp_path, accessToken='stale',
                       expiresAt=(time.time() - 60) * 1000, refreshToken='r')
    calls = []
    monkeypatch.setattr(quota_sources, '_claude_refresh_probe',
                        lambda timeout: calls.append(timeout))
    with pytest.raises(quota_sources.SourceError) as excinfo:
        quota_sources.collect_claude(30)
    assert excinfo.value.kind == 'auth_expired'
    assert 'claude interactively' in str(excinfo.value)
    assert calls == [30] and seen == {}  # the usage request never went out


def test_collect_claude_expired_without_refresh_token_skips_probe(tmp_path, monkeypatch):
    seen = {}
    make_claude_env(tmp_path, monkeypatch, seen)
    write_claude_creds(tmp_path, accessToken='stale', expiresAt=(time.time() - 60) * 1000)
    calls = []
    monkeypatch.setattr(quota_sources, '_claude_refresh_probe',
                        lambda timeout: calls.append(timeout))
    with pytest.raises(quota_sources.SourceError) as excinfo:
        quota_sources.collect_claude(30)
    assert excinfo.value.kind == 'auth_expired'
    assert 'no refresh token' in str(excinfo.value)
    assert calls == [] and seen == {}


def test_collect_claude_missing_expires_at_requests_as_is(tmp_path, monkeypatch):
    seen = {}
    make_claude_env(tmp_path, monkeypatch, seen)
    write_claude_creds(tmp_path, accessToken='t')
    calls = []
    monkeypatch.setattr(quota_sources, '_claude_refresh_probe',
                        lambda timeout: calls.append(timeout))
    entry = quota_sources.collect_claude(30)
    assert entry['status'] == 'ok'
    assert calls == [] and seen['auth'] == 'Bearer t'


def test_parse_openrouter_windows():
    entry = quota_sources.parse_openrouter(OPENROUTER_RESPONSE)
    assert labels(entry) == ['key']
    window = entry['windows'][0]
    assert window['unit'] == 'usd' and window['used'] == 1.9 and window['limit'] == 10
    assert entry['extras']['usage_weekly'] == 0.3
    assert 'sk-or-v1' not in json.dumps(entry)


def test_parse_commandcode_windows():
    entry = quota_sources.parse_commandcode(COMMANDCODE_CREDITS, COMMANDCODE_SUBS)
    assert labels(entry) == ['5h', 'weekly']
    window = entry['windows'][0]
    assert window['used_percent'] == round(0.78 / 14 * 100, 1) and window['unit'] == 'credits'
    assert entry['extras']['plan'] == 'individual-goat'
    assert entry['extras']['monthly_credits_remaining'] == 51.7


def test_parse_opencode_go_tolerant():
    entry = quota_sources.parse_opencode_go(
        {'usage': {'limit': 30, 'used': 10, 'unit': 'usd'}, 'plan': 'go'})
    assert labels(entry) == ['usage']
    assert entry['extras'] == {'plan': 'go'}


def test_parse_agy_windows():
    entry = quota_sources.parse_agy(AGY_SUMMARY, AGY_MODELS)
    assert entry['status'] == 'ok'
    # gemini leads the card even though its coverage comes from per-model buckets,
    # not the summary; summary buckets are scoped by bucketId (3p-* = the Claude
    # and GPT group) and untouched non-gemini families stay extras-only — their
    # zero usage is already covered by the 3p group
    gemini = entry['windows'][0]
    assert gemini['label'] == 'gemini-weekly'
    assert gemini['used_percent'] == 99.9 and gemini['resets_at'] == '2026-10-26T08:30:40Z'
    assert labels(entry) == ['gemini-weekly', '3p-weekly', '3p-5h']
    assert entry['windows'][1]['used_percent'] == 0.0
    assert entry['windows'][1]['resets_at'] == '2026-11-01T07:54:59Z'
    window = entry['windows'][2]
    assert window['used_percent'] == 25.0 and window['resets_at'] == '2026-10-25T12:54:59Z'
    assert entry['extras'] == {'claude_remaining': 1,
                               'claude_resets_at': '2026-10-25T12:55:00Z',
                               'gemini_remaining': 0.0013,
                               'gemini_resets_at': '2026-10-26T08:30:40Z',
                               'gpt-oss-120b-medium_remaining': 1}
    serialized = json.dumps(entry)
    assert 'access_token' not in serialized and 'GOCSPX' not in serialized


def test_parse_agy_gemini_window_when_untouched():
    entry = quota_sources.parse_agy(AGY_SUMMARY, {'buckets': [
        {'tokenType': 'WTUS', 'modelId': 'gemini-2.5-flash', 'remainingFraction': 1,
         'resetTime': '2026-10-25T12:55:00Z'},
    ]})
    # gemini has no covering summary group, so its window shows even at zero usage
    assert labels(entry) == ['gemini-2.5-flash-weekly', '3p-weekly', '3p-5h']
    assert entry['windows'][0]['used_percent'] == 0.0
    # a single-model family is labeled by its full model id (no common prefix to trim)
    assert entry['extras'] == {'gemini-2.5-flash_remaining': 1,
                               'gemini-2.5-flash_resets_at': '2026-10-25T12:55:00Z'}


def test_parse_agy_gemini_never_merges_with_other_families():
    # equal fraction + reset used to collapse everything into one pool labeled by
    # the alphabetically first model; gemini groups separately on both sides
    entry = quota_sources.parse_agy(AGY_SUMMARY, {'buckets': [
        {'tokenType': 'WTUS', 'modelId': 'claude-opus-4-6-thinking', 'remainingFraction': 0.5,
         'resetTime': '2026-10-25T12:55:00Z'},
        {'tokenType': 'WTUS', 'modelId': 'claude-sonnet-4-6', 'remainingFraction': 0.5,
         'resetTime': '2026-10-25T12:55:00Z'},
        {'tokenType': 'WTUS', 'modelId': 'gemini-3-flash', 'remainingFraction': 0.5,
         'resetTime': '2026-10-25T12:55:00Z'},
        {'tokenType': 'WTUS', 'modelId': 'gemini-2.5-pro', 'remainingFraction': 1,
         'resetTime': '2026-10-25T12:55:00Z'},
        {'tokenType': 'WTUS', 'modelId': 'gpt-oss-120b-medium', 'remainingFraction': 1,
         'resetTime': '2026-10-25T12:55:00Z'},
    ]})
    assert labels(entry) == ['gemini-3-flash-weekly', 'gemini-2.5-pro-weekly',
                             '3p-weekly', '3p-5h', 'claude-weekly']
    assert entry['windows'][0]['used_percent'] == 50.0
    assert entry['windows'][1]['used_percent'] == 0.0
    assert entry['windows'][4]['used_percent'] == 50.0
    # untouched non-gemini families stay extras-only
    assert entry['extras'] == {'claude_remaining': 0.5,
                               'claude_resets_at': '2026-10-25T12:55:00Z',
                               'gemini-3-flash_remaining': 0.5,
                               'gemini-3-flash_resets_at': '2026-10-25T12:55:00Z',
                               'gemini-2.5-pro_remaining': 1,
                               'gemini-2.5-pro_resets_at': '2026-10-25T12:55:00Z',
                               'gpt-oss-120b-medium_remaining': 1,
                               'gpt-oss-120b-medium_resets_at': '2026-10-25T12:55:00Z'}


def test_parse_agy_rejects_malformed():
    with pytest.raises(quota_sources.SourceError):
        quota_sources.parse_agy({'nope': 1}, AGY_MODELS)


def test_parse_rejects_malformed():
    with pytest.raises(quota_sources.SourceError):
        quota_sources.parse_zcode({'data': {}})
    with pytest.raises(quota_sources.SourceError):
        quota_sources.parse_codex({}, {})
    with pytest.raises(quota_sources.SourceError):
        quota_sources.parse_openrouter({'nope': 1})


# ---------------------------------------------------------------- isolation


def test_collect_isolates_broken_collector(monkeypatch):
    def broken(timeout):
        raise RuntimeError('boom with key sk-abc123456789012345')

    monkeypatch.setitem(quota_sources.COLLECTORS, 'codex', broken)
    monkeypatch.setitem(quota_sources.COLLECTORS, 'zcode',
                        lambda timeout: quota_sources._source_entry('zcode', [], {}))
    snapshot = quota_sources.collect(['codex', 'zcode'], 10)
    entries = {entry['source']: entry for entry in snapshot['sources']}
    assert entries['zcode']['status'] == 'ok'
    assert entries['codex']['status'] == 'error'
    assert entries['codex']['error'].startswith('unexpected_error:')
    assert 'sk-abc123456789012345' not in entries['codex']['error']


def test_collect_reports_timeout(monkeypatch):
    def slow(timeout):
        time.sleep(0.6)
        return quota_sources._source_entry('claude', [], {})

    monkeypatch.setitem(quota_sources.COLLECTORS, 'claude', slow)
    monkeypatch.setitem(quota_sources.COLLECTORS, 'codex',
                        lambda timeout: quota_sources._source_entry('codex', [], {}))
    started = time.monotonic()
    snapshot = quota_sources.collect(['claude', 'codex'], 0.2)
    entries = {entry['source']: entry for entry in snapshot['sources']}
    assert entries['claude']['status'] == 'error'
    assert 'timeout' in entries['claude']['error']
    assert entries['codex']['status'] == 'ok'
    assert time.monotonic() - started < 5


def test_http_error_is_classified(monkeypatch):
    import urllib.error

    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, None)

    monkeypatch.setattr(quota_sources.urllib.request, 'urlopen', fail)
    monkeypatch.setattr(quota_sources, '_zcode_api_key', lambda: 'key')
    monkeypatch.setattr(quota_sources, '_zcode_login_jwt', lambda: None)
    snapshot = quota_sources.collect(['zcode'], 10)
    entry = snapshot['sources'][0]
    assert entry['status'] == 'error'
    assert entry['error'].startswith('auth_expired: HTTP 401')
    assert 'key' not in entry['error']


# ---------------------------------------------------------------- agy collector


def fake_agy_http(seen):
    def http_json(url, headers, timeout, data=None):
        seen.setdefault('urls', []).append(url)
        seen.setdefault('headers', []).append(headers)
        seen.setdefault('data', []).append(data)
        return AGY_SUMMARY if url.endswith(':retrieveUserQuotaSummary') else AGY_MODELS
    return http_json


def test_collect_agy_fresh_token_sends_required_headers(monkeypatch):
    seen = {}
    monkeypatch.setattr(quota_sources, '_agy_credential', lambda: AGY_TOKEN_FRESH)
    monkeypatch.setattr(quota_sources, '_http_json', fake_agy_http(seen))
    probes = []
    monkeypatch.setattr(quota_sources, '_agy_refresh_probe',
                        lambda timeout: probes.append(timeout))
    # a fresh token collects even with no agy CLI on PATH: the probe is only
    # the renewal path, not part of the request flow
    monkeypatch.setattr(quota_sources.shutil, 'which', lambda name: None)
    entry = quota_sources.collect_agy(10)
    assert entry['status'] == 'ok'
    assert probes == []
    assert [url.rsplit('/', 1)[-1] for url in seen['urls']] == \
        ['v1internal:retrieveUserQuotaSummary', 'v1internal:retrieveUserQuota']
    # both agy endpoints are POST with an empty JSON body; a GET is answered with 404
    assert seen['data'] == [b'{}', b'{}']
    headers = seen['headers'][0]
    assert headers['x-goog-api-client'] == 'antigravity-cli/1.0'
    assert headers['Authorization'] == 'Bearer a'
    assert 'GOCSPX' not in json.dumps(entry)


def test_collect_agy_probe_renews_expired_token(monkeypatch):
    seen = {}
    reads = []

    def credential():
        reads.append(1)
        return AGY_TOKEN_EXPIRED if len(reads) == 1 else AGY_TOKEN_FRESH

    probes = []
    monkeypatch.setattr(quota_sources, '_agy_credential', credential)
    monkeypatch.setattr(quota_sources, '_agy_refresh_probe',
                        lambda timeout: probes.append(timeout))
    monkeypatch.setattr(quota_sources, '_http_json', fake_agy_http(seen))
    assert quota_sources.collect_agy(10)['status'] == 'ok'
    # the verdict comes from re-reading the credential, not from the probe
    assert len(reads) == 2 and len(probes) == 1 and probes[0] <= 10
    assert seen['headers'][0]['Authorization'] == 'Bearer a'


def test_collect_agy_expired_without_refresh_token_is_config_error(monkeypatch):
    monkeypatch.setattr(quota_sources, '_agy_credential',
                        lambda: {'access_token': 'stale', 'expiry': '2020-01-01T00:00:00+00:00'})
    probes = []
    monkeypatch.setattr(quota_sources, '_agy_refresh_probe',
                        lambda timeout: probes.append(timeout))
    snapshot = quota_sources.collect(['agy'], 10)
    entry = snapshot['sources'][0]
    assert entry['status'] == 'error'
    assert entry['error'].startswith('config:')
    assert 'refresh token' in entry['error'] and probes == []


@pytest.mark.parametrize('note, fragment', [
    (None, 'did not refresh the stored credential; run agy interactively'),
    ('`agy models` exited with code 7', 'exited with code 7'),
])
def test_collect_agy_probe_without_renewal_is_auth_expired(monkeypatch, note, fragment):
    # a completed probe with no keyring change is an auth verdict made by the
    # re-read; the note is diagnostics, and the stale token value never leaks
    monkeypatch.setattr(quota_sources, '_agy_credential', lambda: AGY_TOKEN_EXPIRED)
    monkeypatch.setattr(quota_sources, '_agy_refresh_probe', lambda timeout: note)
    requests = []
    monkeypatch.setattr(quota_sources, '_http_json',
                        lambda *args, **kwargs: requests.append(args))
    snapshot = quota_sources.collect(['agy'], 10)
    entry = snapshot['sources'][0]
    assert entry['status'] == 'error'
    assert entry['error'].startswith('auth_expired:')
    assert fragment in entry['error']
    assert 'stale' not in entry['error'] and requests == []


def test_agy_refresh_probe_classifies_failures(monkeypatch):
    monkeypatch.setattr(quota_sources.shutil, 'which', lambda name: None)
    with pytest.raises(quota_sources.SourceError) as excinfo:
        quota_sources._agy_refresh_probe(10)
    assert excinfo.value.kind == 'config' and 'not on PATH' in str(excinfo.value)

    monkeypatch.setattr(quota_sources.shutil, 'which', lambda name: 'agy')

    def timed_out(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr(quota_sources.subprocess, 'run', timed_out)
    with pytest.raises(quota_sources.SourceError) as excinfo:
        quota_sources._agy_refresh_probe(10)
    assert excinfo.value.kind == 'timeout'

    def refused(args, **kwargs):
        raise OSError('permission denied anywhere')

    monkeypatch.setattr(quota_sources.subprocess, 'run', refused)
    with pytest.raises(quota_sources.SourceError) as excinfo:
        quota_sources._agy_refresh_probe(10)
    assert excinfo.value.kind == 'config'
    assert 'OSError' in str(excinfo.value) and 'permission denied' not in str(excinfo.value)


def test_agy_refresh_probe_runs_detached_and_reports_exit_code(monkeypatch):
    captured = {}

    class Result:
        returncode = 7

    def run(args, **kwargs):
        captured.update(argv=args, **kwargs)
        return Result()

    monkeypatch.setattr(quota_sources.shutil, 'which', lambda name: 'agy')
    monkeypatch.setattr(quota_sources.subprocess, 'run', run)
    # a nonzero exit is a diagnostic note for the caller, not a verdict: the
    # keyring re-read decides whether the renewal happened
    assert quota_sources._agy_refresh_probe(10) == '`agy models` exited with code 7'
    assert captured['argv'][-1] == 'models' and captured['stdin'] is subprocess.DEVNULL


def test_collect_agy_probe_and_requests_share_one_budget(monkeypatch):
    reads = []

    def credential():
        reads.append(1)
        return AGY_TOKEN_EXPIRED if len(reads) == 1 else AGY_TOKEN_FRESH

    probe_timeouts, request_timeouts = [], []

    def http_json(url, headers, timeout, data=None):
        request_timeouts.append(timeout)
        return AGY_SUMMARY if url.endswith(':retrieveUserQuotaSummary') else AGY_MODELS

    monkeypatch.setattr(quota_sources, '_agy_credential', credential)
    monkeypatch.setattr(quota_sources, '_agy_refresh_probe',
                        lambda timeout: probe_timeouts.append(timeout))
    monkeypatch.setattr(quota_sources, '_http_json', http_json)
    assert quota_sources.collect_agy(30)['status'] == 'ok'
    assert len(request_timeouts) == 2
    assert all(t <= 30 for t in probe_timeouts + request_timeouts)
    assert request_timeouts[1] <= request_timeouts[0]


def test_agy_source_keeps_no_oauth_client_material():
    # release gate: the agy quota path must not carry OAuth client material of
    # its own — renewal is delegated to the agy CLI probe (issue #136)
    source = Path(quota_sources.__file__).read_text(encoding='utf-8')
    assert 'GOCSPX' not in source
    assert '.apps.googleusercontent.com' not in source
    assert not hasattr(quota_sources, 'AGY_OAUTH_CLIENT_ID')
    assert not hasattr(quota_sources, 'AGY_OAUTH_CLIENT_SECRET')
    assert not hasattr(quota_sources, '_agy_refresh')


def test_agy_expiry_parsing():
    parse = quota_sources._agy_expiry
    assert parse('2026-10-25T10:40:28.3895681+03:00').isoformat() == \
        '2026-10-25T10:40:28.389568+03:00'
    assert parse('2026-10-25T10:40:28Z') is not None
    assert parse(None) is None and parse('') is None and parse('garbage') is None


# ---------------------------------------------------------------- rendering


def SNAPSHOT():
    return {
        'version': 1, 'generated_at': '2026-10-23T12:00:00+03:00',
        'sources': [
            {'source': 'codex', 'status': 'ok', 'fetched_at': '2026-10-23T12:00:01+03:00',
             'error': None, 'windows': [
                 {'label': 'primary</script>', 'used_percent': 42, 'used': None, 'limit': None,
                  'unit': None, 'window_minutes': 300, 'resets_at': '2026-10-23T15:00:00+00:00'}],
             'extras': {'plan': 'plus'}},
            {'source': 'opencode-go', 'status': 'error', 'fetched_at': '2026-10-23T12:00:01+03:00',
             'error': 'auth_expired: HTTP 401: api_key_expired', 'windows': [], 'extras': {}},
        ],
    }


def test_render_text_lines():
    text = quota.render_text(SNAPSHOT())
    assert 'codex         ok      primary</script>' in text
    assert '42%' in text and 'opencode-go   error   auth_expired' in text


def test_render_html_escapes_and_embeds_snapshot():
    html = quota.render_html(SNAPSHOT())
    assert '<script type="application/json" id="snapshot">' in html
    embedded = html.split('id="snapshot">', 1)[1].rsplit('</script>', 1)[0]
    assert '</script>' not in embedded and '<\\/' in embedded
    assert 'primary&lt;/script&gt;' in html
    assert 'auth_expired: HTTP 401' in html


def test_write_atomic_replaces(tmp_path):
    target = tmp_path / 'nested' / 'quota.html'
    target.parent.mkdir(parents=True)
    target.write_text('old', encoding='utf-8')
    quota.write_atomic(target, 'new')
    assert target.read_text(encoding='utf-8') == 'new'
    assert [p.name for p in target.parent.iterdir()] == ['quota.html']


def test_run_html_writes_target(tmp_path, monkeypatch):
    target = tmp_path / 'dashboard.html'
    import quota as quota_module
    monkeypatch.setattr(quota_sources, 'active_sources', lambda selected: selected)
    original = quota_sources.collect
    quota_sources.collect = lambda selected, timeout: SNAPSHOT()
    try:
        code, message = quota_module.run('html', str(target), None, 10)
    finally:
        quota_sources.collect = original
    assert code == 0 and message.endswith('dashboard.html')
    assert 'snapshot' in target.read_text(encoding='utf-8')


# ---------------------------------------------------------------- CLI


def test_cli_unknown_only_is_config_error(capsys):
    assert quota.main(['--only', 'nope']) == 2
    assert 'allowed:' in capsys.readouterr().err


def test_cli_only_accepts_argument_array_tokens(capsys, monkeypatch):
    # PowerShell turns an unquoted `--only codex,zcode` into separate arguments.
    seen = {}

    def fake_collect(selected, timeout):
        seen['selected'] = selected
        return SNAPSHOT()

    monkeypatch.setattr(quota_sources, 'collect', fake_collect)
    assert quota.main(['--only', 'codex', 'zcode', '--format', 'json']) == 0
    assert seen['selected'] == ['codex', 'zcode']
    assert quota.main(['--only', 'codex,zcode', '--format', 'json']) == 0
    assert seen['selected'] == ['codex', 'zcode']
    assert quota.main(['--only', 'agy', 'zcode', '--format', 'json']) == 0
    assert seen['selected'] == ['agy', 'zcode']
    assert quota.main(['--only', 'agy,zcode', '--format', 'json']) == 0
    assert seen['selected'] == ['agy', 'zcode']


def test_cli_json_alias(capsys, monkeypatch):
    monkeypatch.setattr(quota_sources, 'active_sources', lambda selected: selected)
    monkeypatch.setattr(quota_sources, 'collect', lambda selected, timeout: SNAPSHOT())
    assert quota.main(['--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['sources'][0]['source'] == 'codex'


def test_cli_text_default(capsys, monkeypatch):
    monkeypatch.setattr(quota_sources, 'active_sources', lambda selected: selected)
    monkeypatch.setattr(quota_sources, 'collect', lambda selected, timeout: SNAPSHOT())
    assert quota.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith('quota snapshot 2026-10-23T12:00:00+03:00')


def test_cli_help_documents_contract(capsys):
    with pytest.raises(SystemExit):
        quota.main(['--help'])
    text = capsys.readouterr().out
    for fragment in ('codex', 'zcode', 'claude', 'agy', 'openrouter', 'commandcode',
                     'opencode-go', 'ok | error', 'usd | credits', 'exit codes',
                     'used_percent', 'window_minutes', '--timeout', '--only',
                     'skipped automatically'):
        assert fragment in text, fragment


def test_delegator_quota_dispatch(capsys, monkeypatch):
    monkeypatch.setattr(quota_sources, 'active_sources', lambda selected: selected)
    monkeypatch.setattr(quota_sources, 'collect', lambda selected, timeout: SNAPSHOT())
    assert cli.main(['quota', '--format', 'json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['version'] == 1


def test_delegator_quota_json_alias_and_only(capsys, monkeypatch):
    seen = {}

    def fake_collect(selected, timeout):
        seen['selected'] = selected
        return SNAPSHOT()

    monkeypatch.setattr(quota_sources, 'collect', fake_collect)
    assert cli.main(['quota', '--json', '--only', 'codex']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert seen['selected'] == ['codex'] and payload['version'] == 1
    assert cli.main(['quota', '--only', 'codex', 'zcode']) == 0
    assert seen['selected'] == ['codex', 'zcode']
    assert cli.main(['quota', '--only', 'bogus']) == 2
    assert 'allowed:' in capsys.readouterr().err


# ---------------------------------------------------------------- active detection


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding='utf-8')


def make_active_env(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv('ZCODE_HOME', str(tmp_path / 'zcode'))
    monkeypatch.setattr(Path, 'home', staticmethod(lambda: tmp_path))
    monkeypatch.setattr(quota_sources.shutil, 'which',
                        lambda name: str(tmp_path / 'codex.exe') if name == 'codex' else None)
    # The agy presence check reads the real Windows Credential Manager; tests stay
    # offline by pinning it here and overriding explicitly where needed.
    monkeypatch.setattr(quota_sources, '_agy_credential_present', lambda: False)


def test_active_sources_presence_checks(tmp_path, monkeypatch):
    make_active_env(tmp_path, monkeypatch)
    assert quota_sources.active_sources(list(quota_sources.SOURCES)) == []

    write_json(tmp_path / 'codex' / 'auth.json', {'tokens': {'access_token': 't'}})
    assert quota_sources.active_sources(['codex']) == ['codex']

    write_json(tmp_path / 'zcode' / 'v2' / 'credentials.json',
               {'account-provider:coding-plan:account:zai-individual-coding-plan:a:b:api-key':
                'enc:v1:x'})
    assert quota_sources.active_sources(['zcode']) == ['zcode']

    write_json(tmp_path / '.claude' / '.credentials.json',
               {'claudeAiOauth': {'accessToken': 't'}})
    assert quota_sources.active_sources(['claude']) == ['claude']

    write_json(tmp_path / 'data' / 'opencode' / 'auth.json', {
        'opencode-go': {'type': 'api', 'key': 'k'},
        'openrouter': {'type': 'api', 'key': ''},
    })
    assert quota_sources.active_sources(['opencode-go', 'openrouter', 'commandcode']) \
        == ['opencode-go']


def test_active_sources_codex_requires_cli_and_auth(tmp_path, monkeypatch):
    make_active_env(tmp_path, monkeypatch)
    write_json(tmp_path / 'codex' / 'auth.json', {'tokens': {}})
    monkeypatch.setattr(quota_sources.shutil, 'which', lambda name: None)
    assert quota_sources.active_sources(['codex']) == []
    monkeypatch.setattr(quota_sources.shutil, 'which',
                        lambda name: str(tmp_path / 'codex.exe') if name == 'codex' else None)
    (tmp_path / 'codex' / 'auth.json').unlink()
    assert quota_sources.active_sources(['codex']) == []


def test_active_sources_agy_presence(monkeypatch):
    monkeypatch.setattr(quota_sources, 'IS_WINDOWS', True)
    monkeypatch.setattr(quota_sources, '_agy_credential', lambda: {'refresh_token': 'r'})
    assert quota_sources.active_sources(['agy']) == ['agy']
    monkeypatch.setattr(quota_sources, '_agy_credential', lambda: {'access_token': 'a'})
    assert quota_sources.active_sources(['agy']) == []
    monkeypatch.setattr(quota_sources, '_agy_credential_present', lambda: True)
    assert quota_sources.active_sources(['agy']) == ['agy']


def test_agy_credential_requires_windows(monkeypatch):
    monkeypatch.setattr(quota_sources, 'IS_WINDOWS', False)
    with pytest.raises(quota_sources.SourceError) as excinfo:
        quota_sources._agy_credential()
    assert excinfo.value.kind == 'config'
    assert quota_sources._agy_credential_present() is False


def test_run_skips_inactive_and_only_forces(monkeypatch):
    seen = {}

    def fake_collect(selected, timeout):
        seen['selected'] = list(selected)
        return {'version': 1, 'generated_at': 'x', 'sources': []}

    monkeypatch.setattr(quota_sources, 'collect', fake_collect)
    monkeypatch.setattr(quota_sources, 'active_sources',
                        lambda selected: [name for name in selected if name != 'opencode-go'])
    assert quota.run('json', None, None, 10)[0] == 0
    assert seen['selected'] == [name for name in quota_sources.SOURCES if name != 'opencode-go']
    assert quota.run('json', None, ['opencode-go'], 10)[0] == 0
    assert seen['selected'] == ['opencode-go']
