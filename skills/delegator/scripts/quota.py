#!/usr/bin/env python3
"""Read-only quota monitor for delegator harness accounts (issue: quota dashboard).

Collects plan-quota windows for codex, zcode, claude, agy, openrouter, commandcode,
and opencode-go; renders text, JSON snapshot, or a static HTML dashboard.
Standalone: `python -X utf8 -B quota.py [flags]`; also dispatched as
`delegator quota`. No LLM calls; secrets never enter output.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import quota_sources
from native import redact

SNAPSHOT_STATUS = 'ok | error'  # status enum; reasons live in `error`
DEFAULT_TIMEOUT = 30


def parse_only(value):
    if value is None:
        return list(quota_sources.SOURCES)
    if isinstance(value, (list, tuple)):
        # PowerShell turns an unquoted `a,b` into an argument array.
        value = ','.join(value)
    selected = [part.strip() for part in value.split(',') if part.strip()]
    unknown = [part for part in selected if part not in quota_sources.SOURCES]
    if not selected or unknown:
        raise ValueError('unknown --only sources: ' + ', '.join(unknown)
                         + '; allowed: ' + ', '.join(quota_sources.SOURCES))
    return selected


def _count_text(value):
    # Large integer counts (start-plan token buckets) must not collapse into
    # scientific notation; decimal values (usd windows) keep the compact form.
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return f'{int(value):,}'
    return f'{value:g}'


def render_text(snapshot):
    lines = ['quota snapshot ' + snapshot['generated_at']]
    for entry in snapshot['sources']:
        name = entry['source']
        if entry['status'] != 'ok':
            lines.append(f'{name:<13} {entry["status"]:<7} {entry["error"]}')
            continue
        if not entry['windows']:
            lines.append(f'{name:<13} ok      (no windows reported)')
        for window in entry['windows']:
            percent = window['used_percent']
            percent_text = f'{percent:g}%' if isinstance(percent, (int, float)) else '-'
            parts = [f'{name:<13} ok      {window["label"]:<9} {percent_text:>5}']
            if window['used'] is not None and window['limit'] is not None:
                unit = f' {window["unit"]}' if window['unit'] else ''
                parts.append(f'({_count_text(window["used"])}/{_count_text(window["limit"])}{unit})')
            elif window['window_minutes']:
                parts.append(f'({window["window_minutes"]}m window)')
            if window['resets_at']:
                parts.append('resets ' + window['resets_at'])
            lines.append('  '.join(parts))
        extras = '; '.join(f'{key}={entry["extras"][key]}' for key in entry['extras'])
        if extras:
            lines.append(f'{"":<13} extras: {extras}')
    return '\n'.join(lines)


_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="600">
<title>delegator quota</title>
<style>
body { font-family: 'Segoe UI', sans-serif; background: #f4f4f5; color: #18181b;
       margin: 0; padding: 2rem; }
h1 { font-size: 1.2rem; } .generated { color: #71717a; font-size: .85rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
        gap: 1rem; margin-top: 1rem; }
.card { background: #fff; border: 1px solid #e4e4e7; border-radius: 8px; padding: 1rem; }
.card h2 { font-size: 1rem; margin: 0 0 .5rem; display: flex; align-items: center;
           justify-content: space-between; }
.chip { font-size: .75rem; padding: .1rem .5rem; border-radius: 999px; }
.ok { background: #dcfce7; color: #166534; }
.error { background: #fee2e2; color: #991b1b; }
.window { margin: .6rem 0; }
.window .row { display: flex; justify-content: space-between; font-size: .85rem;
               margin-bottom: .2rem; }
.bar { height: 8px; background: #e4e4e7; border-radius: 4px; overflow: hidden; }
.bar > div { height: 100%; background: #2563eb; }
.bar > div.warn { background: #d97706; } .bar > div.crit { background: #dc2626; }
.extras { font-size: .78rem; color: #71717a; margin-top: .5rem; }
.error-text { font-size: .85rem; color: #991b1b; word-break: break-word; }
</style>
</head>
<body>
"""


def _bar(percent):
    if not isinstance(percent, (int, float)):
        return ''
    level = 'crit' if percent >= 90 else 'warn' if percent >= 75 else ''
    class_attr = f' class="{level}"' if level else ''
    return f'<div class="bar"><div{class_attr} style="width: {min(percent, 100):g}%"></div></div>'


def _window_html(window, escape):
    percent = window['used_percent']
    percent_text = f'{percent:g}%' if isinstance(percent, (int, float)) else 'n/a'
    detail = ''
    if window['used'] is not None and window['limit'] is not None:
        unit = f' {window["unit"]}' if window['unit'] else ''
        detail = f"{window['used']:g} / {window['limit']:g}{unit}"
    elif window['window_minutes']:
        detail = f"{window['window_minutes']}m window"
    row = (f'<div class="row"><span>{escape(window["label"])}</span>'
           f'<span>{escape(percent_text)}{" &middot; " + escape(detail) if detail else ""}</span></div>')
    resets = (f'<span class="extras">resets {escape(window["resets_at"])}</span>'
              if window['resets_at'] else '')
    return f'<div class="window">{row}{_bar(percent)}{resets}</div>'


def render_html(snapshot):
    import html
    escape = html.escape

    def esc(value):
        return escape(str(value))

    chips = ''.join(
        f'<span class="chip {"ok" if entry["status"] == "ok" else "error"}">{esc(entry["source"])}</span>'
        for entry in snapshot['sources'])
    cards = []
    for entry in snapshot['sources']:
        body = ''
        if entry['status'] != 'ok':
            body = f'<p class="error-text">{esc(entry["error"])}</p>'
        elif not entry['windows']:
            body = '<p class="extras">no windows reported</p>'
        else:
            body = ''.join(_window_html(window, escape) for window in entry['windows'])
            extras = ' &middot; '.join(f'{esc(key)}={esc(entry["extras"][key])}'
                                       for key in entry['extras'])
            if extras:
                body += f'<p class="extras">{extras}</p>'
        cards.append(f'<div class="card"><h2>{esc(entry["source"])}'
                     f'<span class="chip {"ok" if entry["status"] == "ok" else "error"}">'
                     f'{esc(entry["status"])}</span></h2>{body}</div>')
    embedded = json.dumps(snapshot, ensure_ascii=False).replace('</', '<\\/')
    return (_HTML_HEAD
            + f'<h1>delegator quota</h1>\n<p class="generated">generated {esc(snapshot["generated_at"])}</p>\n'
            + f'<p>{chips}</p>\n<div class="grid">{"".join(cards)}</div>\n'
            + f'<script type="application/json" id="snapshot">{embedded}</script>\n</body>\n</html>\n')


def write_atomic(path, text):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix=f'.{path.name}.', suffix='.tmp', delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(text)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def run(format, out, only, timeout):
    selected = parse_only(only)
    if only is None:
        # Inactive sources are skipped entirely: no local credential means
        # nothing to query; an explicit --only below forces them for diagnosis.
        selected = quota_sources.active_sources(selected)
    snapshot = quota_sources.collect(selected, timeout)
    if format == 'json':
        return 0, json.dumps(snapshot, ensure_ascii=False, indent=2)
    if format == 'text':
        return 0, render_text(snapshot)
    html = render_html(snapshot)
    target = Path(out).expanduser() if out else Path.home() / '.agents' / 'quota-dashboard.html'
    write_atomic(target, html)
    return 0, f'dashboard written: {target}'


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog='sources: ' + ', '.join(quota_sources.SOURCES) + '\n'
               'snapshot: {version, generated_at, sources: [{source, status: ' + SNAPSHOT_STATUS +
               ', fetched_at, error, windows: [{label, used_percent, used, limit, unit, '
               'window_minutes, resets_at}], extras}]}\n'
               'unit is usd | credits | tokens | requests | null; windows of different sources are not '
               'comparable; error is a classified reason (auth_expired, timeout, network, '
               'parse_error, config, unexpected_error) and never contains secrets\n'
               'without --only, sources without local credentials (missing CLI, key, or token '
               'file) are skipped automatically; an explicit --only always collects the named '
               'sources\n'
               'exit codes: 0 collected (source errors are data), 2 CLI/configuration error\n'
               'examples:\n'
               '  quota.py\n'
               '  quota.py --format json\n'
               '  quota.py --format html --out D:\\tools\\quota.html\n'
               '  quota.py --only codex,zcode --timeout 20')
    parser.add_argument('--format', choices=['text', 'json', 'html'], default='text',
                        help='text (default), json snapshot on stdout, or html dashboard file')
    parser.add_argument('--json', action='store_true',
                        help='alias of --format json (delegator management convention)')
    parser.add_argument('--out', help='html target path (default: ~/.agents/quota-dashboard.html)')
    parser.add_argument('--only', nargs='+',
                        help='comma-separated subset of: ' + ', '.join(quota_sources.SOURCES)
                             + '; skipped-source auto-detection is bypassed for named sources '
                               '(a quoted spec and an unquoted PowerShell array are equivalent)')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT,
                        help='wall-clock budget per source in seconds (default 30)')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.json and args.format == 'text':
        args.format = 'json'
    try:
        code, output = run(args.format, args.out, args.only, args.timeout)
    except ValueError as exc:
        print(f'configuration error: {redact(str(exc))}', file=sys.stderr)
        return 2
    print(output)
    return code


def run_from_namespace(args):
    """Dispatch entry for `delegator quota` (namespace with format/out/only/timeout/json)."""
    if getattr(args, 'json', False) and args.format == 'text':
        args.format = 'json'
    try:
        code, output = run(args.format, args.out, args.only, args.timeout)
    except (ValueError, OSError) as exc:
        print(f'configuration error: {redact(str(exc))}', file=sys.stderr)
        return 2
    print(output)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
