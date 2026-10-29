# delegator

Единый CLI для явного делегирования задач coding-агентам и Agent Skill для
кодинг-агентов: запуск и ожидание задачи, приёмка результата, resume,
реестр harness/provider/model, алиасы, однопромтовые запросы и мониторинг квот.

Поддерживаемые харнессы: Codex CLI, OpenCode, ZCode, Antigravity, Claude Code.

## Возможности

- `exec` — запуск одной задачи у нативного воркера с ожиданием и приёмкой;
  resume — явный `--resume-session`. Автоматические повторы — только для
  классифицированных capacity-сбоев (backoff), алиас перебирает маршруты по
  цепочке на capacity/quota; иных повторов нет. Управление оболочкой —
  `--shell none|auto|full`.
- `ask` — однопромтовый запуск без репозитория (временный каталог вне Git,
  review/shell `none`, без ретраев, ответ обычным текстом, `--json` — полный JSON).
- `test` — smoke-проверка настройки моделей: офлайн-валидация + один live-запрос.
- `quota` — read-only мониторинг квот аккаунтов (codex, zcode, claude, agy,
  openrouter, commandcode, opencode-go): текст, JSON-снапшот или статический
  HTML-дашборд. Источники без локальных креденшалов пропускаются автоматически,
  `--only` форсирует.
- Реестр (`list`, `add`, `on`/`off`, `delete`, `sync`, `hide`/`unhide`) —
  единственный источник выбора harness/provider/model; `alias` — алиасы с
  упорядоченными маршрутами.

## Требования

- Python 3.10+ (только стандартная библиотека); для харнесса ZCode дополнительно
  нужны Node.js (расшифровка Coding Plan ключа и desktop-bundle `zcode.cjs`)
  и установленный ZCode.
- Установленный и авторизованный CLI соответствующего харнесса: `codex`,
  OpenCode, ZCode, `agy` или `claude`.

## Установка

```powershell
npx skills add doigu/delegator -g -y
```

По умолчанию `skills` хранит одну каноническую копию и создаёт ссылки в
каталогах агентов. Не используйте `--copy`, если ссылки работают.
После установки перезапустите открытых агентов.

## Обновление

Повторите команду установки. Удаление:

```powershell
npx skills remove delegator -g
```

## Состав

- `skills/delegator/SKILL.md` — инструкции скилла: обычный цикл делегирования
  (запуск, ожидание, приёмка, resume) с картой по кейсам.
- `skills/delegator/references/` — редкие кейсы: специфика харнессов и таблица
  shell-уровней (`harnesses.md`), долгие прогоны и ожидание (`long-runs.md`),
  `ask`/`test` (`ask-test.md`), реестр и установка команды PowerShell
  (`registry.md`), квоты (`quota.md`), контракт результата (`result.md`).
- `skills/delegator/scripts/` — CLI (`delegator.py`), адаптеры харнессов,
  источники квот, установщик PowerShell.
- `skills/delegator/tests/` — офлайн-тесты (pytest).
- `delegator.example.json` — пример конфигурации с алиасами.

## Проверка

Офлайн-тесты (нативные модели и аккаунты не требуются; `--basetemp` — вне
репозитория, родительский каталог нужно создать заранее):

```powershell
python -m pytest skills/delegator/tests -q -p no:cacheprovider --basetemp=<каталог вне репозитория>
```

## Лицензия

[MIT](LICENSE)
