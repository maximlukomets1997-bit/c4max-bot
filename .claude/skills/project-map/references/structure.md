# Структура: файл → что в нём объявлено

Снимок от 2026-08-11, собран из кода скриптом. Пересобрать:

```
python .claude/skills/project-map/scripts/map.py
python .claude/skills/project-map/scripts/map.py --module services/rag.py
```

«Тянут» — сколько модулей проекта импортируют этот файл (считая импорты
внутри функций и относительные). «Публичные имена» — функции и классы
верхнего уровня без подчёркивания в начале, то есть то, чем файл торгует
наружу. Имена с подчёркиванием тоже иногда зовут снаружи — проверяй
скриптом `impact.py`, а не этим списком.

Всего файлов с кодом: **55**. Тестовых файлов: **0**.

## Корень

| файл | строк | тянут | публичные имена |
|---|---:|---:|---|
| `bot.py` | 11 | 0 | — (только вызывает `main.main`) |
| `main.py` | 571 | 1 | `post_init`, `post_stop`, `post_shutdown`, `main` |
| `config.py` | 997 | 34 | `read_build_mark` + ~105 констант верхнего уровня |
| `utils.py` | 181 | 19 | `should_respond_in_group`, `clean_mention`, `keep_chat_action`, `delete_user_message_safe`, `mention`, `schedule_delete`, `register_and_clean_bot_message` |
| `utils_format.py` | 304 | 7 | `strip_thoughts`, `thoughts_enabled`, `build_text_and_entities`, `send_formatted`, `convert_md`, `fits_caption`, `reply_md` |
| `logging_setup.py` | 248 | 7 | `archive_old_logs`, `setup_logging` |
| `preflight.py` | 591 | 0 | `check_imports`, `check_models`, `check_providers`, `check_tables`, `check_ranks`, `check_callbacks`, `check_panels`, `check_handlers`, `main` |
| `reset_db.py` | 81 | 0 | `main` |
| `watchdog_local.py` | 297 | 0 | `main` |

## `database/`

| файл | строк | тянут | публичные имена |
|---|---:|---:|---|
| `database/__init__.py` | 2 | 0 | — |
| `database/history.py` | 2711 | 34 | **109 функций**. Ключевые группы: соединение и схема (`init_db`, `close_db`, `backup_to`), переписка (`get_history`, `add_messages`, `clear_history`), настройки (`get_setting`, `set_setting`, `delete_setting`), деньги и счётчики (`add_provider_cost`, `register_api_call`, `get_bot_stats`, `save_stats_snapshot`), модерация (`log_moderation_action`, `save_mute_evidence`, `get_dossier`), персонал и права (`get_all_staff`, `set_staff_perm`, `log_staff_action`), викторина (`add_quiz_question`, `get_random_quiz_question`, `set_quiz_question_approved`), группы (`save_group_message`, `get_recent_group_messages`), журналы (`add_kb_action`, `log_proactive_check`, `log_join`) |

Полный список — `python -c "import ast;print([n.name for n in ast.parse(open('database/history.py',encoding='utf-8').read()).body if hasattr(n,'name')])"`.

## `handlers/` — всё, что отвечает на действия пользователя

| файл | строк | тянут | публичные имена |
|---|---:|---:|---|
| `handlers/__init__.py` | 114 | 2 | `setup_handlers` — единственное место регистрации обработчиков |
| `handlers/commands.py` | 416 | 5 | `public_commands`, `bot_display_name`, `cmd_start`, `cmd_help`, `cmd_clear`, `cmd_subscribe`, `cmd_unsubscribe`, `handle_menu_callback`, `log_incoming_command`, `handle_unknown_command` |
| `handlers/messages.py` | 448 | 1 | `handle_photo`, `handle_voice`, `handle_video`, `handle_message`, `collect_group_message` |
| `handlers/media.py` | 94 | 1 | `cmd_imagine` |
| `handlers/quiz.py` | 326 | 3 | `send_quiz_question`, `cmd_quiz`, `cmd_rank`, `send_rank_panel`, `handle_poll_answer` |
| `handlers/tech.py` | 486 | 3 | `cmd_ttx`, `catalog_text`, `catalog_keyboard`, `handle_ttx_callback`, `inline_ttx` |

## `handlers/admin/` — админ-панели

| файл | строк | тянут | публичные имена |
|---|---:|---:|---|
| `handlers/admin/__init__.py` | 40 | 4 | только re-export; новое имя, не вписанное сюда, роняет старт бота |
| `handlers/admin/common.py` | 379 | 13 | публичных нет — всё через имена с подчёркиванием (`_onoff`, `_require`, `_send_panel_message`, `_adm_back_row` и др.), но тянут его 13 модулей |
| `handlers/admin/router.py` | 586 | 1 | `handle_callback_query` — единственный роутер всех кнопок |
| `handlers/admin/panel_main.py` | 439 | 4 | `send_stats_panel`, `send_api_panel`, `send_daily_report_panel`, `send_weekly_report_panel`, `cmd_stats`, `build_adm_keyboard`, `send_adm_panel`, `cmd_adm` |
| `handlers/admin/panel_prompts.py` | 1225 | 3 | `send_prompt_files`, `send_prompts_panel`, `handle_prompt_reset`, `cmd_prompt_set/add/reset`, `cmd_news_prompt_set/reset`, `cmd_rag_prompt_set/reset`, `cmd_proactive_prompt_set/reset` |
| `handlers/admin/panel_users.py` | 1337 | 3 | `send_users_panel`, `send_user_card`, `cmd_users` |
| `handlers/admin/panel_rag.py` | 1002 | 6 | `send_rag_panel`, `cmd_rag`, `handle_kb_document`, `handle_kb_test_query` (панель из трёх экранов: разделы → список раздела → настройки поиска) |
| `handlers/admin/panel_mod.py` | 591 | 3 | `send_mod_panel`, `cmd_mod`, `cmd_unmute` |
| `handlers/admin/panel_quiz.py` | 549 | 3 | `send_quiz_panel`, `cmd_quiz_admin` |
| `handlers/admin/panel_balance.py` | 441 | 3 | `send_balance_panel`, `handle_balance_input` |
| `handlers/admin/panel_updates.py` | 181 | 3 | `send_updates_panel` |
| `handlers/admin/panel_digest.py` | 179 | 2 | `digest_keyboard`, `send_digest_panel` |

## `services/` — логика, не привязанная к экрану

| файл | строк | тянут | публичные имена |
|---|---:|---:|---|
| `services/gemini.py` | 2284 | 7 | `compress_newlines`, `ask_gemini`, `ask_gemini_audio`, `ask_gemini_video`, `format_news_as_colonel`, `author_brief`, `ask_group_proactive`, `ask_group_proactive_media`, `generate_image` |
| `services/antispam.py` | 866 | 7 | `is_enabled`, `get_thresholds`, `get_thresholds_for`, `trust_info`, `is_trusted`, `check_and_mute`, `unmute`, `mute_user`, `kick_user`, `ban_user`, `unban_user`, `notify_owners_ai_mute`, `is_linkfilter_enabled`, `check_and_delete_links`, `get_mute_stats`, `get_recent_actions`, `get_evidence` |
| `services/daily_report.py` | 793 | 7 | `kyiv_now`, `kyiv_label`, `collect_counters`, `period_totals`, `render`, `build_report`, `midnight_report`, `today_so_far`, `weekly_report`, `week_so_far`, `last_report_text`, `last_weekly_text` и др. |
| `services/rag.py` | 809 | 5 | `cosine_similarity`, `RagQuotaError`, `get_embedding`, `parse_article_file`, `get_kb_stats`, `load_knowledge_base`, `is_active`, `sync_knowledge_base`, `index_lag`, `rebuild_knowledge_base`, `normalize_query`, `retrieve_relevant_context`, `test_search` |
| `services/proactive.py` | 604 | 2 | `skip_counts`, `is_enabled`, `hands_enabled`, `note_bot_group_reply`, `forget_conversations`, `consider_message` |
| `services/tech_card.py` | 513 | 1 | `index`, `find_local`, `suggest`, `by_kind`, `by_title`, `token`, `by_token`, `load`, `render_card`, `render_section`, `render_candidates`, `kinds_summary`, `section_label`, `is_specs`, `short_title`, `kind_icon` |
| `services/quiz_bank.py` | 478 | 2 | `articles_without_questions`, `generate_for_article`, `generate_batch`, `retry_failed`, `stats`, `seed_stats`, `load_seed` |
| `services/greeter.py` | 419 | 3 | `is_enabled`, `captcha_enabled`, `kick_enabled`, `timeout_sec`, `on_chat_member`, `handle_join_callback` |
| `services/scraper.py` | 346 | 1 | `fetch_latest_news`, `fetch_article` (сайт `https://wtmobile.com/ru/news`) |
| `services/roles.py` | 311 | 12 | `load`, `make_moderator`, `unmake_moderator`, `grant_perm`, `is_owner`, `is_moderator`, `is_staff`, `role_of`, `can`, `has_any_perm`, `perms_of`, `list_moderators`, `can_act_on`, `perm_for_callback`, `may_press` |
| `services/knowledge_store.py` | 300 | 5 | `save_pending_news`, `read_title`, `detect_kind`, `list_articles`, `read_article`, `approve_article`, `delete_article`, `add_article`, `replace_article` |
| `services/group_digest.py` | 299 | 3 | `is_enabled`, `week_key`, `due_now`, `note_sent`, `collect`, `render`, `build` |
| `services/update_log.py` | 263 | 1 | `available`, `version_of`, `recent`, `stats`, `fmt_time`, `fmt_day`, `fmt_ago` |
| `services/backup.py` | 280 | 2 | `backup_dir`, `human_size`, `make_backup`, `list_backups`, `make_kb_backup`, `kb_caption`, `due_today`, `note_done`, `last_done` |
| `services/deploy.py` | 164 | 3 | `note_activity`, `quiet_for`, `can_update`, `update`, `describe` |
| `services/user_settings.py` | 148 | 9 | `load`, `refresh`, `get`, `set_field`, `clear`, `thresholds_for`, `is_immune`, `links_allowed`, `ai_ignored`, `image_limit_for`, `honorary_rank` |
| `services/http.py` | 58 | 5 | `session` |
| `services/__init__.py` | 2 | 0 | — |

## `jobs/` — фоновые задачи

| файл | строк | публичные имена |
|---|---:|---|
| `jobs/__init__.py` | 36 | re-export; новая задача, не вписанная сюда, роняет старт `main.py` |
| `jobs/reports.py` | 294 | `daily_report_loop`, `weekly_group_digest`, `nightly_backup` |
| `jobs/cleanup.py` | 234 | `cleanup_loop` |
| `jobs/update.py` | 221 | `forget_update_notice`, `auto_update_loop` |
| `jobs/news.py` | 201 | `send_news_to_chat`, `news_polling_loop` |
| `jobs/watchdog.py` | 102 | `watchdog_loop` |
| `jobs/rag.py` | 85 | `rag_catchup_loop` |

## Прочее

| путь | что это |
|---|---|
| `quiz/add.py` | отдельный скрипт (53 строки, `main`), тянет `services.quiz_bank`; из бота не вызывается |
| `quiz/questions.json` | 143 КБ данных |
| `knowledge/approved/` | статьи базы знаний (`.md`) — источник RAG и справочника `/ttx`; **в git не едет** (папка принадлежит серверу, 12.08.2026) |
| `knowledge/pending/` | статьи, ждущие одобрения; тоже мимо git |
| `knowledge/knowledge_base_vectors.json` | указатель RAG; строится ботом, в git не едет |
| `history.db`, `seed.db` | боевая база и заготовка |
| `deploy.sh`, `deploy-restart.sh` | выкатка на сервер |
| `.github/workflows/preflight.yml` | проверка в GitHub Actions |
| `.claude/worktrees/` | **копии старых версий проекта, не код** |
