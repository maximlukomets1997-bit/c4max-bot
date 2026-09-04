# ───────────────────────────────────────────────
#  database/history.py — ОГЛАВЛЕНИЕ пакета работы с данными (02.09.2026).
#
#  ⚠️ КОДА ЗДЕСЬ БОЛЬШЕ НЕТ. Раньше это был один файл на 2804 строки и 122
#  имени — самый нужный файл проекта, его тянут 40 модулей. Разрезан на части
#  БЕЗ изменения логики, одиннадцатью шагами; каждый шаг сверялся разбором
#  кода до и после («столько-то функций было, столько же стало, изменившихся
#  тел — ноль»). Тем же приёмом и по той же причине когда-то разошлись
#  admin.py (2140 строк) и jobs.py (837).
#
#  Резать оказалось безопасно потому, что это была не «одна тема», а дюжина
#  независимых: между собой они звали друг друга ВСЕГО ТРИ РАЗА, а остальные
#  103 связи вели к замку и соединению — то есть к общему фундаменту.
#
#  ⚠️ СНАРУЖИ НИЧЕГО НЕ ИЗМЕНИЛОСЬ, и это было условием всей работы:
#  `from database.history import add_messages` работает как работал, и так же
#  работают `hist._lock` и `hist._get_connection()` (их зовёт selftest.py,
#  девятью местами). Сорок зависимых модулей не правились ни разу.
#
#  ⚠️ НОВОЕ ИМЯ, НЕ ВПИСАННОЕ СЮДА, СНАРУЖИ НЕ ВИДНО. Ту же роль играет
#  re-export в jobs/__init__.py и handlers/admin/__init__.py. Перечислено
#  явно, а не звёздочкой: имя, которое отсюда пропадёт, должно ломаться сразу
#  и громко, а не молча исчезать из того, чем торгует модуль.
#
#  Что где лежит:
#    _core.py      — замок базы, общее соединение, close_db, backup_to
#    _schema.py    — таблицы, догоняющие графы, init_db
#    settings.py   — настройки бота и тексты промптов
#    chat.py       — переписка с ботом и гигиена его сообщений
#    groups.py     — архив сообщений групп и список известных групп
#    people.py     — личные дела, персональные настройки, персонал
#    money.py      — копилки расхода и счётчики обращений к моделям
#    stats.py      — счётчики бота и ночные снимки
#    quiz.py       — банк вопросов викторины и счёт игроков
#    moderation.py — журнал наказаний и улики
#    journals.py   — четыре простых журнала (персонал, база знаний,
#                    участие в разговоре, вступления в группы)
#    news.py       — подписки на новости и учёт разосланного
#
#  ⚠️ ЕДИНСТВЕННАЯ СВЯЗЬ МЕЖДУ ЧАСТЯМИ: chat.py и groups.py читают настройки
#  из settings.py. Поэтому настройки и уехали раньше их — иначе получилось бы
#  кольцо. Всё остальное зависит только от _core.py.
#
#  ⚠️ Соединение с базой спрашивать ТОЛЬКО функцией _get_connection().
#  Переменную _conn не импортировать — почему, написано в шапке _core.py.
# ───────────────────────────────────────────────

# ─── фундамент: замок, соединение, копия базы ───────────────────────
from ._core import (_DbLock, _lock, _compact_sql, _short, _LoggingConnection,
                    _get_connection, close_db, backup_to)

# ─── устройство базы: таблицы, графы, запуск ────────────────────────
from ._schema import (_create_schema, _COLUMN_MIGRATIONS,
                      _run_column_migrations, _seed_once, init_db)

# ─── настройки и тексты промптов ────────────────────────────────────
from .settings import (get_setting, set_setting, delete_setting,
                       get_active_system_prompt, append_prompt_addition,
                       get_news_system_prompt, get_rag_instruction,
                       get_proactive_instruction, get_author_brief_instruction)

# ─── переписка с ботом и гигиена его сообщений ──────────────────────
from .chat import (get_history, get_history_length, get_user_usage,
                   add_messages, add_bot_message, clear_history,
                   register_bot_message, get_old_bot_messages,
                   remove_bot_message)

# ─── архив сообщений групп и известные группы ───────────────────────
from .groups import (save_group_message, update_last_group_message_text,
                     set_proactive_reset_mark, get_recent_group_messages,
                     delete_old_group_messages, remember_chat, get_known_chats,
                     get_group_messages_between)

# ─── люди: дела, персональные настройки, персонал ───────────────────
from .people import (dossier_add_message, dossier_add_mute, dossier_add_linkdel,
                     dossier_reset_violations, get_dossier, list_known_users,
                     _USER_SETTING_FIELDS, get_user_settings,
                     get_all_user_settings, set_user_settings,
                     clear_user_settings, _STAFF_PERM_FIELDS, get_all_staff,
                     get_staff, add_staff, remove_staff, set_staff_perm)

# ─── деньги: копилки расхода и счётчики обращений ───────────────────
from .money import (add_provider_cost, spend_qwen_tokens, get_qwen_tokens,
                    register_api_call, clear_api_calls, clear_user_token_usage,
                    register_image_call, unregister_image_call,
                    get_remaining_image_calls)

# ─── счётчики бота и ночные снимки ──────────────────────────────────
from .stats import (_kyiv_today_start_utc, get_bot_stats, save_stats_snapshot,
                    get_last_stats_snapshot, count_api_calls_between,
                    delete_old_stats_snapshots)

# ─── викторина: банк вопросов и счёт игроков ────────────────────────
from .quiz import (add_quiz_attempt, get_user_stats, set_quiz_stats, _row_to_question,
                   add_quiz_question, get_random_quiz_question,
                   note_quiz_question_asked, get_quiz_bank_counts,
                   get_quiz_articles_covered, list_quiz_questions,
                   list_all_quiz_questions, get_quiz_question,
                   update_quiz_question_body, set_quiz_question_approved,
                   delete_quiz_question, note_quiz_failure, clear_quiz_failure,
                   list_quiz_failures, count_quiz_failures, clear_quiz_failures,
                   delete_quiz_drafts, reset_all_quiz_stats,
                   delete_all_quiz_questions, get_all_quiz_stats)

# ─── журнал наказаний и улики ───────────────────────────────────────
from .moderation import (log_moderation_action, save_mute_evidence,
                         get_moderation_counts, get_recent_moderation_actions,
                         get_moderation_entry, get_mute_evidence,
                         delete_old_moderation_log, clear_moderation_log)

# ─── четыре простых журнала ─────────────────────────────────────────
from .journals import (log_staff_action, get_recent_staff_actions,
                       count_staff_actions, delete_old_staff_log,
                       clear_staff_log, add_kb_action, get_recent_kb_actions,
                       delete_old_kb_log, clear_kb_log, log_proactive_check,
                       proactive_stats, proactive_by_chat, proactive_by_day,
                       delete_old_proactive_log, log_join, get_join_counts,
                       delete_old_join_log)

# ─── подписки на новости и учёт разосланного ────────────────────────
from .news import (subscribe_chat, unsubscribe_chat, is_chat_subscribed,
                   get_subscribed_chats, is_news_already_sent,
                   mark_news_as_sent, count_sent_news_between)
