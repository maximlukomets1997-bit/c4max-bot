# ─────────────────────────────────────────────
#  handlers/admin/panel_keys.py — ⌨️ нижние кнопки владельца (2026-10-17)
# ─────────────────────────────────────────────
#  Панель кнопок ПОД ПОЛЕМ ВВОДА в личке владельца (просьба Максима — «как в
#  другом боте»). Кнопки ведут в те же разделы, что /adm, плюс очистка
#  памяти и перезапуск оттуда же (перезапуск и единая очистка — с 18.10.2026;
#  перезапуск с 19.10.2026, очистка с 20.10.2026 — в подменю «🎛 Админ-панель»).
#  Модераторам, другим людям и в группах панели нет.
#
#  Как устроено:
#   • send_owner_keys — шлёт сообщение с панелью и ОСТАВЛЯЕТ его, а
#     предыдущее такое сообщение удаляет: в чате всегда одно. Зовут /start
#     владельца и его /adm (panel_main.send_adm_panel — в том числе автопанель
#     при запуске бота в main.py).
#     ⚠️ СООБЩЕНИЕ С ПАНЕЛЬЮ УДАЛЯТЬ НЕЛЬЗЯ (v5.71 → v5.72, 18.10.2026). В v5.71
#     бот удалял его в ту же секунду — по пробе 17.10, где кнопки пережили
#     удаление, сделанное через несколько минут. При мгновенном удалении
#     Telegram панель не сохраняет: у Максима она пропала сразу. Проба 18.10:
#     сообщение лежит — панель переживает и выход из чата, и перезапуск
#     Telegram; новое пришло, старое удалено — панель на месте.
#     ⚠️ ЭТО СООБЩЕНИЕ И ЕСТЬ /adm ВЛАДЕЛЬЦА (18.10.2026, решение Максима: два
#     одинаковых сообщения — одно). Его текст — заголовок админ-панели с
#     версией и три сводные цифры (carrier_text; цифры — с 18.10.2026, когда
#     «💬 Статистика» стала частью «🎛 Админ-панель»). /adm и «⬅️ Админ-панель»
#     присылают его заново с разделами внизу (panel_main.send_adm_panel),
#     нижняя «🎛 Админ-панель» и /stats — с кнопками бывшей статистики
#     (panel_main.send_stats_panel, подменю SERVICE_ROWS); прежнее удаляется.
#     ⚠️ В гигиену панелей (register_and_clean_bot_message) его НЕ класть:
#     открытие любого раздела стёрло бы его, а с ним и нижние кнопки.
#     Пропала (сообщение удалили руками) — её вернут /start и /adm.
#   • handle_owner_key — нажатие нижней кнопки приходит ОБЫЧНЫМ текстом. Его
#     зовёт handlers/messages.py ДО модели и до склейки: текст совпал с
#     кнопкой и пишет владелец в личке — сообщение удаляется (как команда),
#     открывается раздел. Тот же текст от кого угодно другого — обычный вопрос.
#   • ВЫКЛЮЧАТЕЛЯ НЕТ (18.10.2026, решение Максима): у владельца панель есть
#     всегда. До этого дня в /adm стоял «⌨️ НИЖНИЕ КНОПКИ» (settings
#     'owner_keys_enabled'); запись в базе никем не читается, «0» в ней панель
#     не уберёт — не возвращать.
#   • Номер сообщения с панелью — в settings 'owner_keys_msg_<номер владельца>':
#     по нему удаляется предыдущее. ⚠️ Telegram не даёт ботам удалять сообщения
#     старше двух суток — тогда старое остаётся, и в чате их два.
#   • Подменю «📡 Настройки API» (18.10.2026, пилот) — API_ROWS, «⚙️ PROMPTы»
#     (18.10.2026) — PROMPTS_ROWS, «🎛 Админ-панель» (18.10.2026) —
#     SERVICE_ROWS, «🛠 DDoS-Guard» (19.10.2026) — MOD_ROWS и «🎮 Викторина»
#     (19.10.2026) — ряды из panel_quiz.quiz_keys_rows, «📚 База знаний»
#     (19.10.2026) — ряды из panel_rag, в том числе очередь «Ждут одобрения»
#     списком новостей (меню rag_pending): та же панель, другие
#     кнопки; сообщение с ним — тот же один в чате. Подробности — у самих
#     раскладок.
#
#  ⚠️ «🌐 Сайт» — НЕ кнопка-мини-приложение (проба 17.10.2026): открытый с
#  нижней кнопки сайт не получает от Telegram, кто его открыл, и пишет «Вход
#  только через Telegram». Поэтому кнопка присылает те же две, что вверху /adm
#  (panel_main._web_row): там вход работает.
#  ⚠️ Раздел открывается ТЕМИ ЖЕ функциями, что кнопки /adm (router.py), и
#  гасит те же режимы ожидания, что любая команда: второго набора правил
#  «что открыть» в проекте быть не должно.
# ─────────────────────────────────────────────

import logging
import re

from telegram import KeyboardButton, LinkPreviewOptions, ReplyKeyboardMarkup
from telegram.constants import ParseMode

from config import BOT_VERSION_HTML
from database.history import get_setting, set_setting
from utils import (clean_bot_messages, delete_user_message_safe,
                   register_and_clean_bot_message, schedule_delete)

logger = logging.getLogger(__name__)

# Номер сообщения, с которым стоит панель: owner_keys_msg_<номер владельца>.
MSG_KEY_PREFIX = "owner_keys_msg_"
# Заголовок сообщения с главной панелью — оно же /adm владельца (18.10.2026,
# надпись утверждена Максимом): версия синей ссылкой и черта. До этого дня
# здесь было отдельное «⌨️ Панель управления — кнопки внизу…», а /adm
# приходил вторым сообщением с тем же смыслом.
CARRIER_HEAD = (f"🎛 <b>АДМИН-ПАНЕЛЬ</b> · {BOT_VERSION_HTML}\n"
                "───────────────────────────")


def carrier_text() -> str:
    """
    Текст сообщения с главной панелью: заголовок с версией и три сводные
    цифры (18.10.2026, решение Максима: «🎛 Админ-панель» и «💬 Статистика» —
    одно). Цифры — те же, что в панели статистики (panel_main.stats_lines),
    свежие на момент отправки; сами не обновляются. Подсчёт сорвался —
    сообщение уходит с одной версией: из-за цифр нижние кнопки пропасть не
    должны.
    """
    try:
        from .panel_main import stats_lines
        return f"{CARRIER_HEAD}\n{stats_lines()}"
    except Exception as e:
        logger.warning("⚠️ Цифры для сообщения с версией не посчитались: %s", e)
        return CARRIER_HEAD


# Надпись кнопки → код действия. Порядок — раскладка: первая — во всю ширину,
# остальные по две в ряд, нечётная последняя — одна во всю ширину (по две — утверждено Максимом на пробе 17.10.2026;
# «🎛 Админ-панель» одна сверху — 18.10.2026, когда ушла «💬 Статистика»: так
# все остальные кнопки остались на своих местах). Надпись и есть то, что
# Telegram пришлёт текстом при нажатии, — менять её значит менять и
# распознавание.
KEYS = (
    # «🎛 Админ-панель» с 18.10.2026 — и статистика: присылает сообщение с
    # версией и цифрами, а внизу ставит кнопки бывшей панели статистики
    # (подменю SERVICE_ROWS). Разделы возвращает «⬅️ Назад» или /adm.
    ("🎛 Админ-панель", "adm"),
    ("⚙️ PROMPTы", "prompts"),
    ("📡 Настройки API", "api"),
    ("🛠 DDoS-Guard", "mod"),
    ("👥 Пользователи", "users"),
    ("📚 База знаний", "rag"),
    ("🎮 Викторина", "quiz"),
    ("📜 Логи бота", "logs"),
    ("🌐 Сайт", "site"),
    # «🧹 Очистить ПАМЯТЬ» стояла здесь последней до 20.10.2026 — теперь она в
    # подменю «🎛 Админ-панель» (SERVICE_ROWS, решение Максима). Не возвращать.
)

# Подменю «📡 Настройки API» (2026-10-18, пилот по решению Максима): нижняя
# «📡 Настройки API» присылает панель раздела, как раньше, а нижняя панель на
# время становится переключателями раздела. Ряды — раскладка; «⬅️ Назад»
# возвращает главную. Переключатели — ТЕ ЖЕ шаги, что кнопки под панелью
# (panel_main.flip_api_toggle / step_batch), а под самой панелью их тогда нет
# (panel_main.api_toggles_below). /start и перезапуск бота всегда ставят
# главную: какое подменю открыто, бот не помнит и помнить не обязан.
API_ROWS = (
    (("🧠 МЫСЛИ ПОД КАПОТОМ", "thoughts"),),
    (("✍️ ОТВЕТ НА ГЛАЗАХ", "live_answer"),),
    (("🧠 МЫСЛИ В ЧЕРНОВИКЕ", "live_thoughts"),),
    (("➖ склейка", "batch_dec"), ("🧩 СКЛЕЙКА", "batch_info"), ("➕ склейка", "batch_inc")),
    (("⬅️ Назад", "back"),),
)
API_CARRIER_TEXT = "📡 Настройки API — переключатели внизу. «⬅️ Назад» — к разделам."
API_KEYS = tuple(key for row in API_ROWS for key in row)

# Подменю «⚙️ PROMPTы» (2026-10-18, решение Максима): переключатели и
# регуляторы промптов — внизу (panel_prompts.flip_prompts_toggle /
# step_proactive; под панелью их тогда нет — panel_prompts.prompts_toggles_below).
# ⚠️ В ОТЛИЧИЕ ОТ API, ПОДМЕНЮ ДЕРЖИТ САМА ПАНЕЛЬ ПРОМПТОВ (18.10.2026, решение
# Максима: одно сообщение вместо двух) — своего короткого текста у него нет,
# текст собирает panel_prompts.prompts_panel_text. У панели API так нельзя:
# под ней кнопки моделей, а у сообщения один набор кнопок. Отсюда цена:
# каждое нажатие переключателя присылает заново ВСЮ панель — надписи нижних
# кнопок у отправленного сообщения Telegram поменять не даёт.
# «Сам в разговор» и «Руки» — одним рядом, «Руки» с короткой надписью «(мут)» —
# так решил Максим. Очистки разговоров здесь нет: она в главной панели, с
# 18.10.2026 — частью «🧹 Очистить ПАМЯТЬ».
PROMPTS_ROWS = (
    (("⚙️ PROMPT", "p_prompt"), ("💬 ОТВЕТЫ ИИ", "p_ai_replies")),
    (("🗣 САМ В РАЗГОВОР", "p_proactive"), ("🤚 РУКИ (мут)", "p_hands")),
    (("➖ порог", "p_mm_dec"), ("💬 порог", "p_mm_info"), ("➕ порог", "p_mm_inc")),
    (("➖ контекст", "p_ctx_dec"), ("📜 контекст", "p_ctx_info"), ("➕ контекст", "p_ctx_inc")),
    (("📊 Подробнее об участии", "p_stats"), ("📄 Полные PROMPTы", "p_files")),
    (("⬅️ Назад", "back"),),
)
PROMPTS_KEYS = tuple(key for row in PROMPTS_ROWS for key in row)

# Подменю «🎛 Админ-панель» (18.10.2026, решение Максима, вариант Б): кнопки
# бывшей панели «💬 СТАТИСТИКА». Стоит на том же сообщении с версией и
# цифрами (carrier_text), что и главная. Каждая — то же, что кнопка под
# прежней панелью: экран обновлений, копия базы файлом и экран дайджеста.
# ⚠️ ТУМБЛЕРА «📰 Дайджест по понедельникам» БОЛЬШЕ НЕТ (20.10.2026, решение
# Максима): дайджест приходит всегда (services/group_digest.py), тумблер убран
# отовсюду — внизу, в /adm и на сайте. Старая надпись на экране узнаётся
# (_STATEFUL, код s_digest_auto) и отвечает, что дайджест включён всегда.
# «🧹 Очистить ПАМЯТЬ» (20.10.2026, решение Максима) — встала на его место
# отдельным рядом, из главной панели ушла. Одна очистка вместо двух
# (18.10.2026): память групп и личная переписка ВСЕХ людей стираются разом,
# БЕЗ вопроса (panel_prompts.wipe_memory) — так решил Максим и 20.10, когда
# кнопка встала рядом с перезапуском, который спрашивает.
# «🔄 ПЕРЕЗАПУСК» — с 19.10.2026 здесь, одним рядом с «⬅️ Назад» (решение
# Максима; до этого — последним в главной). Копия кнопки /adm: сначала
# спрашивает, «✅ Да» ведёт на ту же ветку system_restart. Рядом до вечера
# 18.10 стоял тумблер «⬇️ ОБНОВЛЕНИЕ»; самообновление теперь включено всегда
# (jobs/update.py) — не возвращать.
SERVICE_ROWS = (
    (("⬇️ Обновления", "s_updates"), ("💾 Копия базы", "s_backup")),
    (("📊 Дайджест недели", "s_digest"),),
    (("🧹 Очистить ПАМЯТЬ", "wipe"),),
    (("🔄 ПЕРЕЗАПУСК", "restart"), ("⬅️ Назад", "back")),
)
SERVICE_KEYS = tuple(key for row in SERVICE_ROWS for key in row)
DIGEST_ALWAYS_TEXT = "📰 Дайджест по понедельникам теперь приходит всегда — выключать его не нужно."


# Подменю «🛠 DDoS-Guard» (2026-10-19, решение Максима) — так же, как у
# «⚙️ PROMPTы»: сообщение с подменю — сама панель раздела с её текстом
# (panel_mod.send_mod_panel у владельца), под ней кнопок нет. Переключатели и
# регуляторы — те же шаги, что под панелью (panel_mod.flip_mod_toggle /
# step_mod), надписи — panel_mod.mod_toggle_text. Тумблеры по два в ряд, с
# короткими надписями — так утвердил Максим по макету 19.10.
# Последний ряд — MOD_LAST_ROW; «📜 Улики» встаёт рядом с «⬅️ Назад», только
# когда у последних действий есть улики (owner_keyboard). «🧹 Очистить журнал»
# сначала спрашивает — журнал стирается вместе с уликами.
MOD_ROWS = (
    (("🛡 DDoS-Guard", "m_guard"), ("🔗 Фильтр ссылок", "m_linkfilter")),
    (("➖ сообщ.", "m_count_dec"), ("📨 сообщ.", "m_count_info"), ("➕ сообщ.", "m_count_inc")),
    (("➖ окно", "m_window_dec"), ("⏱ окно", "m_window_info"), ("➕ окно", "m_window_inc")),
    (("➖ мут", "m_mute_dec"), ("🔇 мут", "m_mute_info"), ("➕ мут", "m_mute_inc")),
    (("👋 Приветствие", "m_greet"), ("🤖 «Я не бот»", "m_captcha")),
    (("➖ срок", "m_time_dec"), ("⏳ срок", "m_time_info"), ("➕ срок", "m_time_inc")),
    (("👢 Кикать", "m_kick"), ("🧹 Очистить журнал", "m_clearlog")),
)
MOD_EVIDENCE_KEY = ("📜 Улики", "m_evidence")
MOD_KEYS = tuple(key for row in MOD_ROWS for key in row) + (MOD_EVIDENCE_KEY,)

# Подменю «🎮 Викторина» (2026-10-19, решение Максима) — так же, как у
# «🛠 DDoS-Guard»: сообщение с подменю — сама панель викторины, под ней кнопок
# нет. Ряды собирает panel_quiz.quiz_keys_rows В МОМЕНТ ОТПРАВКИ: кнопка есть,
# только когда у неё есть работа, и почти у всех в подписи число. Шаги — те же,
# что у кнопок под панелью (panel_quiz.flip_quiz_auto, seed_into_drafts,
# reseed_from_file); стирание и обнуление сначала спрашивают
# (panel_quiz.confirm_markup — те же «Да / Отмена», что под панелью).
# Здесь — надписи без чисел; с числом узнаются по образцу (_STATEFUL).
QUIZ_KEYS = (("🗑 Очистить черновики", "q_wipe"), ("🗑 Стереть ВСЕ вопросы", "q_nuke"))

# Подменю «📚 База знаний» и «⚙️ Настройки поиска» (2026-10-19, решение
# Максима): ряды собирают panel_rag.rag_keys_rows / rag_settings_rows в момент
# отправки — разделы с живыми счётчиками, регуляторы с живыми числами. Здесь —
# надписи без чисел; с числом узнаются по образцу (_STATEFUL). ⚠️ «🧹 Очистить
# журнал» и «➖/➕ порог» уже заняты (DDoS-Guard, PROMPTы) — отсюда «… RAG».
# «⬅️ К базе знаний» — из настроек в подменю базы, а «⬅️ Назад» — в главную.
RAG_KEYS = (("➕ Добавить RAG", "r_add"), ("🔄 Пересобрать RAG", "r_rebuild"),
            ("⚙️ Настройки поиска", "r_settings"), ("🧹 Очистить журнал RAG", "r_clearlog"),
            ("➖ порог RAG", "r_thr_dec"), ("➕ порог RAG", "r_thr_inc"),
            ("➖ статей", "r_topk_dec"), ("➕ статей", "r_topk_inc"),
            ("➖ запас", "r_margin_dec"), ("➕ запас", "r_margin_inc"),
            ("🔍 Проверить поиск", "r_test"), ("⏹️ Завершить проверку", "r_test"),
            ("⬅️ К базе знаний", "r_back"),
            # Нижний список «🕐 Ждут одобрения» (panel_rag.rag_pending_rows): листание
            # и возврат. Сами новости («📄 <название>», код r_art) постоянной
            # надписи не имеют — их узнаёт panel_rag.pending_label_file.
            ("⬅️", "r_pend_prev"), ("➡️", "r_pend_next"), ("⬅️ К разделам", "r_back"))
# Нажатия ВНУТРИ базы знаний не обрывают проверку поиска: «спросил → подкрутил
# порог → спросил снова» — основной способ настроить поиск (как под панелью).
# Карточка новости (r_art) — обрывает, как кнопка статьи под списком (kb_view).
_KEEP_KB_TEST = {"rag", "r_settings", "r_back", "r_kb", "r_test",
                 "r_thr_dec", "r_thr_inc", "r_thr_info", "r_topk_dec", "r_topk_inc",
                 "r_topk_info", "r_margin_dec", "r_margin_inc", "r_margin_info",
                 "r_pending", "r_ground", "r_sub", "r_ship", "r_guide", "r_air", "r_other",
                 "r_pend_prev", "r_pend_next"}
# Коды, при которых нижний список новостей остаётся на экране — память его
# надписей (user_data["kb_pending_keys"]) не стирается.
_KEEP_PENDING_KEYS = {"r_art", "r_pend_prev", "r_pend_next"}

# Подменю: раскладка и текст сообщения, с которым оно стоит. None — текст
# собирается в момент отправки: у prompts и mod — сама панель раздела, у
# service — сообщение с версией и цифрами.
_MENUS = {"api": (API_ROWS, API_CARRIER_TEXT), "prompts": (PROMPTS_ROWS, None),
          "service": (SERVICE_ROWS, None), "mod": (MOD_ROWS, None)}

_ALL_KEYS = KEYS + API_KEYS + PROMPTS_KEYS + SERVICE_KEYS + MOD_KEYS + QUIZ_KEYS + RAG_KEYS
# «💬 Статистика» ушла из панели 18.10.2026; если её кнопка осталась на
# экране от прежней панели, нажатие — то же, что «🎛 Админ-панель», а не
# вопрос модели.
_LEGACY_LABELS = (("💬 Статистика", "adm"),)
_BY_LABEL = {label: code for label, code in _ALL_KEYS + _LEGACY_LABELS}
# У этих кнопок в надписи состояние или число («…: 🟢ВКЛ», «…: 3 с»,
# «💬 3 сообщ.») — надпись целиком собирает тот же шаг, что у кнопки под
# панелью, а узнаётся она по образцу при ЛЮБОМ состоянии.
_STATEFUL = {
    "thoughts": "🧠 МЫСЛИ ПОД КАПОТОМ: .+",
    "live_answer": "✍️ ОТВЕТ НА ГЛАЗАХ: .+",
    "live_thoughts": "🧠 МЫСЛИ В ЧЕРНОВИКЕ: .+",
    "batch_info": "🧩 СКЛЕЙКА: .+",
    "p_prompt": "⚙️ PROMPT: .+",
    "p_ai_replies": "💬 ОТВЕТЫ ИИ: .+",
    "p_proactive": "🗣 САМ В РАЗГОВОР: .+",
    "p_hands": r"🤚 РУКИ \(мут\): .+",
    "p_mm_info": r"💬 \d+ сообщ\.",
    "p_ctx_info": r"📜 \d+ сообщ\.",
    # Тумблера дайджеста нет с 20.10.2026 — образец оставлен, чтобы старая
    # кнопка с экрана ответила «включён всегда», а не ушла модели вопросом.
    "s_digest_auto": "📰 Дайджест по понедельникам: .+",
    "m_guard": "🛡 DDoS-Guard: .+",
    "m_linkfilter": "🔗 Фильтр ссылок: .+",
    "m_greet": "👋 Приветствие: .+",
    "m_captcha": "🤖 «Я не бот»: .+",
    "m_kick": "👢 Кикать: .+",
    "m_count_info": r"📨 \d+ сообщ\.",
    "m_window_info": r"⏱ \d+ сек",
    "m_mute_info": r"🔇 \d+ сек",
    "m_time_info": r"⏳ \d+ мин",
    # Викторина: число в подписи меняется после каждого действия, а кнопка с
    # прежним числом на экране обязана сработать.
    "q_auto": "🕛 Вопрос дня .+: .+",
    "q_drafts": r"📝 Черновики \(\d+\)",
    "q_live": r"✅ В игре \(\d+\)",
    "q_noq": r"📋 Статьи без вопросов \(\d+\)",
    "q_reseed": r"♻️ Обновить из файла \(\d+\)",
    "q_seed": r"📥 Новые вопросы \(\d+\)",
    "q_zero": r"🧹 Обнулить статистику \(\d+\)",
    # База знаний: разделы со счётчиками, тумблер и числа регуляторов.
    "r_pending": r"🕐 Ждут одобрения \(\d+\)",
    "r_ground": r"🚜 Наземная \(\d+\)",
    "r_sub": r"⚓ Подлодки \(\d+\)",
    "r_ship": r"🛥️? Корабли \(\d+\)",
    "r_guide": r"📘 Механики \(\d+\)",
    "r_air": r"✈️? Авиация \(\d+\)",
    "r_other": r"📄 Без типа \(\d+\)",
    "r_kb": "📖 База знаний: .+",
    "r_thr_info": r"🎯 \d+%",
    "r_topk_info": r"📦 \d+ стат(ья|ьи|ей)",
    "r_margin_info": r"📈 \d+%",
}
_STATEFUL_RX = {code: re.compile(pattern) for code, pattern in _STATEFUL.items()}

# Сколько живут короткие ответы очисток — как у /clear.
_NOTICE_TTL_SEC = 7
# Пояснение склейки длиннее — живёт дольше.
_INFO_TTL_SEC = 20


def _shown(label: str, code: str, user_id: int | None = None) -> str:
    """
    Надпись на кнопке с состоянием — целиком у кнопки под панелью
    (panel_main.api_toggle_text, panel_prompts.prompts_toggle_text): разойтись
    им нельзя. user_id нужен личному тумблеру «⚙️ PROMPT».
    """
    if code.startswith(("q_", "r_")):
        # Надписи викторины и базы знаний уже живые: их собрали
        # panel_quiz.quiz_keys_rows и panel_rag.rag_keys_rows / rag_settings_rows.
        return label
    if code.startswith("p_") and code in _STATEFUL:
        from .panel_prompts import prompts_toggle_text
        return prompts_toggle_text(code[2:].removesuffix("_info"), user_id)
    if code.startswith("m_") and code in _STATEFUL:
        from .panel_mod import mod_toggle_text
        return mod_toggle_text(code[2:].removesuffix("_info"))
    if code in _STATEFUL:
        from .panel_main import api_toggle_text
        return api_toggle_text(code)
    return label


def _code_of(text: str) -> str | None:
    """
    Какая кнопка нажата. Кнопки с состоянием узнаются при ЛЮБОМ состоянии в
    надписи: панель с прежней надписью могла остаться на экране (а
    переключатель — поменяться на сайте), и её нажатие обязано сработать, а
    не уйти модели вопросом.
    """
    text = (text or "").strip()
    code = _BY_LABEL.get(text)
    if code:
        return code
    for key, rx in _STATEFUL_RX.items():
        if rx.fullmatch(text):
            return key
    return None


def owner_keyboard(menu: str = "main", user_id: int | None = None,
                   context=None) -> ReplyKeyboardMarkup:
    """
    Сама панель, компактная, не сворачивается сама. main — «🎛 Админ-панель»
    во всю ширину, остальные разделы по две кнопки в ряд; api, prompts и
    service — подменю по своим рядам; у mod последний ряд — «⬅️ Назад», а при
    уликах рядом с ним «📜 Улики».
    """
    if menu == "mod":
        from .panel_mod import _evidence_buttons
        last = ((MOD_EVIDENCE_KEY, ("⬅️ Назад", "back")) if _evidence_buttons()
                else (("⬅️ Назад", "back"),))
        layout = MOD_ROWS + (last,)
    elif menu == "quiz":
        from .panel_quiz import quiz_keys_rows
        layout = quiz_keys_rows()
    elif menu == "rag":
        # context — ради метки проверки поиска (она в user_data владельца).
        from .panel_rag import rag_keys_rows
        layout = rag_keys_rows(context)
    elif menu == "rag_settings":
        from .panel_rag import rag_settings_rows
        layout = rag_settings_rows(context)
    elif menu == "rag_pending":
        import types
        from .panel_rag import rag_pending_rows
        layout = rag_pending_rows(context if context is not None
                                  else types.SimpleNamespace(user_data={}))
    elif menu in _MENUS:
        layout = _MENUS[menu][0]
    else:
        layout = [KEYS[:1]] + [KEYS[i:i + 2] for i in range(1, len(KEYS), 2)]
    rows = [[KeyboardButton(_shown(label, code, user_id)) for label, code in row]
            for row in layout]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _is_owner_private(chat_id: int, user_id: int | None) -> bool:
    """Личка владельца: в личке номер чата совпадает с номером человека."""
    from services.roles import is_owner
    return bool(user_id) and chat_id == user_id and is_owner(user_id)


def _stored_msg(chat_id: int) -> int:
    """Номер сообщения, с которым сейчас стоит панель (0 — не знаем такого)."""
    try:
        return int(get_setting(f"{MSG_KEY_PREFIX}{chat_id}", "0") or 0)
    except (TypeError, ValueError):
        return 0


async def _delete(bot, chat_id: int, message_id: int) -> bool:
    """Удалить сообщение; False — не вышло (например, оно старше двух суток)."""
    if not message_id:
        return False
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        logger.debug("⌨️ Не удалось удалить сообщение %s с нижними кнопками: %s", message_id, e)
        return False


async def _post(bot, chat_id: int, text: str, markup, html: bool = True) -> None:
    """
    Прислать сообщение, с которым стоит панель, запомнить его и убрать
    предыдущее — уже ПОСЛЕ нового: панель живёт по последнему сообщению.
    Без звука — служебное сообщение будить телефон не должно. Разметка — HTML
    (версия синей ссылкой), превью ссылки погашено — иначе под сообщением
    развернётся карточка GitHub. Тексты — свои постоянные и панель промптов
    (её превью экранированы при сборке); html=False — запасной путь без
    разметки, как у _send_panel_message.
    """
    previous = _stored_msg(chat_id)
    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup,
                                 parse_mode=ParseMode.HTML if html else None,
                                 link_preview_options=LinkPreviewOptions(is_disabled=True),
                                 disable_notification=True)
    set_setting(f"{MSG_KEY_PREFIX}{chat_id}", str(msg.message_id))
    if previous and previous != msg.message_id:
        await _delete(bot, chat_id, previous)


async def send_owner_keys(bot, chat_id: int, user_id: int | None = None,
                          menu: str = "main", context=None) -> None:
    """
    Поставить нижние кнопки в личке владельца: главную панель (main) или
    подменю раздела (api — «📡 Настройки API», prompts — «⚙️ PROMPTы»). Не
    владелец или не личка — ничего не делает. Тихая: панель — удобство, из-за
    неё не должны падать ни /start, ни запуск бота.

    prompts — это САМА ПАНЕЛЬ ПРОМПТОВ с нижними кнопками (18.10.2026), mod и
    quiz — так же сами панели «🛠 DDoS-Guard» и «🎮 Викторина» (19.10.2026): она заменяет открытый
    раздел так же, как заменяла его прежде через гигиену панелей, — только
    сама в гигиену не попадает (см. шапку файла).
    """
    uid = chat_id if user_id is None else user_id
    if not _is_owner_private(chat_id, uid):
        return
    if menu == "rag_pending":
        # Очередь пуста (разобрали последнюю) — вместо пустого списка подменю
        # базы знаний со свежими числами, без строки «Ждут одобрения».
        from .panel_rag import kb_pending_count
        if not kb_pending_count():
            menu = "rag"
            if context is not None:
                context.user_data["kb_screen"] = ""
    if menu != "rag_pending" and context is not None:
        # Список новостей ушёл с экрана — его надписи больше не кнопки.
        context.user_data.pop("kb_pending_keys", None)
    keyboard = owner_keyboard(menu, uid, context)
    if menu in ("prompts", "mod", "quiz", "rag", "rag_settings", "rag_pending"):
        if menu == "prompts":
            from .panel_prompts import prompts_panel_text
            text = prompts_panel_text(uid, getattr(bot, "username", None))
        elif menu == "quiz":
            from .panel_quiz import _build_panel as _build_quiz_panel
            text = _build_quiz_panel(None)[0]
        elif menu in ("rag", "rag_settings", "rag_pending"):
            import types
            from .panel_rag import rag_panel_text, rag_pending_text, rag_settings_text
            ctx = context if context is not None else types.SimpleNamespace(user_data={})
            text = {"rag": rag_panel_text, "rag_settings": rag_settings_text,
                    "rag_pending": rag_pending_text}[menu](ctx)
        else:
            from .panel_mod import _build_mod_panel_text_and_keyboard
            text = _build_mod_panel_text_and_keyboard(uid)[0]
        try:
            await _post(bot, chat_id, text, keyboard)
        except Exception as e:
            logger.warning("⚠️ Панель раздела с разметкой не ушла: %s — отправляю без разметки", e)
            try:
                await _post(bot, chat_id, re.sub(r"<[^>]+>", "", text)[:4000], keyboard, html=False)
            except Exception as e2:
                logger.warning("⚠️ Не удалось поставить панель раздела (%s) владельцу %s: %s",
                               menu, chat_id, e2)
                return
        await clean_bot_messages(bot, chat_id)
        return
    # Главная и подменю «🎛 Админ-панель» стоят на сообщении с версией и цифрами.
    text = (_MENUS[menu][1] if menu in _MENUS else None) or carrier_text()
    try:
        await _post(bot, chat_id, text, keyboard)
    except Exception as e:
        logger.warning("⚠️ Не удалось поставить нижние кнопки владельцу %s: %s", chat_id, e)


async def _notice(bot, chat_id: int, text: str, ttl: int = _NOTICE_TTL_SEC) -> None:
    """Короткий ответ очистки или переключателя — сам исчезает, как ответ /clear."""
    msg = await bot.send_message(chat_id=chat_id, text=text)
    if msg:
        schedule_delete(bot, chat_id, msg.message_id, ttl)


async def _run(code: str, context, chat_id: int, user_id: int, text: str = "") -> None:
    """
    Открыть раздел по коду кнопки. Функции — те же, что у кнопок /adm
    (router.py); берутся в момент нажатия: модули панелей тянут этот файл
    обратно через оглавление пакета. text — сама надпись: по ней r_art
    находит новость.
    """
    bot = context.bot
    if code == "adm":
        # «🎛 Админ-панель» с 18.10.2026 — и статистика: у владельца
        # send_stats_panel присылает сообщение с версией и цифрами и ставит
        # внизу кнопки бывшей статистики (подменю service), открытый раздел
        # убирает. Та же функция у /stats и у «⬅️ Назад» с экранов
        # обновлений и дайджеста.
        from .panel_main import send_stats_panel
        await send_stats_panel(bot, chat_id, user_id)
    elif code == "s_updates":
        from .panel_updates import send_updates_panel
        await send_updates_panel(bot, chat_id, user_id, 0)
    elif code == "s_backup":
        from .panel_main import send_db_backup
        await _notice(bot, chat_id, "⏳ Делаю копию базы…")
        await send_db_backup(bot, chat_id, user_id)
    elif code == "s_digest":
        from .panel_digest import send_digest_panel
        await send_digest_panel(bot, chat_id, user_id)
    elif code == "s_digest_auto":
        # Тумблера больше нет (20.10.2026): нажали старую кнопку с экрана —
        # говорим, что дайджест включён всегда, и ставим новое подменю.
        await _notice(bot, chat_id, DIGEST_ALWAYS_TEXT)
        await send_owner_keys(bot, chat_id, user_id, menu="service")
    elif code == "prompts":
        # Панель раздела — сама с нижними кнопками подменю (send_prompts_panel
        # у владельца зовёт send_owner_keys(menu="prompts")). Второй раз
        # send_owner_keys здесь НЕ звать — панель пришла бы дважды.
        from .panel_prompts import send_prompts_panel
        await send_prompts_panel(bot, chat_id, user_id)
    elif code in ("p_prompt", "p_ai_replies", "p_proactive", "p_hands"):
        from .panel_prompts import flip_prompts_toggle
        said, _ = await flip_prompts_toggle(code[2:], user_id, bot)
        await _notice(bot, chat_id, said)
        await send_owner_keys(bot, chat_id, user_id, menu="prompts")
    elif code in ("p_mm_dec", "p_mm_inc", "p_ctx_dec", "p_ctx_inc"):
        from .panel_prompts import step_proactive
        said = step_proactive(code[2:-4], 1 if code.endswith("inc") else -1)
        if said is None:
            # Упёрлись в предел — надписи те же, панель заново не нужна.
            await _notice(bot, chat_id, "Это крайнее значение.")
            return
        await _notice(bot, chat_id, said)
        await send_owner_keys(bot, chat_id, user_id, menu="prompts")
    elif code in ("p_mm_info", "p_ctx_info"):
        from .panel_prompts import proactive_step_info
        await _notice(bot, chat_id, proactive_step_info(code[2:-5]), _INFO_TTL_SEC)
    elif code == "p_stats":
        # Тот же экран итогов, что у кнопки под панелью; там он заменяет
        # панель на месте, отсюда — новым сообщением. Панель промптов при этом
        # остаётся: она держит нижние кнопки и в гигиену панелей не входит.
        # «⬅️ К промптам» на экране итогов присылает её заново.
        from .common import _send_panel_message
        from .panel_prompts import _build_proactive_stats_panel
        text, markup = _build_proactive_stats_panel()
        await _send_panel_message(bot, chat_id, text, markup)
    elif code == "p_files":
        from .panel_prompts import NO_PROMPT_FILES_TEXT, send_prompt_files
        if not await send_prompt_files(bot, chat_id, user_id):
            await _notice(bot, chat_id, NO_PROMPT_FILES_TEXT, 15)
    elif code == "api":
        # Панель раздела — как раньше, а внизу — его переключатели.
        from .panel_main import send_api_panel
        await send_api_panel(bot, chat_id, user_id)
        await send_owner_keys(bot, chat_id, user_id, menu="api")
    elif code in ("thoughts", "live_answer", "live_thoughts"):
        from .panel_main import flip_api_toggle
        await _notice(bot, chat_id, flip_api_toggle(code, user_id))
        await send_owner_keys(bot, chat_id, user_id, menu="api")
    elif code in ("batch_dec", "batch_inc"):
        from .panel_main import step_batch
        said = step_batch(user_id, 1 if code == "batch_inc" else -1)
        if said is None:
            # Упёрлись в предел — надписи те же, панель заново не нужна.
            await _notice(bot, chat_id, "Это крайнее значение.")
            return
        await _notice(bot, chat_id, said)
        await send_owner_keys(bot, chat_id, user_id, menu="api")
    elif code == "batch_info":
        from .panel_main import BATCH_INFO_TEXT
        await _notice(bot, chat_id, BATCH_INFO_TEXT, _INFO_TTL_SEC)
    elif code == "back":
        await send_owner_keys(bot, chat_id, user_id)
    elif code == "mod":
        # Панель раздела — сама с нижними кнопками подменю (send_mod_panel у
        # владельца зовёт send_owner_keys(menu="mod")). Второй раз
        # send_owner_keys здесь НЕ звать — панель пришла бы дважды.
        from .panel_mod import send_mod_panel
        await send_mod_panel(bot, chat_id, user_id)
    elif code in ("m_guard", "m_linkfilter", "m_greet", "m_captcha", "m_kick"):
        from .panel_mod import flip_mod_toggle
        await _notice(bot, chat_id, flip_mod_toggle(code[2:], user_id))
        await send_owner_keys(bot, chat_id, user_id, menu="mod")
    elif code in ("m_count_dec", "m_count_inc", "m_window_dec", "m_window_inc",
                  "m_mute_dec", "m_mute_inc", "m_time_dec", "m_time_inc"):
        from .panel_mod import step_mod
        said = step_mod(code[2:-4], 1 if code.endswith("inc") else -1, user_id)
        if said is None:
            # Упёрлись в предел — надписи те же, панель заново не нужна.
            await _notice(bot, chat_id, "Это крайнее значение.")
            return
        await _notice(bot, chat_id, said)
        await send_owner_keys(bot, chat_id, user_id, menu="mod")
    elif code in ("m_count_info", "m_window_info", "m_mute_info", "m_time_info"):
        from .panel_mod import mod_step_info
        await _notice(bot, chat_id, mod_step_info(code[2:-5]), _INFO_TTL_SEC)
    elif code == "m_clearlog":
        # Сначала вопрос: журнал стирается ЦЕЛИКОМ и вместе с уликами. «❗️ Да» —
        # та же ветка mod:clearlog_yes, что под панелью (у владельца она потом
        # убирает вопрос и присылает панель заново).
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        sent = await bot.send_message(
            chat_id=chat_id, parse_mode=ParseMode.HTML,
            text="🧹 <b>Очистить журнал модерации?</b>\n<i>Сотрутся все записи и "
                 "улики — вернуть их нельзя.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❗️ Да, очистить журнал", callback_data="mod:clearlog_yes"),
                InlineKeyboardButton("Отмена", callback_data="keys:cancel"),
            ]]))
        if sent:
            await register_and_clean_bot_message(bot, chat_id, sent.message_id)
    elif code == "m_evidence":
        from .panel_mod import evidence_list_view
        text, markup = evidence_list_view()
        sent = await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                                      reply_markup=markup)
        if sent:
            await register_and_clean_bot_message(bot, chat_id, sent.message_id)
    elif code == "users":
        from .panel_users import send_users_panel
        await send_users_panel(bot, chat_id, user_id)
    elif code == "rag":
        # У владельца панель — сама с нижними кнопками подменю (send_rag_panel
        # зовёт send_owner_keys(menu="rag")). Открывается всегда с разделов.
        from .panel_rag import send_rag_panel
        context.user_data["kb_screen"] = ""
        context.user_data["kb_page"] = 0
        await send_rag_panel(bot, chat_id, context)
    elif code == "quiz":
        # У владельца панель викторины — сама с нижними кнопками подменю
        # (send_quiz_panel зовёт send_owner_keys(menu="quiz")).
        from .panel_quiz import send_quiz_panel
        await send_quiz_panel(bot, chat_id, context)
    elif code == "q_auto":
        from .panel_quiz import flip_quiz_auto
        await _notice(bot, chat_id, flip_quiz_auto(user_id))
        await send_owner_keys(bot, chat_id, user_id, menu="quiz")
    elif code in ("q_drafts", "q_live"):
        # Карточка вопроса — отдельным сообщением с листанием под ним, как
        # из-под панели; «⬅️ К викторине» под ней присылает панель заново.
        from .panel_quiz import _show_card
        await _show_card(bot, chat_id, context, "draft" if code == "q_drafts" else "live", None)
    elif code == "q_noq":
        from .common import _send_panel_message
        from .panel_quiz import _build_noq_screen
        text, markup = _build_noq_screen()
        await _send_panel_message(bot, chat_id, text, markup)
    elif code == "q_seed":
        from .panel_quiz import seed_into_drafts
        said, refresh = seed_into_drafts(user_id)
        await _notice(bot, chat_id, said, _INFO_TTL_SEC)
        if refresh:
            await send_owner_keys(bot, chat_id, user_id, menu="quiz")
    elif code == "q_reseed":
        from .panel_quiz import reseed_from_file
        await _notice(bot, chat_id, reseed_from_file(user_id), _INFO_TTL_SEC)
        await send_owner_keys(bot, chat_id, user_id, menu="quiz")
    elif code in ("q_wipe", "q_nuke", "q_zero"):
        # Сначала вопрос отдельным сообщением: стирание не вернуть. «❗️ Да» —
        # та же ветка, что под панелью (quiz:*_yes), после неё панель приходит
        # заново, а вопрос убирает уборка панелей; «Отмена» — так же.
        from .panel_quiz import QUIZ_CONFIRM_TEXT, confirm_markup
        kind = code[2:]
        sent = await bot.send_message(chat_id=chat_id, parse_mode=ParseMode.HTML,
                                      text=QUIZ_CONFIRM_TEXT[kind],
                                      reply_markup=confirm_markup(kind))
        if sent:
            await register_and_clean_bot_message(bot, chat_id, sent.message_id)
    elif code in ("r_ground", "r_sub", "r_ship", "r_guide", "r_air", "r_other"):
        # Список статей раздела — отдельным сообщением, как из-под панели.
        from .panel_rag import send_kb_section
        if not await send_kb_section(bot, chat_id, context, code[2:]):
            await _notice(bot, chat_id, "Этот раздел уже пуст.")
            await send_owner_keys(bot, chat_id, user_id, menu="rag", context=context)
    elif code in ("r_pending", "r_pend_prev", "r_pend_next"):
        # Очередь «Ждут одобрения» — сама нижняя панель (19.10.2026, решение
        # Максима): заголовок с числом и по новости в ряд. Пуста — подменю базы
        # знаний (это решает send_owner_keys). Номер страницы подрезает
        # panel_rag.rag_pending_rows.
        was_open = context.user_data.get("kb_screen") == "pending"
        context.user_data["kb_screen"] = "pending"
        try:
            page = int(context.user_data.get("kb_page", 0)) if was_open else 0
        except (TypeError, ValueError):
            page = 0
        context.user_data["kb_page"] = {"r_pend_prev": page - 1, "r_pend_next": page + 1}.get(code, 0)
        await send_owner_keys(bot, chat_id, user_id, menu="rag_pending", context=context)
        if context.user_data.get("kb_screen") != "pending":
            await _notice(bot, chat_id, "Этот раздел уже пуст.")
    elif code == "r_art":
        # Новость из нижнего списка → её карточка; список остаётся внизу.
        from .panel_rag import send_pending_card
        if not await send_pending_card(bot, chat_id, context, text):
            await _notice(bot, chat_id, "Список устарел — открываю заново.")
            await send_owner_keys(bot, chat_id, user_id, menu="rag_pending", context=context)
    elif code in ("r_settings", "r_back"):
        context.user_data["kb_screen"] = "settings" if code == "r_settings" else ""
        context.user_data["kb_page"] = 0
        await send_owner_keys(bot, chat_id, user_id,
                              menu="rag_settings" if code == "r_settings" else "rag",
                              context=context)
    elif code == "r_kb":
        from .panel_rag import flip_kb_enabled
        await _notice(bot, chat_id, flip_kb_enabled(user_id))
        await send_owner_keys(bot, chat_id, user_id, menu="rag_settings", context=context)
    elif code in ("r_thr_dec", "r_thr_inc", "r_topk_dec", "r_topk_inc",
                  "r_margin_dec", "r_margin_inc"):
        from .panel_rag import kb_step_text, step_kb
        name = code[2:].rsplit("_", 1)[0]
        if step_kb(name, 1 if code.endswith("inc") else -1) is None:
            await _notice(bot, chat_id, "Это крайнее значение.")
            return
        await _notice(bot, chat_id, kb_step_text(name))
        await send_owner_keys(bot, chat_id, user_id, menu="rag_settings", context=context)
    elif code in ("r_thr_info", "r_topk_info", "r_margin_info"):
        from .panel_rag import kb_step_info
        await _notice(bot, chat_id, kb_step_info(code[2:-5]), _INFO_TTL_SEC)
    elif code == "r_test":
        from .panel_rag import KB_TEST_ON_TEXT, toggle_kb_test
        said = await toggle_kb_test(bot, chat_id, context)
        await _notice(bot, chat_id, said, _INFO_TTL_SEC if said == KB_TEST_ON_TEXT else _NOTICE_TTL_SEC)
        # Метка кнопки меняется — подменю то, где нажали: настройки, очередь
        # новостей (там «⏹️ Завершить проверку» тоже есть) или подменю базы.
        menu = {"settings": "rag_settings", "pending": "rag_pending"}.get(
            context.user_data.get("kb_screen"), "rag")
        await send_owner_keys(bot, chat_id, user_id, menu=menu, context=context)
    elif code == "r_add":
        from .panel_rag import start_kb_add
        await _notice(bot, chat_id, start_kb_add(context), _INFO_TTL_SEC)
    elif code in ("r_rebuild", "r_clearlog"):
        # Сначала вопрос отдельным сообщением; «❗️ Да» — та же ветка, что под
        # панелью (kb_rebuild_yes / kb_clearlog_yes), «Отмена» — kb_panel.
        from .panel_rag import KB_CONFIRM_TEXT, _kb_rebuild_running, kb_confirm_markup
        kind = code[2:]
        if kind == "rebuild" and _kb_rebuild_running(context):
            await _notice(bot, chat_id, "⏳ Пересборка уже идёт — дождитесь итога.")
            return
        sent = await bot.send_message(chat_id=chat_id, parse_mode=ParseMode.HTML,
                                      text=KB_CONFIRM_TEXT[kind], reply_markup=kb_confirm_markup(kind))
        if sent:
            await register_and_clean_bot_message(bot, chat_id, sent.message_id)
    elif code == "logs":
        from .common import send_logs_menu
        await send_logs_menu(bot, chat_id)
    elif code == "site":
        from telegram import InlineKeyboardMarkup
        from .panel_main import _web_row
        rows = _web_row()
        if not rows:
            await _notice(bot, chat_id, "🌐 Сайт не поднят или не настроен его адрес.")
            return
        sent = await bot.send_message(
            chat_id=chat_id, parse_mode=ParseMode.HTML,
            text="🌐 <b>Веб-админка</b>\n<i>Внутри Telegram — «🌐 Админка», "
                 "в настоящем браузере — ссылка на пять минут.</i>",
            reply_markup=InlineKeyboardMarkup(rows))
        if sent:
            await register_and_clean_bot_message(bot, chat_id, sent.message_id)
    elif code == "wipe":
        from .panel_prompts import wipe_memory
        await _notice(bot, chat_id, wipe_memory(user_id))
    elif code == "restart":
        # Сначала вопрос. «✅ Да» — та же ветка system_restart, что у кнопки
        # /adm (router.py): заберёт свежий код и перезапустит бота.
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        sent = await bot.send_message(
            chat_id=chat_id, parse_mode=ParseMode.HTML,
            text="🔄 <b>Перезапустить бота?</b>\n<i>На сервере он сначала заберёт "
                 "свежий код с GitHub.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, перезапустить", callback_data="system_restart"),
                InlineKeyboardButton("Отмена", callback_data="keys:restart_no"),
            ]]))
        if sent:
            await register_and_clean_bot_message(bot, chat_id, sent.message_id)


async def handle_keys_callback(query, data: str) -> None:
    """
    Кнопки под сообщениями нижней панели (router.py, ветка «keys:»): «Отмена»
    у вопроса о перезапуске (keys:restart_no) и у вопроса об очистке журнала
    модерации (keys:cancel, 19.10.2026) — вопрос убирается.
    """
    if data in ("keys:restart_no", "keys:cancel"):
        await query.answer("Отменено.")
        try:
            await query.message.delete()
        except Exception as e:
            logger.debug("⌨️ Не удалось убрать вопрос о перезапуске: %s", e)
        return
    await query.answer("Кнопка устарела.", show_alert=True)


async def handle_owner_key(update, context, text: str) -> bool:
    """
    Нажатие нижней кнопки владельца. Возвращает True, если это была она —
    тогда сообщение уже обработано и модели не уходит.

    Перехватывает ТОЛЬКО: личку, владельца и текст ровно как на кнопке. Как любая команда, гасит ожидание числа (экраны счетов и
    счёта викторины) и режимы панели базы знаний.
    """
    code = _code_of(text)
    is_news = (text or "").strip().startswith("📄 ")
    if not code and not is_news:
        return False
    chat, user = update.effective_chat, update.effective_user
    owner_here = chat is not None and user is not None and _is_owner_private(chat.id, user.id)
    if owner_here and is_news:
        # Новость из нижнего списка «Ждут одобрения» — раньше образцов:
        # «📄 Без типа (3)» могла бы оказаться и названием новости.
        from .panel_rag import pending_label_file
        if pending_label_file(context, text):
            code = "r_art"
    if not code or not owner_here:
        return False
    await delete_user_message_safe(update.message)
    for key in ("balance_edit", "quiz_edit", "kb_add_mode", "kb_replace_target"):
        context.user_data.pop(key, None)
    if code not in _KEEP_PENDING_KEYS:
        context.user_data.pop("kb_pending_keys", None)
    if code not in _KEEP_KB_TEST:
        from .panel_rag import _end_kb_test
        await _end_kb_test(context.bot, chat.id, context)
    logger.info("⌨️ Нижняя кнопка: %s | владелец %s", text.strip(), user.id)
    try:
        await _run(code, context, chat.id, user.id, text.strip())
    except Exception as e:
        logger.warning("⚠️ Нижняя кнопка «%s» не сработала: %s", text.strip(), e)
    return True
