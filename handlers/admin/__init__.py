# ───────────────────────────────────────────────
#  handlers/admin — админ-панели бота (пакет)
#
#  До 2026-07-13 это был один файл admin.py (~2140 строк). Разрезан на части
#  БЕЗ изменения логики (чистый переезд); резервная копия монолита
#  (admin.py.bak-before-split) со временем удалена — файла в проекте больше нет.
#    common.py        — общие помощники (гейт админа, отправка панелей, логи)
#    router.py        — handle_callback_query: ЕДИНСТВЕННЫЙ роутер всех кнопок
#    panel_main.py    — главная панель /adm, статистика /stats, модели/API
#    panel_prompts.py — панель промптов + /prompt_*, /news_prompt_*, /rag_prompt_*
#    panel_mod.py     — модерация: /mod, улики, размут
#    panel_rag.py     — база знаний: /rag, приём файлов, «Проверить поиск»
#    panel_users.py   — «👥 Пользователи»: список и карточка участника
#
#  Этот файл — «оглавление»: собирает имена, которыми пользуется остальной
#  код (handlers/__init__.py, main.py, handlers/messages.py), поэтому снаружи
#  пакет выглядит как прежний handlers.admin — импорты не менялись.
# ───────────────────────────────────────────────

from .common import _adm_back_row, _send_panel_message
from .panel_rag import (
    send_rag_panel, cmd_rag, handle_kb_document, handle_kb_test_query, _end_kb_test,
)
from .panel_prompts import (
    send_prompts_panel, cmd_prompt_set, cmd_prompt_add, cmd_prompt_reset,
    cmd_news_prompt_set, cmd_news_prompt_reset, cmd_rag_prompt_set, cmd_rag_prompt_reset,
    cmd_proactive_prompt_set, cmd_proactive_prompt_reset, send_prompt_files,
)
from .panel_main import (send_adm_panel, cmd_adm, send_stats_panel, cmd_stats, send_api_panel,
                         send_daily_report_panel, send_weekly_report_panel)
from .panel_mod import send_mod_panel, cmd_mod, cmd_unmute
from .panel_users import send_users_panel, send_user_card, cmd_users
from .router import handle_callback_query
