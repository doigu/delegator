import re
from pathlib import Path

SKILL_DIR = Path(__file__).parents[1]
REPO_ROOT = Path(__file__).parents[3]
SKILL = (SKILL_DIR / 'SKILL.md').read_text(encoding='utf-8')
REFERENCES = sorted((SKILL_DIR / 'references').glob('*.md'))
LINK = re.compile(r'\]\(([^)#\s]+\.md)\)')


def markdown_links(path):
    return [(path.parent / target).resolve()
            for target in LINK.findall(path.read_text(encoding='utf-8'))]


def test_skill_map_mentions_every_reference():
    case_map = SKILL[SKILL.index('## Case map'):]
    for reference in REFERENCES:
        assert f'references/{reference.name}' in case_map, reference.name


def test_skill_reference_links_resolve():
    for target in markdown_links(SKILL_DIR / 'SKILL.md'):
        assert target.is_file(), target


def test_all_markdown_links_resolve():
    files = [SKILL_DIR / 'SKILL.md'] + REFERENCES + [REPO_ROOT / 'README.md']
    for path in files:
        for target in markdown_links(path):
            assert target.is_file(), f'{path.name} -> {target}'


def test_skill_carries_the_ordinary_cycle_sections():
    for heading in ('## Assignment and launch', '## Waiting and reading the result',
                    '## Acceptance', '## Failures, retries, exit codes', '## Resume',
                    '## Case map'):
        assert heading in SKILL, heading


def test_reference_topic_markers_stay_in_their_files():
    markers = {
        'harnesses.md': ('workspace-write', 'acceptEdits', 'app-server --stdio',
                         'stream-json', '--dangerously-skip-permissions',
                         'permission-prompts none'),
        'long-runs.md': ('sleep 3300', 'write_stdin', 'Job Objects'),
        'ask-test.md': ('--skip-git-repo-check', 'Reply with exactly the word: Hi'),
        'registry.md': ('desktop-visible', 'migrate_default_providers.py',
                        'install.ps1'),
        'quota.md': ('daily-cloudcode-pa', 'quota-dashboard.html'),
    }
    by_name = {path.name: path for path in REFERENCES}
    for name, phrases in markers.items():
        assert name in by_name, name
        text = by_name[name].read_text(encoding='utf-8')
        for phrase in phrases:
            assert phrase in text, f'{name}: {phrase}'
