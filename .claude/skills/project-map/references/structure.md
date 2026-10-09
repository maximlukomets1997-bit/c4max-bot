# Структура: файл → что в нём объявлено

Снимок от 2026-10-07 по коду версии v5.55: числа «строк» и «тянут»
пересобраны скриптом, публичные имена сверены с кодом. Пересобрать:

```
python .claude/skills/project-map/scripts/map.py
python .claude/skills/project-map/scripts/map.py --module services/rag.py
```

«Тянут» — сколько модулей проекта импортируют этот файл (считая импорты
внутри функций и относительные). «Публичные имена» — функции и классы
верхнего уровня без подчёркивания в начале, то есть то, чем файл торгует
наружу. Имена с подчёркиванием тоже иногда зовут снаружи — проверяй
скриптом `impact.py`, а не этим списком.

Всего файлов с кодом: **82** (было 67 на 30.08.2026): двенадцать файлов,
на которые 02.09 разрезали `database/history.py`, плюс `jobs/balance.py`
(сверка остатка с платформой, 14.09.2026), `services/group_guard.py`
(заслон чужих групп, 15.09.2026) и `services/dialog_log.py` (дословный лог
прямых обращений, 21.09.2026). Отдельных тестовых
файлов (`test_*.py`, `pytest`) по-прежнему **0** — но с 28.08.2026 есть
`selftest.py`:
проверки поведения в том же стиле, что `preflight.py`, без сторонних
библиотек. Он не заменяет ручной прогон (проверяет функции и отдельные пути,
а не бота целиком), но впервые отвечает на вопрос «правильно ли считает».

## Корень

| файл | строк | тянут | публичные имена |
|---|---:|---:|---|
| `bot.py` | 11 | 0 | — (только вызывает `main.main`) |
| `main.py` | 627 | 1 | `post_init`, `post_stop`, `post_shutdown`, `main` |
| `config.py` | 1681 | 48 | `read_build_mark` + 132 константы верхнего уровня (заводские тексты пяти промптов «личности» — пустые строки; у трёх заданий разборщику вложений, `MEDIA_PROMPT_VOICE`, `MEDIA_PROMPT_PHOTO`, `MEDIA_PROMPT_VIDEO`, заводской текст непустой) |
| `utils.py` | 301 | 22 | `should_respond_in_group`, `clean_mention`, `keep_chat_action`, `delete_user_message_safe`, `mention`, `schedule_delete`, `restore_pending_deletes`, `cancel_pending_deletes`, `register_and_clean_bot_message`. Самоудаление с 03.10.2026 переживает перезапуск: у таймера есть заметка в базе (`pending_deletes`), запуск её подхватывает, остановка отменяет таймеры, а не бросает |
| `utils_format.py` | 312 | 11 | `strip_thoughts`, `thoughts_enabled`, `build_text_and_entities`, `send_formatted`, `convert_md`, `fits_caption`, `reply_md` |
| `logging_setup.py` | 258 | 4 | `archive_old_logs`, `setup_logging` |
| `preflight.py` | 729 | 0 | `check_imports`, `check_models`, `check_providers`, `check_tables`, `check_ranks`, `check_callbacks`, `check_panels`, `check_handlers`, `check_web`, `main` |
| `selftest.py` | 9301 | 0 | проверки ПОВЕДЕНИЯ (28.08.2026), **51 группа** — перечислять их здесь перестали 08.09.2026: список рос вчетверо быстрее, чем его переписывали, и врал уже на четырнадцать имён. Живой список отдаёт сам файл — кортеж `CHECKS` в его конце, где рядом с каждой проверкой стоит её человеческое название; что каждая ловит, а что нет — `references/checks.md`. Отвечает на «правильно ли считает», тогда как `preflight.py` — на «запустится ли». Зовётся из `deploy.sh` и CI, красный откатывает выкатку |
| `reset_db.py` | 81 | 0 | `main` |
| `watchdog_local.py` | 297 | 0 | `main` |

## `database/`

✅ **РАЗРЕЗ ЗАВЕРШЁН 02.09.2026 (решение Максима).** Был один файл
`history.py` на 2804 строки и 122 имени — самый нужный в проекте, его тянут
40 модулей. Стал пакет из двенадцати файлов плюс ОГЛАВЛЕНИЕ. Резать оказалось
безопасно потому, что это была не «одна тема», а дюжина независимых: между
собой они звали друг друга ВСЕГО ТРИ РАЗА, а остальные 103 связи вели к замку
и соединению. Тем же приёмом и по той же причине разошлись `admin.py` (2140
строк, 07.2026) и `jobs.py` (837 строк, 08.2026).

⚠️ **СНАРУЖИ НЕ ИЗМЕНИЛОСЬ НИЧЕГО, и это было условием работы.** `from
database.history import add_messages` работает как работал; так же работают
`hist._lock` и `hist._get_connection()` (их зовёт `selftest.py`: 55 мест
на 07.10.2026). Сорок зависимых модулей не правились ни разу. Каждый из
одиннадцати шагов сверялся разбором кода до и после: «столько-то функций
было, столько же стало, изменившихся тел — ноль».

За весь разрез из того, что файл отдавал наружу, ушло 12 имён — и ни одного
из них не брал снаружи никто (проверено поиском): это импортированные модули
(`os`, `json`, `time`, `sqlite3`, `threading`, `logging`), объект `logger`,
константы из config (`DB_PATH`, `QUIZ_RANKS`, `PROVIDERS`,
`MAX_CONTEXT_MESSAGES`) и переменная соединения `_conn`, скрытая намеренно.

⚠️ **ЕДИНСТВЕННАЯ СВЯЗЬ МЕЖДУ ЧАСТЯМИ:** `chat.py` и `groups.py` читают
настройки из `settings.py`. Ради этого порядок шагов и менялся на ходу —
иначе получилось бы кольцо. Всё остальное зависит только от `_core.py`.

| файл | строк | тянут | публичные имена |
|---|---:|---:|---|
| `database/__init__.py` | 2 | 0 | — |
| `database/_core.py` | 241 | 12 | `close_db`, `backup_to` + внутренние `_lock`, `_get_connection`, `_LoggingConnection`. ⚠️ Путь к базе берётся из `config` В МОМЕНТ открытия соединения, а не снимком в шапке: обе проверки уводят базу во временную папку, и со снимком они писали бы в боевую `history.db` |
| `database/_schema.py` | 465 | 1 | `init_db` + внутренние `_create_schema`, `_COLUMN_MIGRATIONS`, `_run_column_migrations`, `_seed_once`. ⚠️ Схему по темам НЕ дробить: «какие вообще есть таблицы» должно читаться в одном месте, иначе правило «новая таблица — дописать в `reset_db.py::USER_TABLES`» станет невыполнимым на глаз |
| `database/news.py` | 102 | 1 | `subscribe_chat`, `unsubscribe_chat`, `is_chat_subscribed`, `get_subscribed_chats`, `is_news_already_sent`, `mark_news_as_sent`, `count_sent_news_between`. Две таблицы, путать не надо: `news_subscriptions` — КУДА слать, `sent_news` — ЧТО уже слали |
| `database/stats.py` | 205 | 1 | `get_bot_stats`, `save_stats_snapshot`, `get_last_stats_snapshot`, `count_api_calls_between`, `delete_old_stats_snapshots` + `_kyiv_today_start_utc`. ⚠️ Денег здесь НЕ считают — только достают; арифметика расхода в `services/daily_report.py`, её проверяет `selftest`. ⚠️ Напрямую `selftest` отсюда ничего не зовёт; косвенно доходит до трёх функций: цикл чистки (`check_cleanup`, 05.10.2026) — до `delete_old_stats_snapshots`, `get_bot_stats` (итоги месяца) и `get_last_stats_snapshot` (перенос в `daily_report.note_monthly_reset`), сводка сайта (`check_web_pages`) — до `get_bot_stats` и `get_last_stats_snapshot`. Результат сверяется только у чистки снимков; `save_stats_snapshot` и `count_api_calls_between` не зовёт никто — чтение счётчиков для отчёта и панелей проверяется руками |
| `database/moderation.py` | 192 | 1 | `log_moderation_action`, `save_mute_evidence`, `get_moderation_counts`, `get_recent_moderation_actions`, `get_moderation_entry`, `get_mute_evidence`, `delete_old_moderation_log`, `clear_moderation_log`. ⚠️ ЗДЕСЬ ЧУЖАЯ ПЕРЕПИСКА — улики это дословные сообщения людей, и с 01.09.2026 их показывает сайт. ⚠️ Улики умирают вместе с журналом, обе чистки делают это явно. ⚠️ `get_moderation_counts` читает `selftest` ИСХОДНЫМ ТЕКСТОМ (обходом всего пакета `database/`) — сверяет виды записей со сводкой |
| `database/journals.py` | 356 | 1 | четыре журнала: персонал (`log_staff_action`, `get_recent_staff_actions`, `count_staff_actions`, `delete_old_staff_log`, `clear_staff_log`), база знаний (`add_kb_action`, `get_recent_kb_actions`, `delete_old_kb_log`, `clear_kb_log`), проактив (`log_proactive_check`, `proactive_stats`, `proactive_by_chat`, `proactive_by_day`, `delete_old_proactive_log`), вступления (`log_join`, `get_join_counts`, `delete_old_join_log`). ⚠️ Журнал НАКАЗАНИЙ сюда не входит — он в `moderation.py`, к нему привязаны улики. ⚠️ Время у персонала, базы знаний и вступлений — `time.time()`, а у проактива (`proactive_log`) — строка UTC, как в архиве групп и вызовах API; сравнивать одно с другим нельзя. ⚠️ Сроки хранения — в `config.py`, не тут |
| `database/settings.py` | 226 | 3 | `get_setting`, `set_setting`, `delete_setting` + запись дополнений `append_prompt_addition` и восемь читалок промптов (`get_active_system_prompt`, `get_news_system_prompt`, `get_rag_instruction`, `get_proactive_instruction`, `get_author_brief_instruction` и с 21.09.2026 три задания разборщику вложений — `get_media_prompt_voice`, `get_media_prompt_photo`, `get_media_prompt_video`: пусто в базе — отдают заводской текст из `config`). ⚠️ Хранит ТОЛЬКО строки — забыл `int()`, получил тихую ошибку сравнения. ⚠️ У `get_setting` есть ПОБОЧНОЕ ДЕЙСТВИЕ: для `active_model` и `active_image_model` она сама сбрасывает настройку в заводскую, если модели больше нет в списке, — читалка пишет в базу. Пределы и шаги простых настроек — в `services/settings_spec.py`, не тут |
| `database/groups.py` | 295 | 1 | `save_group_message`, `update_last_group_message_text`, `set_proactive_reset_mark`, `get_recent_group_messages`, `delete_old_group_messages`, `remember_chat`, `get_known_chats`, `is_known_chat`, `forget_chat`, `get_group_messages_between`. Через файл проходит КАЖДОЕ сообщение группы. ⚠️ Время — строка UTC, а не `time.time()` как в журналах персонала, базы знаний и вступлений (у журнала проактива тоже строка); исключение `known_chats.last_seen`. ⚠️ `known_chats` с 15.09.2026 — список СВОИХ групп: новую строку кладёт только `services/group_guard.py`, группы без строки бот не слушает вовсе. ⚠️ Проверками архив покрыт в трёх местах: список своих групп (`remember_chat`, `is_known_chat`, `forget_chat`) гоняет проверка «чужие группы», запись разбора вложения и сборку стенограммы — проверка «кому файл в группе» (30.09.2026), чистку по сроку (`delete_old_group_messages`) — проверка «чистка и сброс» (05.10.2026); остальное (дайджест, список групп в карточке) — только руками. ⚠️ `update_last_group_message_text` с 30.09.2026 пишет разбор в ПОСЛЕДНЮЮ запись человека С ВЛОЖЕНИЕМ: прежняя «просто последняя» затирала вопрос, написанный следом за фото |
| `database/people.py` | 307 | 1 | личные дела (`dossier_add_message`, `dossier_add_mute`, `dossier_add_linkdel`, `dossier_reset_violations`, `get_dossier`), список людей (`list_known_users`), персональные настройки (`get_user_settings`, `get_all_user_settings`, `set_user_settings`, `clear_user_settings`), персонал (`get_all_staff`, `get_staff`, `add_staff`, `remove_staff`, `set_staff_perm`). ⚠️ ВЛАДЕЛЬЦЕВ здесь нет — они в `config.ADMIN_IDS`. ⚠️ Два белых списка колонок — это ЗАЩИТА: имя колонки вклеивается в текст запроса, и они молча отбрасывают чужое (проверено подстановкой). ⚠️ Кэш живёт не здесь, а в `services/user_settings.py` и `services/roles.py` — запись мимо них бот не заметит до перезапуска |
| `database/chat.py` | 301 | 1 | `get_history`, `get_history_length`, `get_user_usage`, `add_messages`, `add_bot_message`, `clear_history` + гигиена панелей (`register_bot_message`, `get_old_bot_messages`, `remove_bot_message`) + заметки самоудаления (`add_pending_delete`, `remove_pending_delete`, `list_pending_deletes` — пишет и читает только `utils.py`, 03.10.2026). САМЫЙ ГОРЯЧИЙ ПУТЬ: `get_history` зовётся на каждый ответ модели. ⚠️ Контекст берётся ПО ЧЕЛОВЕКУ, а не по чату — личка и группы вместе, поэтому и `/clear` стирает переписку целиком. ⚠️ `MAX_CONTEXT_MESSAGES` в шапке, а не внутри функции: он стоит значением по умолчанию в сигнатуре. ⚠️ `/clear` НЕ обнуляет накопленный расход токенов — так задумано |
| `database/money.py` | 359 | 2 | `add_provider_cost`, `spend_qwen_tokens`, `get_qwen_tokens`, `register_api_call`, `clear_api_calls`, `clear_user_token_usage`, `register_image_call`, `unregister_image_call`, `get_remaining_image_calls`, `sync_provider_balance`, `plan_balance_sync`, `balance_sync_key`. ⚠️ ОДНА функция на всех провайдеров — имена ключей из реестра `config.PROVIDERS`, новый провайдер добавляется в реестр, а не сюда. ⚠️ Попытка картинки списывается АВАНСОМ и возвращается, если картинка не вышла. ⚠️ Здесь только копилки; арифметика цены — в `services/gemini.py`, и покрыта она, а копилки — нет. Исключений четыре: `plan_balance_sync` (14.09.2026) — развилка сверки остатка вынесена в функцию БЕЗ базы ровно затем, чтобы её мог проверять `selftest`; и `spend_qwen_tokens` (19.09.2026) — возвращает пару «было → стало», по ней `gemini._spend_qwen_quota` пишет владельцу о квоте, и эту пару сверяет группа «квота Qwen»; и с 05.10.2026 месячные `clear_api_calls` и `clear_user_token_usage` — их гоняет группа «чистка и сброс» |
| `database/quiz.py` | 464 | 1 | банк вопросов (`add_quiz_question`, `get_random_quiz_question`, `list_all_quiz_questions`, `update_quiz_question_body`, `set_quiz_question_approved` — с 04.10.2026 он же ставит вопрос в очередь: при попадании в игру счётчик показов поднимается до «самый малый минус один», `delete_quiz_question`, `get_quiz_bank_counts`), счёт и звания (`add_quiz_attempt`, `get_user_stats`, `set_quiz_stats`, `get_all_quiz_stats`, `reset_all_quiz_stats`), чистки и выборки банка (`list_quiz_questions`, `get_quiz_question`, `delete_quiz_drafts`, `delete_all_quiz_questions`), покрытие статей (`note_quiz_question_asked`, `get_quiz_articles_covered`). ⚠️ Варианты ответа хранятся СТРОКОЙ JSON — разбор спрятан в `_row_to_question`, наружу всегда уходит готовый список |
| `database/history.py` | 126 | 43 | **ОГЛАВЛЕНИЕ, кода нет.** Только re-export: собирает имена из двенадцати файлов пакета и отдаёт наружу, как `jobs/__init__.py`. ⚠️ Новое имя, не вписанное сюда, снаружи не видно |

Полный список — `python -c "import ast;print([n.name for n in ast.parse(open('database/history.py',encoding='utf-8').read()).body if hasattr(n,'name')])"`.

## `handlers/` — всё, что отвечает на действия пользователя

| файл | строк | тянут | публичные имена |
|---|---:|---:|---|
| `handlers/__init__.py` | 135 | 3 | `setup_handlers` — единственное место регистрации обработчиков. Первой (группа −3) стоит пара заслона чужих групп: событие «мой статус» и за ним сам заслон — порядок внутри пары важен |
| `handlers/commands.py` | 419 | 4 | `public_commands`, `bot_display_name`, `cmd_start`, `cmd_help`, `cmd_clear`, `cmd_subscribe`, `cmd_unsubscribe`, `handle_menu_callback`, `log_incoming_command`, `handle_unknown_command` |
| `handlers/messages.py` | 703 | 2 | `handle_photo`, `handle_voice`, `handle_video`, `handle_message`, `collect_group_message`. С 08.10.2026 `handle_message` и `handle_photo`, с 09.10.2026 и `handle_voice`, `handle_video` ведут ответ «на глазах» (`services/live_answer.py`) через общие `_start_live`, `_stop_live`, `_deliver`, `_reply_error`; с 09.10.2026 текст идёт через склейку (`services/message_batch.py`): `handle_message` кладёт сообщение в пачку, ответ — `_answer_text` на всю пачку (на последнее сообщение; замучен за флуд, пока ждали, — молчит; удалённое модерацией выкидывается из пачки, удалено всё — молчит); у голосового и видео первая стадия — «🎧 Слушаю голосовое…» / «🎬 Смотрю видео…», отдельный статус с удалением остался только при выключенном выключателе; в личке черновик, в группе — сообщение, которое правится по ходу и последней правкой становится ответом; вложение не скачалось — в группе показ становится сообщением об ошибке; второй, кто его тянет, — `selftest` (гоняет настоящий обработчик) |
| `handlers/media.py` | 93 | 1 | `cmd_imagine` |
| `handlers/quiz.py` | 429 | 5 | `send_quiz_question`, `cmd_rank`, `send_rank_panel`, `handle_poll_answer` |
| `handlers/tech.py` | 486 | 3 | `cmd_ttx`, `catalog_text`, `catalog_keyboard`, `handle_ttx_callback`, `inline_ttx` |

## `handlers/admin/` — админ-панели

| файл | строк | тянут | публичные имена |
|---|---:|---:|---|
| `handlers/admin/__init__.py` | 46 | 4 | только re-export; новое имя, не вписанное сюда, роняет старт бота |
| `handlers/admin/common.py` | 611 | 19 | публичных нет — всё через имена с подчёркиванием (`_onoff`, `_require`, `_send_panel_message`, `_adm_back_row` и др.), но тянут его 19 модулей |
| `handlers/admin/router.py` | 1018 | 2 | `handle_callback_query` — единственный роутер всех кнопок |
| `handlers/admin/panel_main.py` | 590 | 5 | `send_stats_panel`, `send_api_panel`, `send_daily_report_panel`, `send_weekly_report_panel`, `cmd_stats`, `build_adm_keyboard`, `send_adm_panel`, `cmd_adm` |
| `handlers/admin/panel_prompts.py` | 1498 | 6 | `send_prompt_files`, `send_prompts_panel`, `handle_prompt_reset`, `cmd_prompt_set/add/reset`, `cmd_news_prompt_set/reset`, `cmd_rag_prompt_set/reset`, `cmd_author_prompt_set/reset`, `cmd_proactive_prompt_set/reset`, `cmd_voice_prompt_set/reset`, `cmd_photo_prompt_set/reset`, `cmd_video_prompt_set/reset` (три задания разборщику вложений, 21.09.2026) |
| `handlers/admin/panel_users.py` | 1677 | 9 | `send_users_panel`, `send_user_card`, `cmd_users`, `handle_quiz_score_input`, `target_name_with_nick` (имя человека с ником — одна подпись на карточку, журнал наказаний и письмо о муте от бота) + правила счёта викторины, общие с сайтом (`_set_quiz_score`, `fix_quiz_misses`, `quiz_score_summary`, `_QUIZ_FIELDS`, `_QUIZ_SCORE_MAX`) |
| `handlers/admin/panel_rag.py` | 1007 | 7 | `send_rag_panel`, `cmd_rag`, `handle_kb_document`, `handle_kb_test_query` (панель из трёх экранов: разделы → список раздела → настройки поиска) |
| `handlers/admin/panel_mod.py` | 587 | 5 | `send_mod_panel`, `cmd_mod`, `cmd_unmute`, `MOD_ACTION_TITLES` и `MOD_ACTIONS_WITH_EVIDENCE` — названия видов записей журнала и список тех, у кого бывают улики; их же читает страница журналов на сайте |
| `handlers/admin/panel_quiz.py` | 536 | 4 | `send_quiz_panel`, `cmd_quiz_admin`; экран «📋 Статьи без вопросов» — `_build_noq_screen` (его собирает и `preflight`). Машинной сборки здесь больше нет (04.10.2026) |
| `handlers/admin/panel_balance.py` | 527 | 7 | `send_balance_panel`, `handle_balance_input` |
| `handlers/admin/panel_updates.py` | 178 | 5 | `send_updates_panel` |
| `handlers/admin/panel_digest.py` | 179 | 2 | `digest_keyboard`, `send_digest_panel` |

## `services/` — логика, не привязанная к экрану

| файл | строк | тянут | публичные имена |
|---|---:|---:|---|
| `services/gemini.py` | 3765 | 11 | `compress_newlines`, `thinking_level`, `ask_gemini`, `ask_gemini_audio`, `ask_gemini_video`, `format_news_as_colonel`, `author_brief`, `ask_group_proactive`, `ask_group_proactive_media`, `generate_image`, `fetch_deepseek_balance` (остаток на счету у самой платформы, 14.09.2026) |
| `services/antispam.py` | 954 | 10 | `is_enabled`, `get_thresholds`, `get_thresholds_for`, `trust_info`, `check_and_mute`, `unmute`, `mute_user`, `kick_user`, `ban_user`, `unban_user`, `notify_owners_ai_mute`, `is_linkfilter_enabled`, `check_and_delete_links`, `linkfilter_window_text`, `is_muted_now` и `was_deleted` (склейка, 09.10.2026: замучен ли человек и удалила ли модерация сообщение за последние `DELETED_MEMORY_SEC`), `get_mute_stats`, `get_recent_actions`, `get_evidence` |
| `services/daily_report.py` | 836 | 12 | `kyiv_now`, `kyiv_label`, `balance_sync_note` (приписка «сверено в 14:05» к остатку — одна на четыре экрана: три в боте и страница сайта `/system`), `collect_counters`, `period_totals`, `render`, `midnight_report`, `today_so_far`, `weekly_report`, `week_so_far`, `last_report_text`, `last_weekly_text`, плюс метки и границы периодов (`seconds_to_next_hour`, `period_closed`, `week_add_day`, `week_closed`, `start_snapshot_if_needed`, `note_monthly_reset`) |
| `services/rag.py` | 788 | 7 | `RagQuotaError`, `get_embedding`, `parse_article_file`, `is_active`, `sync_knowledge_base`, `index_lag`, `rebuild_knowledge_base`, `normalize_query`, `retrieve_relevant_context`, `test_search` |
| `services/proactive.py` | 860 | 5 | `skip_counts`, `is_enabled`, `hands_enabled`, `note_bot_group_reply`, `forget_conversations`, `consider_message` |
| `services/tech_card.py` | 514 | 2 | `index`, `find_local`, `suggest`, `by_kind`, `by_title`, `token`, `by_token`, `load`, `render_card`, `render_section`, `render_candidates`, `kinds_summary`, `section_label`, `is_specs`, `short_title`, `kind_icon` |
| `services/quiz_daily.py` | 233 | 7 | `is_enabled`, `set_enabled`, `day_key`, `hours_label`, `due_now`, `note_sent`, `active`, `remember`, `forget`, `restore`, `next_run_label` (вопрос дня: расписание, тумблер, память о разосланных опросах) |
| `services/quiz_bank.py` | 344 | 5 | `articles_without_questions`, `stats`, `seed_stats`, `load_seed`, `seed_diff`, `seed_apply` — файл вопросов `quiz/questions.json` и сверка с ним. Машинная сборка (`generate_for_article`, `generate_batch`, `retry_failed`) удалена 04.10.2026: вопросы пишет Claude |
| `services/greeter.py` | 419 | 5 | `is_enabled`, `captcha_enabled`, `kick_enabled`, `timeout_sec`, `on_chat_member`, `handle_join_callback` |
| `services/group_guard.py` | 567 | 3 | 🚪 заслон от чужих групп (15.09.2026): `gate` (группа −3, ПЕРВЫМ среди обработчиков — обновление из группы, которой нет в `known_chats`, дальше не идёт), `on_my_chat_member` (бота добавили/удалили), `ask_owner` и `request_keyboard` (вопрос владельцу с кнопками `grp:stay:`/`grp:leave:`), `handle_group_callback` (кнопки, после гейта прав), `forget_group`. ⚠️ Сбой проверки ПРОПУСКАЕТ, а не запирает. ⚠️ О каких группах уже спросили — в settings `group_guard_pending`, не в памяти; там же номера сообщений с вопросами: при переезде группы в супергруппу кнопки переключаются на новый номер или вопрос снимается (`_carry_migration`). ⚠️ Служебное «бот покинул группу» и чат, где бота нет, вопроса не порождают. ⚠️ Всё, по чему решать уже нечего (итоги кнопок, снятые вопросы, «Меня удалили из группы»), исчезает через `DONE_TTL_SEC` = 5 минут; вопросы, ждущие решения, — никогда. ⚠️ «Состоит ли бот в группе» решает `greeter._is_in` — второго правила не заводить |
| `services/scraper.py` | 339 | 1 | `fetch_latest_news`, `fetch_article` (сайт `https://wtmobile.com/ru/news`) |
| `services/roles.py` | 331 | 16 | `load`, `make_moderator`, `unmake_moderator`, `grant_perm`, `is_owner`, `is_moderator`, `is_staff`, `role_of`, `can`, `has_any_perm`, `perms_of`, `list_moderators`, `can_act_on`, `perm_for_callback`, `may_press` |
| `services/settings_spec.py` | 358 | 10 | ЕДИНЫЙ список простых настроек (тумблер и число): пределы, шаги, начальные значения. `SPEC`, `SECTIONS`, `read`, `display`, `toggle`, `adjust`, `write`, `keys_of`. Читают и панели бота, и сайт — второй копии пределов быть не должно |
| `services/prompts_spec.py` | 186 | 4 | список промптов — девять записей: восемь текстов (пять «личности» и с 21.09.2026 три задания разборщику вложений) плюс дополнения: ключ, название, куда уходит текст, запасная константа. `PROMPTS`, `BY_KEY`, `read`, `has_factory` (вернётся ли заводской текст при очистке поля — по нему страница промптов и лог правки с сайта различают «стёрт насовсем» и «вернулось заводское», 29.09.2026), `write`, `assembled_system_prompt`. Тексты подсказок для Telegram остались в `panel_prompts._PROMPTS`; что оба списка про одно и то же, сверяет `selftest` |
| `services/knowledge_store.py` | 300 | 9 | `save_pending_news`, `read_title`, `detect_kind`, `list_articles`, `read_article`, `approve_article`, `delete_article`, `add_article`, `replace_article` |
| `services/group_digest.py` | 329 | 6 | `is_enabled`, `week_key`, `due_now`, `note_sent`, `collect`, `render`, `build` |
| `services/update_log.py` | 263 | 3 | `available`, `version_of`, `recent`, `stats`, `fmt_time`, `fmt_day`, `fmt_ago` |
| `services/backup.py` | 273 | 7 | `backup_dir`, `human_size`, `make_backup`, `list_backups`, `make_kb_backup`, `kb_caption`, `due_today`, `note_done` |
| `services/deploy.py` | 197 | 5 | `note_activity`, `quiet_for`, `consume_planned_restart` (записка «перезапуск плановый» от отправки с ПК — её читает `main.post_stop`, 08.10.2026), `can_update`, `update`, `describe` |
| `services/user_settings.py` | 148 | 12 | `load`, `refresh`, `get`, `set_field`, `clear`, `thresholds_for`, `is_immune`, `links_allowed`, `ai_ignored`, `image_limit_for`, `honorary_rank` |
| `services/dialog_log.py` | 314 | 4 | 👤 дословный лог ПРЯМЫХ обращений к боту (21.09.2026): `path_for`, `note_ask`, `note_request`, `note_answer`, `note_outcome`, `stats`, `list_records`, `total_size`, `count_records`, `clear`, `clear_all`. Файл НА ЧЕЛОВЕКА в `logs/dialog` — как память бота, которая тоже общая на человека (`database/chat.py::get_history`), а не на чат. Архива нет: от роста держит потолок `MAX_BYTES` (2 МБ), переросшая запись режется сверху. Пишут его `ask_gemini` и `_ask_native_media`, читают экраны «👤 Разговор с ботом» |
| `services/chat_log.py` | 296 | 8 | `archive_path`, `current_path`, `started_label`, `note_check`, `note_media`, `note_request`, `note_answer`, `note_outcome`, `close_session`, `stats` (дословный лог проактивного режима в `logs/chat`) |
| `services/http.py` | 58 | 5 | `session` |
| `services/message_batch.py` | 119 | 4 | `wait_sec`, `submit` — склейка сообщений подряд (09.10.2026): пачка одного человека в одном чате ждёт `wait_sec()` тишины (регулятор `batch_wait_sec`, 0 = выключено; кнопки ➖/➕ «🧩 СКЛЕЙКА» в «📡 Настройки API» и сайт), но не дольше `MAX_WAIT_SEC`, и уходит модели одним вопросом; пришедшее во время ответа — одним следующим, без параллельного. Про Telegram не знает: `on_open`/`on_flush` даёт `handlers/messages.py`. ⚠️ Только текст; состояние в памяти. Проверка — группа `selftest` «склейка сообщений» |
| `services/live_answer.py` | 324 | 2 | `live_answer_enabled`, `LiveDraft`, `LiveGroupMessage` — ответ «на глазах» (08.10.2026). Куски ответа получает из `services/gemini.py` через `_live` (по потоку исполнителя, только основной запрос `ask_gemini`). Личка — черновик Telegram (`send_message_draft`) раз в `TICK_SEC`; группа — сообщение «💭 Думаю…» ответом на вопрос, правка раз в `GROUP_TICK_SEC` (3 с, предел Telegram на группу), последней правкой (`finish`) — готовый ответ с разметкой и мыслями; не вышло — недописанное удаляется, ответ уходит новым. Насос гасится МЯГКО (`stop` будит паузу): отмена посреди первой отправки оставила бы в группе лишнее «Думаю…». ⚠️ Только показ: в память, архив группы и учёт идёт то, что вернула модель; отказ Telegram прекращает обновления до конца ответа. «Сам в разговор» не трогает (решение Максима — без него). Голосовое и видео (09.10.2026): `label` — первая стадия, `thinking()` из `gemini.ask_gemini` переводит на «Думаю»; запасной путь «файл слышащей/зрячей цепочке» показа не даёт. Выключатель один на личку и группы — `live_answer_enabled`: на сайте и кнопкой «✍️ ОТВЕТ НА ГЛАЗАХ» в «📡 Настройки API» (`toggle_live_answer`, код журнала `live_answer`); он же откат без выкатки. Проверка — группа `selftest` «ответ «на глазах»» |
| `services/__init__.py` | 2 | 0 | — |

## `jobs/` — фоновые задачи

| файл | строк | публичные имена |
|---|---:|---|
| `jobs/__init__.py` | 46 | re-export; новая задача, не вписанная сюда, роняет старт `main.py` |
| `jobs/reports.py` | 411 | `daily_report_loop`, `weekly_group_digest`, `daily_quiz`, `nightly_backup` |
| `jobs/cleanup.py` | 234 | `cleanup_loop` |
| `jobs/update.py` | 372 | `forget_update_notice`, `auto_update_loop`, `notice_since`, `notice_expired`, `drop_expired_notice` (срок жизни уведомления об обновлении, 04.09.2026) |
| `jobs/news.py` | 242 | `send_news_to_chat`, `news_polling_loop` |
| `jobs/watchdog.py` | 102 | `watchdog_loop` |
| `jobs/rag.py` | 161 | `send_notice`, `drop_notices`, `rag_catchup_loop` |
| `jobs/balance.py` | 94 | `balance_sync_loop`, `sync_balances_once` — раз в 10 минут (до 15.09.2026 — раз в час) спрашивает у платформы НАСТОЯЩИЙ остаток на счету (14.09.2026). Заведён потому, что собственный счёт бота занижен на оборванных потоках: токены оплачены, а отчёт о них не пришёл. ⚠️ Сверяется только DeepSeek — остаток по своему адресу отдаёт он один; список провайдеров внутри файла, не в реестре |
| `jobs/web.py` | 74 | `web_loop` — поднимает сайт внутри процесса бота |

## `web/` — веб-админка (30.08.2026, этапы 0–5)

⚠️ Перенос НЕ полный. Что осталось только в кнопках бота —
`references/risks.md`, раздел про сайт.

Сайт живёт внутри процесса бота; как он подключён и почему именно так —
`references/wiring.md`, раздел «Веб-админка».

| файл | строк | публичные имена |
|---|---:|---|
| `web/__init__.py` | 23 | re-export `ROUTES`, `build_app` |
| `web/routes.py` | 883 | `ROUTES` — единственный список адресов; `build_app`, `index`, `apply`, `prompts`, `kb`, `quiz`, `journal`, `users`, `user_card`, `system`, `download`, `enter`, `exit_`, `health` |
| `web/auth.py` | 252 | `check_webapp`, `is_allowed`, `make_session`, `read_session`, `make_login_token`, `read_login_token`, `make_login_url`, `csrf_for`, `csrf_ok`, `current_user` |
| `web/pages.py` | 1958 | `esc`, `page_login`, `page_denied`, `page_summary`, `page_prompts`, `page_users`, `page_user_card`, `page_kb`, `page_quiz`, `page_journal`, `page_system`, `plain`, `current_theme` + `THEMES` (две темы оформления), `NAV` (список разделов верхней полосы), `css_version` (отпечаток оформления против кэша браузера) |
| `web/actions.py` | 922 | `ActionError`, `apply_setting`, `apply_prompt`, `apply_model`, `apply_image_model`, `apply_thinking`, `apply_theme`, `user_adjust`, `user_toggle`, `user_reset_settings`, `user_reset_violations`, `user_clear_history`, `user_rank`, `user_quiz_score`, `user_quiz_fix`, `user_role`, `user_perm`, `user_moderate`, `kb_add`, `kb_replace`, `kb_approve`, `kb_delete`, `kb_rebuild`, `kb_test_search`, `kb_clear_log`, `quiz_approve`, `quiz_delete`, `quiz_seed`, `quiz_wipe_drafts`, `quiz_nuke`, `quiz_zero`, `quiz_auto_toggle`, `quiz_reseed`, `balance_set`, `report_text`, `make_backup`, `digest_text`, `digest_send`, `digest_toggle`, `wipe_conversations`, `toggle_personal_prompt`, `clear_moderation_journal`, `clear_staff_journal`, `restart_bot` — правка с сайта делает ВСЁ то же, что нажатие кнопки |
| `web/longjobs.py` | 79 | `is_running`, `last_result`, `forget_result`, `start` — долгие работы с сайта (пересборка базы знаний; сборка вопросов удалена 04.10.2026): запускаются в отдельном потоке, страница опрашивает результат. Из проекта не тянет НИЧЕГО, поэтому в списке зависимостей выглядит одиноко |
| `web/static/style.css` | — | оформление; ни одного адреса со стороны |

## Прочее

| путь | что это |
|---|---|
| `quiz/add.py` | отдельный скрипт (52 строки, `main`), тянет `services.quiz_bank`; из бота не вызывается |
| `quiz/questions.json` | 219 КБ данных, 341 вопрос (сверено 03.10.2026); JSON с отступом в ОДИН пробел и переводами строк LF — и в git, и в рабочей копии; пересобираешь файл скриптом, сверяй байт в байт, иначе получишь разницу на все 4100 строк. ⚠️ `quiz/add.py` под Windows пишет его с CRLF — дописывать своим скриптом в двоичном режиме. Читается при показе панели викторины и страницы `/quiz` (строка «📄 Файл вопросов» — `quiz_bank.seed_stats`/`seed_diff`) и по двум кнопкам: «📥 Новые вопросы в черновики» (`load_seed`; кнопка есть, только когда в файле есть вопросы, которых нет в банке, — с 04.10.2026) и «♻️ Обновить из файла» (`seed_apply`) |
| `knowledge/approved/` | статьи базы знаний (`.md`) — источник RAG и справочника `/ttx`; **в git не едет** (папка принадлежит серверу, 12.08.2026) |
| `knowledge/pending/` | статьи, ждущие одобрения; тоже мимо git |
| `knowledge/knowledge_base_vectors.json` | указатель RAG; строится ботом, в git не едет |
| `history.db`, `seed.db` | боевая база и заготовка |
| `deploy.sh`, `deploy-restart.sh` | выкатка на сервер |
| `.github/workflows/preflight.yml` | проверка в GitHub Actions |
| `.claude/worktrees/` | **копии старых версий проекта, не код**; сейчас папки нет (удалена 16.08.2026), но появляется заново при работе в отдельной копии |
