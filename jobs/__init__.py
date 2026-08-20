# ───────────────────────────────────────────────
#  jobs/__init__.py — ОГЛАВЛЕНИЕ пакета фоновых задач.
#
#  До 2026-08-04 всё это лежало в одном файле jobs.py на 837 строк. Шесть
#  циклов не зовут друг друга вовсе — это была не «одна тема», а шесть,
#  сложенных в один файл; поэтому он разрезан по циклам БЕЗ правки логики.
#
#  Снаружи пакет выглядит как прежний модуль: `from jobs import cleanup_loop`
#  работает как работал. ⚠️ Новая фоновая задача — класть в СВОЙ файл и
#  обязательно дописывать имя сюда, иначе `from jobs import …` в main.py
#  упадёт при старте (ту же роль играет re-export в handlers/admin/__init__.py).
#
#  Что где:
#    news.py     — опрос wtmobile.com, форматирование моделью, рассылка
#    cleanup.py  — суточная чистка журналов + месячный сброс статистики API
#    reports.py  — суточный и недельный отчёты о расходах + ночная копия базы
#    rag.py      — ежечасный добор базы знаний после лимита Google
#    update.py   — самообновление: забрать новый код с GitHub и перезапуститься
#    watchdog.py — отметки живости внешнему сторожу (healthchecks.io)
# ───────────────────────────────────────────────

from .news import send_news_to_chat, news_polling_loop
from .cleanup import cleanup_loop, _monthly_stats_reset
from .reports import (daily_report_loop, nightly_backup, weekly_group_digest,
                      daily_quiz)
from .rag import rag_catchup_loop
from .update import auto_update_loop, forget_update_notice
from .watchdog import watchdog_loop

__all__ = [
    "send_news_to_chat", "news_polling_loop",
    "cleanup_loop", "_monthly_stats_reset",
    "daily_report_loop", "nightly_backup", "weekly_group_digest", "daily_quiz",
    "rag_catchup_loop", "auto_update_loop", "forget_update_notice",
    "watchdog_loop",
]
