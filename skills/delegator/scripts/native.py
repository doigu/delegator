"""Small shared event result; only native error channels reach classification."""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def redact(text):
    text = re.sub(r'(?i)(\b(?:proxy-)?authorization["\s]*[:=]\s*)[^\r\n]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|password|passwd|secret)["\s]*[:=]\s*)["\']?[^\s"\',;}]+', r'\1[redacted]', text)
    return re.sub(r'\b(?:sk-|gh[pousr]_|tvly-)[A-Za-z0-9_-]{8,}', '[redacted]', text)


def classify(message):
    text = ' '.join(message.replace('’', "'").lower().split())
    if any(p in text for p in ('selected model is at capacity', 'flex capacity unavailable',
                               'experiencing high demand', 'server_overloaded', 'overloaded_error',
                               'the system is busy', 'retries have reached the maximum',
                               'provider returned a server error', 'server_error',
                               'internal server error', 'service unavailable',
                               'bad gateway', 'gateway timeout', 'model is overloaded',
                               'network connection failed')):
        return 'capacity'
    if any(p in text for p in ('hit your usage limit', 'out of credits', 'hit your spend cap',
                               'quota exceeded', 'insufficient_quota', 'quota_exceeded',
                               'quota exhausted', 'usage_limit_reached', 'usage limit reached',
                               'credit balance is too low',
                               'exceeded your current quota', 'insufficient_balance',
                               'insufficient quota', 'insufficient balance', 'balance depleted')):
        return 'quota'
    if any(p in text for p in ('unauthorized', 'authentication', 'invalid api key', 'statuscode": 401')):
        return 'auth'
    if any(p in text for p in ('permission', 'approval', 'user input', 'question rejected')):
        return 'needs_user_input'
    return 'native_error'


def reset_at(message):
    match = re.search(r'(?:reset_time|reset_at|resets? at)["\s:=]+(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))', message, re.I)
    if match:
        try:
            return datetime.fromisoformat(match[1].replace('Z', '+00:00')).isoformat()
        except ValueError:
            return None
    match = re.search(r'retry[-_]after["\s:=]+(\d+)', message, re.I)
    if match:
        return (datetime.now(timezone.utc) + timedelta(seconds=int(match[1]))).isoformat()
    match = re.search(r'try again in\s+((?:\d+\s*(?:days?|hours?|minutes?|seconds?)[\s,]*)+)', message, re.I)
    if match:
        seconds = sum(int(n) * {'d':86400, 'h':3600, 'm':60, 's':1}[unit.lower()[0]]
                      for n, unit in re.findall(r'(\d+)\s*(days?|hours?|minutes?|seconds?)', match[1], re.I))
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    match = re.search(r'try again at\s+([A-Za-z]{3,}\s+\d{1,2}(?:st|nd|rd|th)?,\s*\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM))',
                      message, re.I)
    if match:
        value = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', match.group(1), flags=re.I)
        for fmt in ('%b %d, %Y %I:%M %p', '%B %d, %Y %I:%M %p',
                    '%b %d, %Y %I:%M:%S %p', '%B %d, %Y %I:%M:%S %p'):
            try:
                parsed = datetime.strptime(value, fmt)
            except ValueError:
                continue
            return parsed.replace(tzinfo=timezone.utc).isoformat()
    return None


MAX_DENIED_ACTIONS = 20


@dataclass
class Summary:
    answer: str = ''
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    reported: dict = field(default_factory=lambda: dict(harness=None, provider=None, model=None, effort=None))
    error: str | None = None
    reason: str | None = None
    reset: str | None = None
    finished: bool = False
    limited: bool = False
    last_tool: str | None = None
    completed_tools: int = 0
    tools_observed: bool = False
    denied_actions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def fail(self, message):
        reason = classify(message)
        # A later generic failure must not hide an observed provider limit.
        if self.reason == 'quota' or (self.reason == 'capacity' and reason == 'native_error'):
            return
        self.error, self.reason = redact(message)[:500], reason
        self.reset = reset_at(message) if reason == 'quota' else None

    def warn(self, message):
        # An adapter-level diagnostic for the result warnings; never a failure.
        if message not in self.warnings and len(self.warnings) < 5:
            self.warnings.append(redact(message)[:500])

    def deny(self, tool, detail):
        # A native permission denial; the runner decides whether it stopped the work.
        if len(self.denied_actions) < MAX_DENIED_ACTIONS:
            self.denied_actions.append({'tool': tool if isinstance(tool, str) else None,
                                        'detail': redact(detail)[:300]})

    def consume(self, adapter, channel, line, session=None):
        line = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', line)
        if channel == 'stderr':
            if session is not None:
                session.stderr(self, line)
            else:
                adapter.stderr(self, line)
            return None
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if isinstance(event, dict):
            if session is not None:
                return session.event(self, event)
            adapter.event(self, event)
        return None

    def add_usage(self, values):
        for key, value in values.items():
            if type(value) in (int, float):
                self.usage[key] = self.usage.get(key, 0) + value
