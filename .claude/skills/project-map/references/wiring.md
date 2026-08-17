# Проводка: чем что запускается и где регистрируется

Снимок от 2026-08-16. Всё ниже проверено чтением кода; если правишь что-то из
этого — перечитай исходный файл, а не этот текст.

## Точки входа

| запуск | файл | что делает |
|---|---|---|
| `python main.py` | `main.py::main` | боевой запуск: `ApplicationBuilder()` → `post_init` → `run_polling(allowed_updates=_ALLOWED_UPDATES)` |
| `python bot.py` | `bot.py` | 11 строк, зовёт `main.main`; нужен хостингам, которые ищут файл `bot.py` |
| `python preflight.py` | `preflight.py::main` | предполётные проверки; первым делом уводит базу во временную папку |
| `python reset_db.py` | `reset_db.py::main` | стирает пользовательские таблицы; при `KEEP_SETTINGS = False` сотрёт и `settings` |
| `python watchdog_local.py` | `watchdog_local.py::main` | сторож; из кода бота НЕ импортируется (тянет только `config`) — это его свойство, а не недосмотр |
| `python quiz/add.py` | `quiz/add.py::main` | отдельный скрипт наполнения банка вопросов; ботом не вызывается |

## Жизненный цикл процесса (`main.py`)

- `post_init` — выставляет меню команд Telegram (публичное + отдельное для
  персонала), поднимает **6 фоновых задач** в `application.bot_data["background_tasks"]`,
  убирает устаревшее уведомление об обновлении, шлёт админам «Бот запущен»
  и открывает каждому его панель `/adm`.
- `post_stop` — сообщение об остановке, пока бот ещё «живой».
- `post_shutdown` — закрытие базы. Порядок важен: закрытие идёт ПОСЛЕ
  остановки фоновых задач.
- `_error_handler` — единый обработчик ошибок (`application.add_error_handler`).
- `_ALLOWED_UPDATES` — все типы обновлений, кроме реакций. Список строится
  вычитанием из `Update.ALL_TYPES`, а не перечислением.
- До сборки приложения: `_write_pid`, `_kill_existing_instance`,
  `_seed_db_if_missing`, `_restart_self`.

## Фоновые задачи — запускаются в `main.post_init`

Все шесть создаются как `asyncio.create_task(...)`; их имена приходят из
`jobs/__init__.py` (re-export). Задача, не вписанная туда, роняет старт.

| задача | файл |
|---|---|
| `news_polling_loop` | `jobs/news.py` |
| `cleanup_loop` | `jobs/cleanup.py` |
| `rag_catchup_loop` | `jobs/rag.py` |
| `daily_report_loop` | `jobs/reports.py` |
| `watchdog_loop` | `jobs/watchdog.py` |
| `auto_update_loop` | `jobs/update.py` |

`jobs/reports.py` отдаёт наружу ещё `weekly_group_digest` и `nightly_backup`;
`jobs/update.py` — `forget_update_notice`.

## Регистрация обработчиков — только `handlers/__init__.py::setup_handlers`

Второго места регистрации в проекте нет. `preflight.py::check_handlers`
считает зарегистрированные обработчики и группы (на 2026-08-17 — **39 в 4
группах**).

**Группа 0 (по умолчанию)** — команды и основной разбор сообщений:

- `CommandHandler`: `start`, `help`, `clear`, `subscribe`, `unsubscribe`,
  `prompt_set`, `prompt_add`, `prompt_reset`, `news_prompt_set`,
  `news_prompt_reset`, `rag_prompt_set`, `rag_prompt_reset`,
  `proactive_prompt_set`, `proactive_prompt_reset`,
  `author_prompt_set`, `author_prompt_reset`,
  `adm`, `stats`, `mod`, `rag`, `unmute`, `users`,
  `quizadm`, `imagine` (`block=False`), `rank`, `ttx` (`block=False`)
- `InlineQueryHandler(inline_ttx, block=False)`
- `CallbackQueryHandler(handle_callback_query)` — **все** кнопки идут сюда
- `PollAnswerHandler(handle_poll_answer)`
- `MessageHandler`: документы в личке (`handle_kb_document`), `PHOTO`,
  `VOICE|AUDIO`, `VIDEO`, `TEXT & ~COMMAND` (`handle_message`),
  `COMMAND` (`handle_unknown_command`)
- `ChatMemberHandler(on_chat_member, CHAT_MEMBER)`

**Группа 1** — `collect_group_message`: архив сообщений группы.
Фильтр собран из `TEXT|PHOTO|VOICE|AUDIO|VIDEO` — стикеры, кружочки и гифки
в него не попадают.

**Группа −1** — `log_incoming_command`: пишет в лог каждую входящую команду.

**Группа −2** — `_note_chat_activity`: отметка «в чатах кто-то пишет»
(`filters.ALL`). Внутри — одно присваивание; её читает самообновление.

⚠️ Команда `/quiz` (`handlers/quiz.py::cmd_quiz`) в проекте объявлена, но
в `setup_handlers` НЕ зарегистрирована.

## Кнопки

Единственный роутер — `handlers/admin/router.py::handle_callback_query`.
`preflight.py::check_callbacks` сверяет кнопки, найденные в коде панелей,
с ветками роутера (на 2026-08-17 — **219 кнопок, 38 точных
веток + 13 по приставке**).

Ограничение Telegram: `callback_data` ≤ 64 байта. Права на нажатие
проверяются через `services/roles.py` (`perm_for_callback`, `may_press`).

⚠️ Известная слепая зона проверки: если `callback_data` собрана f-строкой,
у которой переменная стоит В НАЧАЛЕ (`f"{имя}_reset"`), проверка её не
видит — постоянного начала у строки нет. Заметно только по счётчику
«кнопок проверено». Собирай `callback_data` с постоянной приставки впереди.

## База данных

Один файл — `database/history.py` (2792 строки, 112 публичных функций),
одно соединение на процесс, доступ под общим замком `_lock`.

Схема создаётся в `_create_schema`; на 2026-08-16 — **24 таблицы**:

```
api_calls, bot_sent_messages, group_messages, join_log, knowledge_log,
known_chats, messages, moderation_log, mute_evidence, news_subscriptions,
proactive_log, quiz_bank, quiz_failed, quiz_stats, sent_news, settings,
staff, staff_log, stats_snapshots, user_context, user_dossier,
user_image_calls, user_settings, user_token_usage
```

`reset_db.py::USER_TABLES` перечисляет **23** из них — без `settings`
(она добавляется отдельно, когда `KEEP_SETTINGS = False`).
`preflight.py::check_tables` сверяет эти два списка.

Таблица `settings` хранит **только строки** — сравнение без `int()` даёт
тихую ошибку. Кэш персональных настроек — `services/user_settings.py`,
он грузится в `main` при старте; читать настройки мимо него значит читать
устаревшее.

## Конфигурация

`config.py` (1061 строка, 106 констант) — читают 34 модуля. Значения
берутся из `.env` (`python-dotenv`). Ключи из `.env.example`:

```
TELEGRAM_TOKEN, GEMINI_API_KEY, GEMINI_IMAGEN_API_KEY, QWEN_API_KEY,
DEEPSEEK_API_KEY, XIAOMI_API_KEY, RAG_ENABLED, RAG_TOP_K,
RAG_MIN_SIMILARITY, RAG_CHUNK_MODE, RAG_LEX_BOOST, RAG_PEAK_MARGIN,
RAG_STRONG_SIM, WATCHDOG_URL
```

Заметные константы: `ADMIN_IDS` (список id владельцев, зашит в код),
`DB_PATH = "history.db"`, `BACKUP_DIR = "backups"`,
`RAG_INDEX_FILE = "knowledge/knowledge_base_vectors.json"`,
`KNOWLEDGE_PENDING_DIR`, `KNOWLEDGE_APPROVED_DIR`,
`AVAILABLE_MODELS` (12 моделей), `PROVIDERS` (4: deepseek, gemini, qwen,
xiaomi), `AVAILABLE_IMAGE_MODELS` (2), `QUIZ_RANKS` (20 званий),
`AUTO_UPDATE_INTERVAL_SEC = 300`, `AUTO_UPDATE_QUIET_SEC = 60`,
`WATCHDOG_PING_SEC = 60`.

`preflight.py::check_models` сверяет `AVAILABLE_MODELS` с раскладкой кнопок
в `handlers/admin/panel_main.py` — это место расходится чаще прочих.

## Сеть

Все запросы к внешним сервисам идут через `services/http.py::session()`.
Сессия своя на каждый поток — это намеренно: запросы к моделям уходят из
рабочих потоков, а `requests.Session` потокобезопасность не обещает.
Единственное исключение — `watchdog_local.py`: он специально не зависит от
кода бота и ходит через `requests` напрямую.

## Выкатка

- `deploy.sh` (на сервере): `git fetch` → `git merge --ff-only origin/main`
  → `compileall -q -f` → `preflight.py`. Любая осечка → `git reset --hard`
  на прежний коммит. Отдельно проверяет, менялся ли `requirements.txt`.
- `.github/workflows/preflight.yml`: те же две проверки на каждый
  pull request и на push в `main`, Python 3.12 (как на сервере).
- `jobs/update.py::auto_update_loop`: раз в 5 минут спрашивает GitHub про
  новый код и забирает его — но только если в чатах тихо ≥60 секунд.
  **Следствие: код, попавший в `main`, оказывается на боевом сервере сам,
  без участия человека.**
- `VERSION` руками не правится — номер поднимает `.bat` при отправке.
