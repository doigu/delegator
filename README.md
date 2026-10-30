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

### 1. Установка скилла

```powershell
npx skills add doigu/delegator -g -y
```

По умолчанию `skills` хранит одну каноническую копию и создаёт ссылки в
каталогах агентов. Не используйте `--copy`, если ссылки работают.
После установки перезапустите открытых агентов.

Обновление — повторите команду установки. Удаление:

```powershell
npx skills remove delegator -g
```

### 2. Установка CLI-лаунчера PowerShell (рекомендуется)

Чтобы запускать команду `delegator` напрямую без явного вызова Python,
установите лаунчер в директорию, входящую в `PATH`:

```powershell
& <skill-dir>/scripts/install.ps1 -BinDir <директория-в-PATH>
```

Инсталлятор создаст:
- `delegator.ps1` — обёртку для запуска CLI через установленный Python;
- `delegator-quota.cmd` — ярлык для двойного клика, который обновляет HTML-дашборд
  квот и открывает его в браузере по умолчанию.

Без лаунчера вызывайте точку входа напрямую:
`python -X utf8 -B <skill-dir>/scripts/delegator.py <команда>`.

## Конфигурация и реестр моделей

Реестр — единственный источник выбора связок `harness/provider/model`. Конфигурация
хранится в `~/.agents/delegator.json` (переопределение: `--config <путь>`, затем
переменная `DELEGATOR_CONFIG`). Хранение вне каталога скилла защищает настройки
от перезаписи при обновлениях.

### Инициализация

Файл `~/.agents/delegator.json` автоматически не создаётся. Перед началом работы
создайте каталог и скопируйте шаблон:

```powershell
New-Item -ItemType Directory -Force ~/.agents
Copy-Item skills/delegator/delegator.example.json ~/.agents/delegator.json
```

При отсутствии файла команды чтения видят пустой реестр, а первая команда записи
(`add` или `sync --add`) создаст его со стандартным блоком `execution`.

### Управление реестром

Команды работают локально и не требуют обращения к LLM:

```powershell
# Добавление модели (с проверкой в локальном каталоге харнесса)
delegator add codex/openai/gpt-6-luna --efforts low medium high --effort low
delegator add zcode/account:zai-start-plan/GLM-5.3 --override

# Просмотр зарегистрированных моделей (--active скрывает отключенные)
delegator list
delegator list --active

# Включение и отключение (включение сбрасывает флаг блокировки квоты провайдера)
delegator off openrouter
delegator on opencode/openrouter

# Удаление записей (--dry-run для безопасной проверки)
delegator delete opencode/openrouter --recursive --dry-run
delegator delete opencode/openrouter --recursive --yes

# Синхронизация с каталогами/кэшами установленных харнессов
delegator sync
delegator sync --add 1,3
delegator sync --prune 1 --yes

# Скрытие моделей или провайдеров из отчёта sync
delegator hide codex/openai/gpt-5.6-terra
delegator hidden
delegator unhide opencode/openrouter

# Алиасы (цепочки моделей с автоматическим перебором при capacity/quota)
delegator alias add deepseek-flash opencode/opencode/deepseek/deepseek-v4.1-flash opencode/commandcode/deepseek/deepseek-v4.1-flash
delegator alias list
delegator alias remove deepseek-flash
```

Селекторы (`on`, `off`, `delete`, `hide`) сопоставляются с полным адресом либо
с суффиксом целого сегмента (например, `/opencode`).

Подробное описание параметров реестра, кэшей и правил синхронизации — в
[registry.md](skills/delegator/references/registry.md).

## Харнессы и адресация

Адрес модели всегда имеет вид `<harness>/<provider>/<model>`.

- **Codex**: `codex/openai/<model>`. Передаёт `model_provider="openai"` явно в CLI.
  Проверка моделей выполняется по `$CODEX_HOME/models_cache.json`.
- **OpenCode**: `opencode/<provider>/<model>`. Использует нативную адресацию
  провайдеров OpenCode.
- **ZCode**: `zcode/<plan-provider>/<model>`. Обязателен флаг `--effort`. Режим
  сессии следует за `--shell` (`plan`/`edit`/`guarded`/`yolo`). Модель вне
  встроенной таблицы регистрируется через `add --override`. Resume выполняется
  через `--resume-session sess_<UUID>` (режим переприкрепляется через `session/setMode`).
- **Antigravity**: `agy/gemini/<model>`. Дефолт `--shell full`
  (`--dangerously-skip-permissions`) в обоих режимах; уровень `auto` запрещён.
  Review выполняется в `plan`, implementation — в `accept-edits`. Resume — явный
  `--resume-session` с UUID диалога.
- **Claude Code**: `claude/anthropic/<полный-id-модели>`. Каталога моделей нет
  (`add` не проверяет вхождение). Флаг `--permission-prompts none` ставится только
  при `--shell none` (на уровнях `auto`/`full` рискованные команды фильтрует
  нативный классификатор). Resume — явный `--resume-session` с UUID.

### Уровни доступа к оболочке (`--shell`)

Команды оболочки (тесты, сборка, скрипты) регулируются флагом `--shell none|auto|full`:
- `none` — нативный режим без выполнения команд терминала (правки файлов в implementation разрешены);
- `auto` — исполнение рутинных команд под контролем нативного гарда (запрет путей вне репозитория);
- `full` — наиболее доверенный режим без подтверждений.

По умолчанию используется `none` для `--mode review` и `auto` для `--mode implementation`
(исключение: у Antigravity дефолт — `full` для обоих режимов).

Специфика харнессов, соответствие сессий и таблица безопасности — в
[harnesses.md](skills/delegator/references/harnesses.md).

## Примеры использования

### Делегирование задач (`exec`)

Команда `exec` блокирует терминал до завершения задачи, выводит прогресс в stderr
и финальный JSON-результат в stdout. В одной рабочей копии Git допускается только
один активный исполнитель (`--repo`).

```powershell
# Реализация по файлу задачи (дефолт shell: auto)
delegator exec -m codex/openai/gpt-6-luna -e low --mode implementation --repo D:\prj\example --prompt-file .tmp\issue-42\task.md

# Ревью diff (дефолт shell: none)
delegator exec -m claude/anthropic/claude-sonnet-5-5 -e high --mode review --repo D:\prj\example -p 'Review the specified diff; report concrete defects only.'

# Запуск по цепочке алиаса (автоматический переход к следующему маршруту при сбое)
delegator exec -m deepseek-flash -e high --mode review --repo D:\prj\example -p 'Проверь изменения...'

# Возобновление сессии (Resume) при неизменных OUTCOME, SCOPE и ORACLE
delegator exec -m codex/openai/gpt-6-luna -e low --mode implementation --repo D:\prj\example --resume-session <session-id> -p 'Continue: устрани замечания тестов.'
```

Коды выхода: `0` — успех, `1` — ошибка/квота/отмена, `2` — ошибка параметров,
`3` — вопрос исполнителя (`needs_input`/`worker_question`), требующий ответа через `--resume-session`.
Детали полей результата — в [result.md](skills/delegator/references/result.md),
длинные фоновые прогоны — в [long-runs.md](skills/delegator/references/long-runs.md).

### Однопромтовые запросы без репозитория (`ask`)

Запуск изолированного запроса во временном каталоге вне Git (shell `none`,
без ретраев, вывод ответа обычным текстом, с `--json` — полный JSON-результат):

```powershell
delegator ask -m codex/openai/gpt-6-luna -p "Объясни назначение декоратора @functools.wraps"
delegator ask -m deepseek-flash -p "Ответь одним словом: тест"
```

### Smoke-проверка конфигурации моделей (`test`)

Офлайн-валидация доступности модели + один проверочный live-запрос:

```powershell
# Проверить все активные модели реестра
delegator test

# Проверить конкретную модель или маршруты алиаса
delegator test -m codex/openai/gpt-6-luna
delegator test -m deepseek-flash
```

Подробнее про `ask` и `test` — в [ask-test.md](skills/delegator/references/ask-test.md).

### Мониторинг квот (`quota`)

Read-only сбор состояния квот аккаунтов без запуска моделей:

```powershell
# Компактная текстовая таблица в консоли
delegator quota

# Опрос конкретных источников с ограничением по времени
delegator quota --only codex,zcode --timeout 20

# Сохранение снапшота в JSON
delegator quota --json

# Создание автономного HTML-дашборда
delegator quota --format html --out D:\tools\quota.html
```

Подробнее про окна квот и планировщик — в [quota.md](skills/delegator/references/quota.md).

## Состав

- [SKILL.md](skills/delegator/SKILL.md) — инструкции скилла: стандартный цикл делегирования
  (запуск, ожидание, приёмка, resume) с картой по кейсам.
- `skills/delegator/references/` — специализированные справочники:
  - [harnesses.md](skills/delegator/references/harnesses.md) — специфика харнессов, соответствие сессий и таблица shell-уровней;
  - [registry.md](skills/delegator/references/registry.md) — реестр, конфиг, алиасы, лаунчер PowerShell;
  - [quota.md](skills/delegator/references/quota.md) — источники квот, окна сброса, планировщик;
  - [ask-test.md](skills/delegator/references/ask-test.md) — однопромтовые запуски и smoke-тесты моделей;
  - [long-runs.md](skills/delegator/references/long-runs.md) — долгие прогоны и фоновое ожидание;
  - [result.md](skills/delegator/references/result.md) — контракт результата, поля JSON и коды выхода.
- `skills/delegator/scripts/` — CLI (`delegator.py`), адаптеры харнессов,
  источники квот, установщик PowerShell.
- `skills/delegator/tests/` — офлайн-тесты (pytest).
- `skills/delegator/delegator.example.json` — пример конфигурации с алиасами.

## Проверка

Офлайн-тесты (нативные модели и аккаунты не требуются; `--basetemp` — вне
репозитория, родительский каталог нужно создать заранее):

```powershell
python -m pytest skills/delegator/tests -q -p no:cacheprovider --basetemp=<каталог вне репозитория>
```

## Лицензия

[MIT](LICENSE)
