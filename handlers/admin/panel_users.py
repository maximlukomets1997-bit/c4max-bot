# ───────────────────────────────────────────────
#  handlers/admin/panel_users.py — раздел «👥 Пользователи»: список всех, кого
#  знает бот, и карточка одного участника (личное дело, звание, лимиты,
#  персональные настройки).
#
#  Этап 1: только просмотр — панель ничего не меняет в поведении бота.
#  Персональные настройки уже ЧИТАЮТСЯ и показываются (таблица user_settings),
#  но задавать их будут кнопки этапа 2, а действия (мут/кик/бан) — этапа 3.
# ───────────────────────────────────────────────
import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import ADMIN_IDS, IMAGE_DAILY_LIMIT, MAX_CONTEXT_MESSAGES
from database.history import (
    list_known_users,
    get_known_chats,
    get_user_stats,
    get_history_length,
    get_user_usage,
    get_remaining_image_calls,
)
from utils import delete_user_message_safe

logger = logging.getLogger(__name__)
from .common import (_adm_back_row, _audit, _filter_keyboard, _fmt_mod_time, _is_group_chat,
                     _known_names, _notify_owners, _onoff, _require,
                     _send_panel_message, _staff_name)
from .panel_rag import _end_kb_test


# Сколько человек показывать кнопками. Потолок Telegram — ~100 кнопок на
# сообщение, служебных здесь 2, так что запас большой. Когда людей станет
# больше этого числа, понадобится листание (сейчас их 9).
_USERS_LIST_LIMIT = 40

# Значок «эту строку карточки бот пересказывает НЕЙРОСЕТИ» (2026-07-26, решение
# Максима). Помечены ровно те поля, что попадают в справку об авторе
# (services/gemini.py::_who_is_talking) — она уходит модели ТОЛЬКО в режиме
# «Сам в разговор». Меняешь состав справки — поменяй и расстановку значков,
# иначе карточка начнёт врать о том, что знает модель.
_BRAIN = "🧠"

# Месяцы в винительном падеже — для строки «Запросов к ИИ: N за июль».
# Счётчик обнуляется месячным сбросом (jobs.py::_monthly_stats_reset), поэтому
# без названия месяца цифра читалась как «за всё время» и вводила в заблуждение.
_MONTHS_ACC = ("январь", "февраль", "март", "апрель", "май", "июнь",
               "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


def _month_ru() -> str:
    """Текущий месяц по Киеву словом — тем же поясом, по которому идёт сброс."""
    from services.daily_report import kyiv_now
    try:
        return _MONTHS_ACC[kyiv_now().month - 1]
    except Exception:
        return "этот месяц"


# Регуляторы ➖/➕ карточки: короткий код кнопки → графа в user_settings.
# Короткий код нужен, чтобы callback_data влезала в 64 байта Telegram.
# Ниже минимума значение НЕ упирается, а обнуляется в «общую настройку бота» —
# это единственный способ вернуть человека на общие правила по одному полю.
#
# ⚠️ ШАГИ И ГРАНИЦЫ ТРЁХ ПЕРВЫХ БЕРУТСЯ ИЗ ОБЩЕЙ НАСТРОЙКИ
# (services/settings_spec.py), а не пишутся здесь. Иначе персональное значение
# можно было бы завести туда, куда общее завести нельзя, — а это тихая дыра:
# человеку выставили бы порог, недостижимый для всех остальных. До 30.08.2026
# тут стояла своя копия чисел с припиской «держим такими же» — ровно тот
# случай, когда две копии обязаны совпадать, но ничто этого не проверяет.
# Теперь совпадение обеспечено устройством, а не памятью.
_USER_FIELD_BY_CODE = {
    "count":  "antispam_msg_count",
    "window": "antispam_window_sec",
    "mute":   "antispam_mute_sec",
}


def _user_limit(code: str) -> dict:
    """Шаг и границы регулятора карточки: graфа user_settings, min/max/step."""
    if code == "img":
        # У лимита картинок общей настройки в settings нет — он приходит
        # константой config.IMAGE_DAILY_LIMIT, поэтому границы свои.
        # 0 = полный запрет картинок, это осмысленное значение, а не «сброс».
        return {"field": "image_limit", "step": 1, "min": 0, "max": 50}
    from services.settings_spec import SPEC
    item = SPEC[_USER_FIELD_BY_CODE[code]]
    return {"field": _USER_FIELD_BY_CODE[code], "step": item["step"],
            "min": item["min"], "max": item["max"]}


_USER_LIMITS = {code: _user_limit(code)
                for code in ("count", "window", "mute", "img")}

# Тумблеры карточки: код кнопки → графа в user_settings.
_USER_TOGGLES = {
    "immune": "antispam_immune",
    "links":  "links_allowed",
    "ai":     "ai_ignored",
}


def _base_value(code: str) -> int:
    """Общая (не персональная) величина настройки — от неё стартует регулятор."""
    from services.antispam import get_thresholds
    if code == "img":
        return IMAGE_DAILY_LIMIT
    count, window, mute = get_thresholds()
    return {"count": count, "window": window, "mute": mute}[code]


def _adjust_user_setting(user_id: int, code: str, delta: int):
    """
    Двигает персональную настройку на один шаг. Возвращает (новое значение,
    изменилось ли). Новое значение None означает «вернулся на общую настройку».
    Если ничего не изменилось (упёрлись в границу) — вызывающий код НЕ должен
    перерисовывать сообщение: Telegram отвечает ошибкой на перерисовку «в то же
    самое», и кнопка выглядит мёртвой.
    """
    from services.user_settings import get as _us_get, set_field
    lim = _USER_LIMITS[code]
    was = _us_get(user_id).get(lim["field"])

    # Настройка ещё общая — шагаем от общего значения, а не от нуля.
    cur = _base_value(code) if was is None else was
    new = cur + lim["step"] * delta
    if new < lim["min"]:
        new = None                      # ниже минимума = вернуть общую настройку
    elif new > lim["max"]:
        new = lim["max"]

    if new == was:
        return new, False
    set_field(user_id, lim["field"], new)
    return new, True


def _toggle_user_setting(user_id: int, code: str):
    """Переключает тумблер карточки. Возвращает новое значение для подсказки."""
    from services.user_settings import get as _us_get, set_field
    field = _USER_TOGGLES[code]
    new = 0 if _us_get(user_id).get(field) else 1
    set_field(user_id, field, new)
    return new


# ─── имя пользователя ───────────────────────────────────────────────

def _display_name(u: dict) -> str:
    """
    Человекочитаемое имя из того, что есть: имя → @ник → имя из викторины → ID.
    Возвращает СЫРОЙ текст (экранировать перед вставкой в HTML — обязательно).
    """
    return (u.get("first_name")
            or (f"@{u['username']}" if u.get("username") else "")
            or u.get("quiz_name")
            or str(u["user_id"]))


# ─── список пользователей ───────────────────────────────────────────

def _build_users_panel(viewer_id: int = 0, bot_id: int = 0):
    """Текст и клавиатура панели «👥 Пользователи» (список кнопками)."""
    # Сам бот попадает в список через архив групп — туда пишутся его собственные
    # реплики (без этого «Сам в разговор» не помнил бы, что уже говорил).
    # В списке ЛЮДЕЙ ему не место: настраивать в его карточке нечего, а кнопки
    # мута/кика/бана к нему применимы буквально. Берём с запасом +2, чтобы после
    # выкидывания бота список не стал на одного короче потолка.
    users = [u for u in list_known_users(_USERS_LIST_LIMIT + 2)
             if u["user_id"] != bot_id]
    hidden = max(0, len(users) - _USERS_LIST_LIMIT)
    users = users[:_USERS_LIST_LIMIT]

    text = (
        "👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n"
        "───────────────────────────\n"
        f"Известно боту: <b>{len(users) + hidden}</b> чел.\n"
        "Сортировка — по активности в группах.\n"
    )
    if hidden:
        text += f"\n<i>Скрыто ещё {hidden} — покажу, когда добавим листание.</i>\n"
    text += "\n<i>Нажми на человека, чтобы открыть карточку.</i>"

    from services import roles
    buttons = []
    for u in users:
        uid = u["user_id"]
        # Метка слева: роль важнее пометки о нарушениях.
        role = roles.role_of(uid)
        if role == "owner":
            mark = "👑 "
        elif role == "moderator":
            mark = "🛡 "
        elif u["mute_count"]:
            mark = "⚠️ "
        else:
            mark = ""
        name = _display_name(u)
        label = f"{mark}{name[:16]} · {u['msg_count']}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"usr:card:{uid}"))

    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    # Журнал персонала — надзорный инструмент; уровень владельца задан в
    # таблице прав (usr:slog), кнопку уберёт фильтр.
    keyboard.append([InlineKeyboardButton("📋 Журнал персонала",
                                          callback_data="usr:slog")])
    keyboard.append(_adm_back_row())
    return text, InlineKeyboardMarkup(_filter_keyboard(keyboard, viewer_id))


async def send_users_panel(bot, chat_id: int, viewer_id: int | None = None):
    """Отправляет панель со списком пользователей (кнопка в /adm и команда /users)."""
    text, markup = _build_users_panel(chat_id if viewer_id is None else viewer_id, bot.id)
    await _send_panel_message(bot, chat_id, text, markup)


# ─── журнал действий персонала (только владельцу) ───────────────────

# Коды действий журнала → как их показывать. Все коды перечислены ЯВНО:
# незнакомый покажется как есть, а не притворится чем-то другим.
_ACTION_TITLES = {
    "mute":       ("🔇", "мут"),
    "unmute":     ("🔓", "размут"),
    "kick":       ("👢", "кик"),
    "ban":        ("⛔", "бан"),
    "unban":      ("🔙", "разбан"),
    "setting":    ("⚙️", "настройка"),
    "reset":      ("↩️", "сброс настроек"),
    "clear_ctx":  ("🧹", "очистка диалога"),
    "reset_viol": ("🧾", "обнуление нарушений"),
    "rank":       ("🎖️", "почётное звание"),
    "role_on":    ("🛡", "НАЗНАЧЕН МОДЕРАТОРОМ"),
    "role_off":   ("🚫", "СНЯТ С МОДЕРАТОРОВ"),
    "perm":       ("🔑", "право"),
    "antispam":   ("🔧", "общие настройки"),
    # Приветствие новичков в панели /mod (2026-08-04): три тумблера и срок
    # проверки «я не бот».
    "greet":      ("👋", "приветствие новичков"),
    # Недельный дайджест группы в панели статистики (2026-08-04): тумблер
    # понедельничной отправки и ручная отправка текста в группу.
    "digest":     ("📊", "дайджест недели"),
    "modlog_clear": ("🧹", "ОЧИЩЕН ЖУРНАЛ МОДЕРАЦИИ"),
    "proactive_wipe": ("🗣", "БОТ ЗАБЫЛ РАЗГОВОРЫ"),
    "proactive_hands": ("🤚", "РУКИ БОТА (мут)"),
    # Экран «💰 Счета и квоты» панели API (2026-07-27): ручная правка остатка
    # на счету, квоты Qwen и — с 2026-08-17 — счётчика «потрачено». Все три
    # пишутся кодом "balance"; что именно менялось, сказано в подробности
    # записи («Потрачено DeepSeek: стало $2.87»).
    "balance":    ("💵", "счёт/квота"),
    # ⚠️ Кодом "cost_zero" бот больше НЕ пишет (обнуление с подтверждением
    # убрано 2026-08-17, ноль теперь вводится числом). Строка оставлена ради
    # старых записей: журнал персонала хранит 30 дней, и без неё они потеряли
    # бы человеческий заголовок. После 16.09.2026 можно удалить.
    "cost_zero":  ("♻️", "ОБНУЛЁН СЧЁТЧИК РАСХОДА"),
    # Тумблер «⬇️ САМООБНОВЛЕНИЕ» в /adm (2026-07-27): бот сам забирает
    # новый код с GitHub раз в 5 минут.
    "autoupdate": ("⬇️", "самообновление"),
    # Тумблер «🧠 МЫСЛИ ПОД КАПОТОМ» в панели промптов (2026-08-03): показ
    # свёрнутой цитаты с рассуждениями модели в ответах бота.
    "thoughts":   ("🧠", "мысли под капотом"),
    # Кнопки глубины раздумий в панели «📡 Настройки API» (2026-08-29) — по
    # одной на провайдера. ⚠️ Не путать с соседним "thoughts": тот про ПОКАЗ
    # мыслей, этот про то, сколько модель думает. Какому провайдеру что
    # выставили, сказано в подробности записи («глубина Gemini: Полно»).
    "thinking":   ("🌗", "глубина раздумий"),
    # Панель викторины /quizadm (2026-08-05): сборка вопросов по статьям базы
    # знаний и разбор черновиков. В игру идут только одобренные вопросы.
    # Тумблер «🕛 Вопрос дня» в панели викторины (2026-08-20; с 2026-08-23
    # сроков два — 12:00 и 18:00, часы в надписи собираются из расписания).
    "quiz_auto":     ("🕛", "вопрос дня"),
    "quiz_generate": ("🧠", "СОБРАНЫ ВОПРОСЫ ВИКТОРИНЫ"),
    "quiz_approve":  ("✅", "вопрос в игру"),
    "quiz_draft":    ("↩️", "вопрос в черновики"),
    "quiz_delete":   ("🗑", "удалён вопрос"),
    "quiz_wipe":     ("🧹", "ОЧИЩЕНЫ ЧЕРНОВИКИ ВИКТОРИНЫ"),
    "quiz_zero":     ("🧹", "ОБНУЛЕНА СТАТИСТИКА ВИКТОРИНЫ У ВСЕХ"),
    "quiz_retry":    ("🔁", "ПОВТОР НЕУДАЧНЫХ СТАТЕЙ"),
    "quiz_forget_fails": ("🗑", "забыт список неудачных статей"),
    # ⚠️ Эти два кода панель викторины пишет с 2026-08-05, а подписей у них
    # не было — журнал показывал «❔ quiz_nuke» и «❔ quiz_seed» голым кодом
    # (замечено 30.08.2026 при переносе викторины на сайт). Ровно те же
    # грабли, что с кодом «thinking» в v4.83.
    "quiz_nuke":     ("🗑", "СТЁРТЫ ВСЕ ВОПРОСЫ ВИКТОРИНЫ"),
    "quiz_seed":     ("📥", "загружен эталонный набор вопросов"),
    "quiz_reseed":   ("♻️", "банк догнан до эталонного файла"),
    # Тумблер «🚨 ТРЕВОГА (Днепр)» — функция УДАЛЕНА 2026-07-31 (просьба
    # Максима: больше не нужна). Подпись ОСТАВЛЕНА намеренно: в журнале
    # персонала ещё лежат записи о её включениях, и без строки они рисовались
    # бы значком ❔ и голым кодом. Уйдут сами, когда журнал состарится
    # (STAFF_LOG_DAYS) — тогда и эту строку можно убрать.
    "airalert":   ("🚨", "слежение за тревогой"),
}

_STAFF_LOG_LIMIT = 25       # сколько записей показывать на экране


def _build_staff_log_panel():
    """Текст и клавиатура экрана «📋 Журнал персонала»."""
    from database.history import get_recent_staff_actions, count_staff_actions
    from config import STAFF_LOG_DAYS

    rows = get_recent_staff_actions(_STAFF_LOG_LIMIT)
    week = count_staff_actions(7)
    # Имена собираем ОДНИМ проходом на весь экран: иначе на каждую из 25 строк
    # шёл бы отдельный скан таблиц.
    names = _known_names()

    text = ("📋 <b>ЖУРНАЛ ПЕРСОНАЛА</b>\n"
            "───────────────────────────\n"
            f"Действий за 7 дней: <b>{week}</b>\n"
            "───────────────────────────\n")

    if not rows:
        text += "<i>Пока пусто — персонал ничего не делал.</i>\n"
    else:
        for r in rows:
            icon, verb = _ACTION_TITLES.get(r["action"], ("❔", r["action"]))
            when = _fmt_mod_time(r["ts"])
            actor = html.escape((r.get("actor_name") or str(r["actor_id"]))[:24])
            line = f"{icon} <b>{when}</b> {actor} — {verb}"
            if r.get("target_id"):
                line += f" → {html.escape(_staff_name(r['target_id'], names)[:20])}"
            if r.get("details"):
                line += f" <i>({html.escape(r['details'][:60])})</i>"
            text += line + "\n"

    text += ("───────────────────────────\n"
             f"<i>Хранится {STAFF_LOG_DAYS} дней. Показаны последние "
             f"{_STAFF_LOG_LIMIT}.</i>")

    keyboard = [
        [InlineKeyboardButton("🧹 Очистить журнал", callback_data="usr:slogclear")],
        [InlineKeyboardButton("⬅️ К списку", callback_data="usr:list")],
        _adm_back_row(),
    ]
    return text, InlineKeyboardMarkup(keyboard)


# ─── карточка одного участника ──────────────────────────────────────

async def _chat_membership(bot, user_id: int) -> list[str]:
    """
    Строки «где человек состоит» по известным группам бота: участник, админ,
    вышел, забанен, под мутом (с временем окончания). Спрашивает Telegram,
    поэтому зовётся только при открытии карточки.

    Группы опрашиваются ВСЕ РАЗОМ (2026-07-27, ускорение): раньше запросы шли
    по очереди и карточка открывалась тем дольше, чем больше групп у бота —
    каждый запрос к Telegram это 100–300 мс, при шести группах почти две
    секунды ожидания. Теперь все ответы приходят за время одного запроса.
    Порядок строк сохраняется (asyncio.gather отдаёт результаты по порядку).
    Ошибки глушатся: недоступная группа просто не попадает в список.
    """
    import asyncio
    import datetime as _dt
    from database.history import remember_chat

    chats = get_known_chats()
    if not chats:
        return []

    async def _ask(chat: dict):
        """Название (если неизвестно) и статус участника в одной группе."""
        title = chat.get("title")
        # Название неизвестно (группа попала в список из старого архива) —
        # спрашиваем у Telegram и запоминаем, чтобы больше не спрашивать.
        if not title:
            try:
                info = await bot.get_chat(chat["chat_id"])
                title = info.title or ""
                if title:
                    remember_chat(chat["chat_id"], title)
            except Exception as e:
                logger.debug("👥 Не удалось узнать название чата %s: %s", chat["chat_id"], e)
        try:
            member = await bot.get_chat_member(chat["chat_id"], user_id)
        except Exception as e:
            logger.debug("👥 Не удалось узнать статус %s в чате %s: %s", user_id, chat["chat_id"], e)
            return None
        return (title or str(chat["chat_id"])), member

    # return_exceptions=True: падение одного запроса не должно ронять карточку.
    answers = await asyncio.gather(*(_ask(c) for c in chats), return_exceptions=True)

    lines = []
    for answer in answers:
        if not answer or isinstance(answer, BaseException):
            continue
        title, member = answer
        status = member.status
        if status == "creator":
            mark = "👑 владелец"
        elif status == "administrator":
            mark = "🛠 админ группы"
        elif status == "restricted":
            until = getattr(member, "until_date", None)
            if getattr(member, "can_send_messages", True):
                # Telegram помечает человека «restricted» и ПОСЛЕ снятия мута —
                # права возвращены, писать он может. Без приписки эта строка
                # читалась как «сидит в муте» (правка 2026-07-26).
                mark = "⚠️ ограничен Telegram (писать может)"
            elif until:
                mark = f"🔇 мут до {_dt.datetime.fromtimestamp(until.timestamp()):%d.%m %H:%M}"
            else:
                mark = "🔇 мут (бессрочно)"
        elif status == "left":
            mark = "🚪 не состоит"
        elif status == "kicked":
            mark = "⛔ забанен"
        else:
            mark = "✅ участник"
        lines.append(f"{html.escape(title)}: {mark}")
    return lines


def _settings_lines(user_id: int) -> list[str]:
    """
    Блок «персональные настройки» карточки. Пустое значение в user_settings
    означает «работает общая настройка бота» — так и пишем, чтобы всегда было
    видно, где «своё», а где «общее».
    """
    from services.antispam import get_thresholds
    from services.user_settings import get as _us_get
    # Читаем из ПАМЯТИ, а не из БД: именно этими значениями бот и пользуется,
    # так карточка не соврёт, даже если кэш и база почему-то разойдутся.
    us = _us_get(user_id)
    g_count, g_window, g_mute = get_thresholds()

    p_count = us.get("antispam_msg_count")
    p_window = us.get("antispam_window_sec")
    p_mute = us.get("antispam_mute_sec")
    own = any(v is not None for v in (p_count, p_window, p_mute))
    # Именно «is None», а не «or»: персональный ноль — это значение, а не пустота.
    count = g_count if p_count is None else p_count
    window = g_window if p_window is None else p_window
    mute = g_mute if p_mute is None else p_mute
    # Иммунитет: у персонала он есть ВСЕГДА по роли (antispam._is_protected
    # не мутит владельцев и модераторов), поэтому личный тумблер им ничего
    # не добавляет — так и пишем, чтобы строка не выглядела рычагом (2026-07-26).
    from services import roles
    if roles.is_staff(user_id):
        immune_line = "🟢 есть (по роли, тумблер не нужен)"
    else:
        immune_line = "🟢 есть" if us.get("antispam_immune") else "🔴 нет"

    # Ссылки: личное разрешение перекрывает ОБЩИЙ фильтр, а не работает само по
    # себе. Прежняя надпись «Ссылки разрешены: 🔴 нет» читалась как «ссылки
    # запрещены» — при выключенном общем фильтре это было прямой ложью.
    from database.history import get_setting as _get_setting
    filter_on = _get_setting("linkfilter_enabled", "0") == "1"
    if us.get("links_allowed"):
        links_line = "🟢 разрешены лично (общий фильтр не трогает)"
    else:
        links_line = (f"по общему правилу (фильтр {'🟢 включён' if filter_on else '🔴 выключен'})")

    lines = [
        f"├ Антифлуд: <b>{count}</b> сообщ. за <b>{window}</b> сек · мут <b>{mute}</b> сек "
        f"{'(своё)' if own else '(общее)'}",
        f"├ Иммунитет от мута: {immune_line}",
        f"├ Ссылки: {links_line}",
        f"├ Ответы ИИ: {'🔴 игнорирует' if us.get('ai_ignored') else '🟢 отвечает'}",
    ]

    img_limit = us.get("image_limit")
    if user_id in ADMIN_IDS:
        lines.append("├ Лимит картинок: <b>∞</b> (админ бота)")
    else:
        lines.append(f"├ Лимит картинок: <b>{img_limit if img_limit is not None else IMAGE_DAILY_LIMIT}</b>"
                     f" {'(своё)' if img_limit is not None else '(общее)'}")

    # 🧠: почётное звание уходит модели в справке об авторе — в отличие от
    # остальных персональных настроек, которые она не видит вовсе.
    # Значок ставится, ТОЛЬКО когда звание задано: не заданное модели не уходит
    # вовсе, и 🧠 у пустой строки обещал бы то, чего нет.
    honorary = us.get("honorary_rank")
    lines.append(f"└ Почётное звание: {html.escape(honorary) + ' ' + _BRAIN if honorary else '<i>не задано</i>'}")

    note = us.get("note")
    if note:
        lines.append(f"\n📝 <i>{html.escape(note)}</i>")
    return lines


# Ручные действия карточки. needs_chat — действию нужна конкретная группа
# (Telegram не умеет «глобальный бан»); confirm — спрашиваем подтверждение
# (необратимо для человека); duration — сначала выбрать срок.
_ACTIONS = {
    "mute":   {"label": "🔇 Мут",    "confirm": False, "duration": True},
    "unmute": {"label": "🔓 Размут", "confirm": False, "duration": False},
    "kick":   {"label": "👢 Кик",    "confirm": True,  "duration": False},
    "ban":    {"label": "⛔ Бан",    "confirm": True,  "duration": False},
    "unban":  {"label": "🔙 Разбан", "confirm": False, "duration": False},
}

# Сроки ручного мута (секунды → надпись на кнопке).
_MUTE_PRESETS = [(300, "5 мин"), (1800, "30 мин"), (3600, "1 час"),
                 (21600, "6 час"), (86400, "1 сутки")]


def _max_mute_for(actor_id: int):
    """
    Потолок срока мута: None — без ограничений (владелец), иначе секунды
    (модератор, config.MODERATOR_MAX_MUTE_SEC). Более длинные наказания —
    решение владельца.
    """
    from services.roles import is_owner
    from config import MODERATOR_MAX_MUTE_SEC
    return None if is_owner(actor_id) else MODERATOR_MAX_MUTE_SEC


def _actions_keyboard(user_id: int):
    """
    Кнопки ручных действий над участником — ПОЛНЫМ составом. Кто что увидит,
    решает `_filter_keyboard` по таблице прав (см. _build_user_card): второго
    списка «кому что показывать» в проекте нет намеренно.
    """
    return [
        [
            InlineKeyboardButton("🔇 Мут", callback_data=f"usr:pick:{user_id}:mute"),
            InlineKeyboardButton("🔓 Размут", callback_data=f"usr:pick:{user_id}:unmute"),
        ],
        [
            InlineKeyboardButton("👢 Кик", callback_data=f"usr:pick:{user_id}:kick"),
            InlineKeyboardButton("⛔ Бан", callback_data=f"usr:pick:{user_id}:ban"),
            InlineKeyboardButton("🔙 Разбан", callback_data=f"usr:pick:{user_id}:unban"),
        ],
        [
            InlineKeyboardButton("🧹 Очистить диалог", callback_data=f"usr:clr:{user_id}"),
            InlineKeyboardButton("🧾 Обнулить нарушения", callback_data=f"usr:viol:{user_id}"),
        ],
        [InlineKeyboardButton("🎖️ Почётное звание", callback_data=f"usr:rank:{user_id}")],
    ]


def _role_lines(target_id: int) -> list[str]:
    """
    Строка роли в тексте карточки. У модератора — сводка «прав N из 4»;
    сами галочки живут на отдельном экране «⚙️Настройки Прав Модератора», чтобы одно
    и то же не показывалось в двух местах.
    """
    from services import roles
    role = roles.role_of(target_id)
    if role == "owner":
        # 🧠: справка об авторе называет модели владельца по нику (решение
        # Максима 2026-07-23). У модератора и обычного участника роль модели
        # не сообщается — там значка нет.
        return [f"👑 <b>Владелец</b> — полный доступ, снять нельзя {_BRAIN}"]
    if role == "user":
        return ["🙍 Обычный участник"]

    granted = sum(1 for on in roles.perms_of(target_id).values() if on)
    # 🧠: с 2026-07-26 справка сообщает модели и о модераторе — чтобы бот
    # не грозил мутом персоналу, которого всё равно не тронет.
    return [f"🛡 <b>Модератор</b> · прав: <b>{granted}</b> из {len(roles.PERMS)} {_BRAIN}"]


def _role_keyboard(target_id: int):
    """
    Кнопки управления ролью. Модератору они не достанутся: ветки usr:role,
    usr:perm и usr:rights помечены в таблице прав уровнем владельца, и фильтр
    клавиатуры их уберёт.

    У действующего модератора здесь одна кнопка — вход на экран прав; галочки
    и снятие модератора переехали туда (2026-07-20).
    """
    from services import roles
    if roles.is_owner(target_id):
        # Владельца из панели не трогаем — он задан в config.py. Это НЕ про
        # права смотрящего, а про цель, поэтому проверка остаётся здесь.
        return []

    if roles.is_moderator(target_id):
        return [[InlineKeyboardButton("⚙️Настройки Прав Модератора",
                                      callback_data=f"usr:rights:{target_id}")]]
    return [[InlineKeyboardButton("🛡 Сделать модератором",
                                  callback_data=f"usr:role:{target_id}:on")]]


# ─── экран «⚙️Настройки Прав Модератора» ────────────────────────────

async def _show_rights(query, target_id: int):
    """
    Экран прав модератора: галочки, пояснение к каждому праву и снятие
    модератора. Заменяет карточку в ТОМ ЖЕ сообщении — как экраны мута и бана.

    Зовётся и при открытии, и после каждой галочки: права накликиваются
    подряд, не выбрасывая владельца обратно в карточку.
    """
    from services import roles
    perms = roles.perms_of(target_id)
    granted = sum(1 for on in perms.values() if on)

    text = (
        "⚙️ <b>ПРАВА МОДЕРАТОРА</b>\n"
        "───────────────────────────\n"
        f"👤 <b>{html.escape(_target_name(target_id))}</b>\n"
        f"🆔 <code>{target_id}</code>\n"
        f"Выдано: <b>{granted}</b> из {len(roles.PERMS)}\n"
        "───────────────────────────\n"
    )
    # Дерево ├ │ └ — как в блоках карточки участника. Подпись под каждым правом
    # берётся из roles.PERMS (`hint`) и обязана отвечать тому, что кнопки
    # реально позволяют: владелец решает по ней, что выдавать.
    items = list(roles.PERMS.items())
    for i, (code, cfg) in enumerate(items):
        last = i == len(items) - 1
        text += (f"{'└' if last else '├'} {'✅' if perms[code] else '⬜'} <b>{cfg['title']}</b>\n"
                 f"{' ' if last else '│'}      <i>{cfg['hint']}</i>\n")
    text += ("───────────────────────────\n"
             "<i>«⚙️ Правка карточек» без «👥 Карточек» не работает — бот выдаёт "
             "и снимает их вместе.</i>")

    rows, pair = [], []
    for code, cfg in roles.PERMS.items():
        pair.append(InlineKeyboardButton(f"{'✅' if perms[code] else '⬜'} {cfg['title']}",
                                         callback_data=f"usr:perm:{target_id}:{code}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("🚫 Снять модератора",
                                      callback_data=f"usr:role:{target_id}:off")])
    rows.append([InlineKeyboardButton("⬅️ К карточке",
                                      callback_data=f"usr:cancel:{target_id}")])

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        logger.debug("🛡 Не удалось показать экран прав %s: %s", target_id, e)


def _settings_keyboard(user_id: int):
    """
    Кнопки управления персональными настройками в карточке: четыре регулятора
    ➖/➕ (на средней кнопке — живое значение и пометка «своё»/«общ.») и четыре
    тумблера. Ряд лимита картинок админам бота не показываем: у них безлимит,
    и регулятор противоречил бы тексту карточки.
    """
    from services.user_settings import get as _us_get
    us = _us_get(user_id)
    rows = []

    regulators = [
        ("count",  "💬 порог"),
        ("window", "⏱ окно"),
        ("mute",   "🔇 мут"),
        ("img",    "🎨 картинки"),
    ]
    for code, title in regulators:
        if code == "img" and user_id in ADMIN_IDS:
            continue
        val = us.get(_USER_LIMITS[code]["field"])
        shown = _base_value(code) if val is None else val
        mark = "общ." if val is None else "своё"
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"usr:set:{user_id}:{code}:dec"),
            InlineKeyboardButton(f"{title}: {shown} {mark}", callback_data="usr:noop"),
            InlineKeyboardButton("➕", callback_data=f"usr:set:{user_id}:{code}:inc"),
        ])

    # Метки — общий _onoff (единый стиль кнопок, 2026-07-20). У «ИИ» ВКЛ
    # означает «бот отвечает», ВЫКЛ — «игнорирует»; расшифровка осталась
    # в тексте карточки. (Тумблер «Проверенный» с тремя состояниями удалён
    # 2026-07-20 — статус присваивается только автоматикой.)
    immune = _onoff(us.get("antispam_immune"))
    links = _onoff(us.get("links_allowed"))
    ai = _onoff(not us.get("ai_ignored"))
    rows.append([
        InlineKeyboardButton(f"🛡 Иммунитет: {immune}", callback_data=f"usr:tog:{user_id}:immune"),
        InlineKeyboardButton(f"🔗 Ссылки: {links}", callback_data=f"usr:tog:{user_id}:links"),
        InlineKeyboardButton(f"🤖 ИИ: {ai}", callback_data=f"usr:tog:{user_id}:ai"),
    ])
    if us:
        # Кнопка нужна, только если персональное вообще что-то задано.
        rows.append([InlineKeyboardButton("↩️ Сбросить всё персональное",
                                          callback_data=f"usr:reset:{user_id}")])
    return rows


async def _build_user_card(bot, user_id: int, viewer_id: int):
    """
    Текст и клавиатура карточки участника. Клавиатура собирается ПО ПРАВАМ
    смотрящего (viewer_id): модератор без права на бан не увидит кнопок бана,
    без права правки — регуляторов настроек, а управление ролями видит только
    владелец.
    """
    from services.antispam import trust_info

    # Имя ищем в общем списке — там оно собрано из личного дела, архива групп
    # и викторины. Если человека там нет (например, стёрли базу) — покажем ID.
    info = next((u for u in list_known_users(1000) if u["user_id"] == user_id),
                {"user_id": user_id, "username": "", "first_name": "",
                 "quiz_name": "", "msg_count": 0, "mute_count": 0, "link_count": 0})
    name = html.escape(_display_name(info))
    # 🧠: имя и ник — то, чем справка представляет человека модели (2026-07-26).
    nick = f" (@{html.escape(info['username'])})" if info.get("username") else ""
    nick += f" {_BRAIN}"

    # Строка статуса доверия («✅ Проверенный боец» / «⚠️ Взыскание» /
    # «⏳ Испытательный срок») УБРАНА 2026-07-26 по решению Максима: с 20 июля
    # статус ни на что не влияет (поблажку в антифлуде тогда отменили), а стаж
    # и число сообщений строкой выше говорят о человеке то же самое.
    # trust_info по-прежнему нужен — из него берутся сами цифры.
    ti = trust_info(user_id)

    stats = get_user_stats(user_id)
    rate = f" · {stats['success_rate']}%" if stats["total_attempts"] else ""
    usage = get_user_usage(user_id)
    ctx_len = get_history_length(user_id)
    img_left = get_remaining_image_calls(user_id, IMAGE_DAILY_LIMIT)
    # «осталось N из M», а не «N из M»: прежняя надпись читалась наоборот —
    # как «потрачено N из M» (правка 2026-07-26 по замечанию Максима).
    img_line = ("∞ (админ бота)" if user_id in ADMIN_IDS
                else f"осталось {img_left} из {IMAGE_DAILY_LIMIT}")

    # Звание: почётное (если назначено) перекрывает заработанное — ровно как
    # это видит сам человек в /rank. Заработанное показываем строкой ниже,
    # чтобы админ видел и то, и другое.
    from services.user_settings import honorary_rank
    from config import QUIZ_RANKS
    hon = honorary_rank(user_id)
    if hon:
        found = next((r for r in QUIZ_RANKS if r["name"] == hon), None)
        rank_line = f"{found['icon'] if found else '🏅'} {html.escape(hon)} 🏅 <i>(почётное)</i>"
        quiz_line = (f"└ В викторине заработано: {stats['rank_icon']} {html.escape(stats['rank'])} · "
                     f"<b>{stats['correct_answers']}</b> верных из <b>{stats['total_attempts']}</b>{rate}")
    else:
        rank_line = f"{stats['rank_icon']} {html.escape(stats['rank'])}"
        quiz_line = (f"└ Викторина: <b>{stats['correct_answers']}</b> верных "
                     f"из <b>{stats['total_attempts']}</b>{rate}")

    # Строка викторины прячется, если человек в неё не играл: «0 верных из 0»
    # ни о чём не говорит, а таких участников большинство (решение Максима
    # 2026-07-26). Почётное звание при этом всё равно видно строкой выше.
    quiz_block = f"{quiz_line}\n" if stats["total_attempts"] else ""

    text = (
        f"👤 <b>{name}</b>{nick}\n"
        f"🆔 <code>{user_id}</code>\n"
        + "\n".join(_role_lines(user_id)) + "\n"
        "───────────────────────────\n"
        "🪪 <b>Служба в гарнизоне</b>\n"
        f"├ Стаж: <b>{ti['days']}</b> дн. · сообщений в группах: <b>{ti['msgs']}</b>\n"
        f"└ Нарушения: мутов <b>{ti['mutes']}</b> · ссылок <b>{ti['links']}</b>\n"
        "───────────────────────────\n"
        f"🎖️ <b>Звание:</b> {rank_line}\n"
        f"{quiz_block}"
        "───────────────────────────\n"
        "🤖 <b>Общение с ботом</b>\n"
        f"├ Контекст диалога: <b>{ctx_len}</b> из {MAX_CONTEXT_MESSAGES} сообщ.\n"
        f"├ Запросов к ИИ: <b>{usage['total_requests']}</b> за {_month_ru()}\n"
        f"└ Картинки: <b>{img_line}</b>\n"
        "───────────────────────────\n"
    )

    membership = await _chat_membership(bot, user_id)
    if membership:
        text += "👥 <b>Группы</b>\n"
        for i, line in enumerate(membership):
            text += f"{'└' if i == len(membership) - 1 else '├'} {line}\n"
        text += "───────────────────────────\n"

    text += "⚙️ <b>Персональные настройки</b>\n" + "\n".join(_settings_lines(user_id))

    # ── Справка для нейросети НА ЭТОГО человека (2026-07-26, просьба Максима) ──
    # Тот же текст, что показывает панель промптов, но собранный на владельца
    # карточки: видно дословно, что бот расскажет модели именно об этом
    # участнике. Источник — services/gemini.py::author_brief, тот же, что уходит
    # модели: карточка и запрос разъехаться не могут.
    # Цитата ОБЫЧНАЯ (<blockquote> без expandable) — решение Максима: справка
    # короткая, разворачивать нечего.
    # Имя внутри справки приходит из Telegram, поэтому экранируем: символ «<»
    # в нике сломал бы отправку всей карточки.
    try:
        from services.gemini import author_brief
        brief = author_brief(user_id) or ""
    except Exception as e:
        logger.debug("👥 Не удалось собрать справку для карточки %s: %s", user_id, e)
        brief = ""
    if brief:
        safe_brief = (brief.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        brief_block = f"<blockquote>{safe_brief}</blockquote>"
    else:
        brief_block = "<i>(не собирается — бот не знает ни имени, ни ника)</i>"

    text += (f"\n───────────────────────────\n"
             f"{_BRAIN} <b>СПРАВКА ДЛЯ НЕЙРОСЕТИ</b>\n"
             f"{brief_block}\n"
             f"<i>Собирается из строк с {_BRAIN}. Уходит модели в режиме «Сам "
             f"в разговор» и когда к боту обращаются в группе. В личке — нет.</i>")

    # Клавиатура собирается ПОЛНОЙ, а лишнее отсекает фильтр по таблице прав:
    # у модератора с «⚙️ Правкой карточек» останутся картинки, мут/размут,
    # обнуление нарушений и звание, у владельца — всё.
    keyboard = (_settings_keyboard(user_id)
                + _actions_keyboard(user_id)
                + _role_keyboard(user_id)
                + [[InlineKeyboardButton("⬅️ К списку", callback_data="usr:list")],
                   _adm_back_row()])
    return text, InlineKeyboardMarkup(_filter_keyboard(keyboard, viewer_id))


async def send_user_card(bot, chat_id: int, user_id: int, viewer_id: int | None = None):
    """Отправляет карточку участника. viewer_id по умолчанию = chat_id
    (карточка открывается в личке, где это одно и то же число)."""
    text, markup = await _build_user_card(bot, user_id, chat_id if viewer_id is None else viewer_id)
    await _send_panel_message(bot, chat_id, text, markup)


# ─── роутер кнопок раздела ──────────────────────────────────────────

async def _redraw_card(query, bot, target_id: int):
    """
    Перерисовывает карточку прямо в сообщении (после смены настройки).
    Текст собирается заново целиком — вместе с живым опросом Telegram о
    членстве в группах. Это пара запросов на нажатие, но карточка обязана
    оставаться правдивой, а групп у бота единицы.
    """
    text, markup = await _build_user_card(bot, target_id, query.from_user.id)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception as e:
        logger.debug("👥 Не удалось обновить карточку %s: %s", target_id, e)


def _target_name(target_id: int) -> str:
    """Имя участника (сырое, без HTML) — для журнала модерации и сообщений."""
    info = next((u for u in list_known_users(1000) if u["user_id"] == target_id), None)
    return _display_name(info) if info else str(target_id)


def _target_label(target_id: int) -> str:
    """Имя с id для логов."""
    return f"{_target_name(target_id)} ({target_id})"


async def _sync_staff_menu(bot, user_id: int, is_staff_now: bool) -> None:
    """
    Обновляет меню команд Telegram в личке человека сразу после выдачи или
    снятия прав модератора: назначенному добавляется строка /adm, снятому —
    убирается. Без этого он увидел бы изменения только после перезапуска бота.
    Тихая: у того, кто не открывал личку с ботом, меню не ставится — не беда.
    """
    from telegram import BotCommand, BotCommandScopeChat
    # ⚠️ Список публичных команд — ОДИН на весь проект (handlers/commands.py).
    # Своей копии здесь быть не должно: она уже успела разъехаться с меню при
    # старте — у модератора оставался /subscribe, убранный из общего списка,
    # да ещё и с другими подписями у всех команд (2026-08-04).
    from handlers.commands import public_commands
    public = public_commands()
    cmds = ([BotCommand("adm", "Панель модератора")] + public) if is_staff_now else public
    try:
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=user_id))
    except Exception as e:
        logger.debug("🛡 Меню команд для %s не обновлено: %s", user_id, e)


def _chat_title(chat_id: int) -> str:
    """Название группы по id (или сам id, если название ещё не известно)."""
    for c in get_known_chats():
        if c["chat_id"] == chat_id:
            return c.get("title") or str(chat_id)
    return str(chat_id)


async def _screen(query, title: str, rows: list, target_id: int):
    """
    Промежуточный экран действия (выбор группы, срока, подтверждение) — заменяет
    карточку в ТОМ ЖЕ сообщении. Внизу всегда «Отмена», возвращающая карточку.
    """
    rows = list(rows) + [[InlineKeyboardButton("⬅️ Отмена", callback_data=f"usr:cancel:{target_id}")]]
    try:
        await query.edit_message_text(title, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        logger.debug("👥 Не удалось показать экран действия: %s", e)


async def _action_step(query, context, target_id: int, act: str, chat_id: int, admin_id: int):
    """
    Второй шаг действия: выбор срока (мут), подтверждение (кик/бан) или
    немедленное выполнение (размут, разбан — они восстанавливают права,
    спрашивать не о чем).

    ВАЖНО: на кнопку отвечает ЭТА функция (или _run_action внутри неё), а не
    вызывающий код. Ответить дважды нельзя — Telegram вернёт ошибку, карточка
    не перерисуется, а кнопка будет выглядеть зависшей.
    """
    cfg = _ACTIONS[act]
    who = html.escape(_target_name(target_id))
    where = html.escape(_chat_title(chat_id))

    if cfg["duration"]:
        await query.answer()
        limit = _max_mute_for(admin_id)
        rows, pair = [], []
        for sec, lbl in _MUTE_PRESETS:
            if limit is not None and sec > limit:
                continue      # модератору длинные сроки не показываем
            pair.append(InlineKeyboardButton(lbl, callback_data=f"usr:do:{target_id}:{act}:{chat_id}:{sec}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        await _screen(query, f"🔇 <b>Мут: {who}</b>\nГруппа: {where}\n\nНа какой срок?", rows, target_id)
        return

    if cfg["confirm"]:
        await query.answer()
        warn = ("Человек сможет вернуться в группу сам." if act == "kick"
                else "Вернуться сам он НЕ сможет — только через «Разбан».")
        rows = [[InlineKeyboardButton(f"✅ Да, {cfg['label'].split()[1].lower()}",
                                      callback_data=f"usr:do:{target_id}:{act}:{chat_id}:0")]]
        await _screen(query, f"{cfg['label']}: <b>{who}</b>\nГруппа: {where}\n\n⚠️ {warn}\n\nПодтверждаешь?",
                      rows, target_id)
        return

    await _run_action(query, context, target_id, act, chat_id, 0, admin_id)


async def _run_action(query, context, target_id: int, act: str, chat_id: int,
                      arg: int, admin_id: int):
    """Выполняет ручное действие и перерисовывает карточку с итогом."""
    from services import antispam
    from utils import mention

    bot = context.bot
    admin_name = mention(query.from_user)
    name = _target_name(target_id)

    if act == "mute":
        # Потолок срока для модератора проверяется ЗДЕСЬ, а не только при
        # отрисовке кнопок: нажатие старой кнопки с длинным сроком не должно
        # проходить мимо лимита.
        limit = _max_mute_for(admin_id)
        if limit is not None and arg > limit:
            await query.answer(f"⛔ Модератор может мутить максимум на {limit // 3600} ч.",
                               show_alert=True)
            return
        err = await antispam.mute_user(bot, chat_id, target_id, arg, name, admin_name,
                                       actor_id=admin_id)
        done = f"🔇 Мут выдан на {arg // 60} мин."
    elif act == "unmute":
        ok = await antispam.unmute(bot, chat_id, target_id, name, admin_name)
        err = "" if ok else "Не удалось снять мут — проверь права бота в группе."
        done = "🔓 Мут снят."
    elif act == "kick":
        err = await antispam.kick_user(bot, chat_id, target_id, name, admin_name,
                                       actor_id=admin_id)
        done = "👢 Выгнан из группы."
    elif act == "ban":
        err = await antispam.ban_user(bot, chat_id, target_id, name, admin_name,
                                      actor_id=admin_id)
        done = "⛔ Забанен."
    elif act == "unban":
        err = await antispam.unban_user(bot, chat_id, target_id, name, admin_name,
                                        actor_id=admin_id)
        done = "🔙 Бан снят."
    else:
        await query.answer("❌ Неизвестное действие.", show_alert=True)
        return

    if err:
        # Всплывающее окно Telegram — максимум 200 символов, режем с запасом.
        await query.answer(f"❌ Не вышло: {err}"[:190], show_alert=True)
        await _redraw_card(query, bot, target_id)
        return

    await query.answer(done, show_alert=False)

    # Журнал персонала — только после успеха: неудачная попытка действием не была.
    where = _chat_title(chat_id)
    detail = f"на {arg // 60} мин · {where}" if act == "mute" else where
    _audit(admin_id, act, target_id, detail)

    # Тяжёлые действия модератора — владельцу в личку. Свои действия владельцу
    # не шлём: уведомлять человека о том, что он только что сделал сам, незачем.
    from services import roles
    if act in ("kick", "ban", "unban") and not roles.is_owner(admin_id):
        titles = {"kick": "👢 выгнал", "ban": "⛔ забанил", "unban": "🔙 разбанил"}
        await _notify_owners(
            bot,
            f"🛡 <b>Действие модератора</b>\n"
            f"{html.escape(_staff_name(admin_id))} {titles[act]} "
            f"<b>{html.escape(name)}</b>\n"
            f"Группа: {html.escape(where)}"
        )

    await _redraw_card(query, bot, target_id)


async def _handle_users_callback(query, context, data: str, chat_id: int, admin_id: int):
    """
    Ветки usr:… раздела «Пользователи». Гейт ADMIN_IDS уже пройден выше
    по стеку (в handle_callback_query).
      usr:list                     — вернуться к списку
      usr:card:<id>                — открыть карточку участника
      usr:set:<id>:<код>:inc|dec   — регулятор персональной настройки
      usr:tog:<id>:<код>           — тумблер персональной настройки
      usr:reset:<id>               — спросить подтверждение сброса
      usr:resetok:<id>             — сбросить все персональные настройки
      usr:cancel:<id>              — отмена (вернуть карточку)
      usr:pick:<id>:<действие>     — ручное действие: выбор группы
      usr:step:<id>:<действие>:<чат>        — срок мута / подтверждение
      usr:do:<id>:<действие>:<чат>:<аргум.> — выполнить действие
      usr:clr:<id> / usr:clrgo:<id>         — очистка истории диалога
      usr:viol:<id> / usr:violgo:<id>       — обнуление нарушений
      usr:rank:<id> / usr:rankset:<id>:<№>  — почётное звание (№ -1 = убрать)
      usr:rights:<id>              — экран «⚙️Настройки Прав Модератора»
      usr:role:<id>:on|off|offgo   — назначить / спросить / снять модератора
      usr:perm:<id>:<код>          — галочка права (на экране прав)
      usr:noop                     — средняя кнопка регулятора (значение)
    """
    parts = data.split(":")
    section = parts[1] if len(parts) > 1 else ""

    if section == "noop":
        await query.answer()
        return

    if section == "list":
        await query.answer()
        await send_users_panel(context.bot, chat_id, admin_id)
        return

    # Журнал персонала (в таблице прав помечен уровнем владельца).
    if section == "slog":
        await query.answer()
        text, markup = _build_staff_log_panel()
        await _send_panel_message(context.bot, chat_id, text, markup)
        return

    if section == "slogclear":
        from database.history import clear_staff_log
        deleted = clear_staff_log()
        logger.info("📋 Владелец %s очистил журнал персонала (%d записей)", admin_id, deleted)
        await query.answer(f"🧹 Журнал очищен: {deleted} записей.", show_alert=True)
        text, markup = _build_staff_log_panel()
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug("📋 Не удалось обновить журнал персонала: %s", e)
        return

    # Все остальные ветки адресные — во второй части id участника.
    try:
        target_id = int(parts[2])
    except (IndexError, ValueError):
        await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
        return

    # Карточка самого бота недоступна никому, включая владельца: из списка он
    # убран, но кнопка могла остаться в старом, ещё не затёртом сообщении —
    # а в карточке живые «👢 Кик» и «⛔ Бан».
    if target_id == context.bot.id:
        await query.answer("⛔ Карточка бота недоступна.", show_alert=True)
        return

    # Карточки персонала (владелец, другие модераторы) модератору недоступны —
    # ни смотреть, ни трогать. Проверка ОДНА на все ветки ниже.
    from services import roles
    if not roles.is_owner(admin_id) and roles.is_staff(target_id) and admin_id != target_id:
        await query.answer("⛔ Карточки владельца и других модераторов недоступны.",
                           show_alert=True)
        return

    if section == "card":
        await query.answer()
        await send_user_card(context.bot, chat_id, target_id, admin_id)
        return

    if section == "set":
        code = parts[3] if len(parts) > 3 else ""
        action = parts[4] if len(parts) > 4 else ""
        if code not in _USER_LIMITS or action not in ("inc", "dec"):
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return
        new_val, changed = _adjust_user_setting(target_id, code, 1 if action == "inc" else -1)
        if not changed:
            # Упёрлись в границу: перерисовка «в то же самое» — ошибка Telegram.
            await query.answer("Дальше некуда — значение на границе.", show_alert=False)
            return
        logger.info("👥 Админ %s: %s → %s = %s", admin_id, _target_label(target_id),
                    _USER_LIMITS[code]["field"],
                    "общая настройка" if new_val is None else new_val)
        _audit(admin_id, "setting", target_id,
               f"{code} = {'общая настройка' if new_val is None else new_val}")
        await query.answer("↩️ Вернул общую настройку" if new_val is None else f"Новое значение: {new_val}")
        await _redraw_card(query, context.bot, target_id)
        return

    if section == "tog":
        code = parts[3] if len(parts) > 3 else ""
        if code not in _USER_TOGGLES:
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return
        new_val = _toggle_user_setting(target_id, code)
        titles = {
            "immune": ("Иммунитет от мута", {0: "снят", 1: "выдан"}),
            "links":  ("Ссылки", {0: "запрещены", 1: "разрешены"}),
            "ai":     ("Ответы ИИ", {0: "включены", 1: "выключены (игнор)"}),
        }
        title, words = titles[code]
        logger.info("👥 Админ %s: %s → %s = %s", admin_id, _target_label(target_id), title, words[new_val])
        _audit(admin_id, "setting", target_id, f"{title}: {words[new_val]}")
        await query.answer(f"{title}: {words[new_val]}")
        await _redraw_card(query, context.bot, target_id)
        return

    if section == "reset":
        # Подтверждение: настройки могли собираться долго, случайное нажатие
        # не должно стирать их молча.
        await query.answer()
        confirm_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, сбросить", callback_data=f"usr:resetok:{target_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"usr:cancel:{target_id}"),
        ]])
        try:
            await query.edit_message_reply_markup(reply_markup=confirm_kb)
        except Exception as e:
            logger.debug("👥 Не удалось показать подтверждение сброса: %s", e)
        return

    if section == "cancel":
        # Отмена подтверждения или экрана действия: возвращаем карточку
        # в том же сообщении.
        await query.answer("Отменено.")
        await _redraw_card(query, context.bot, target_id)
        return

    # ── Ручные действия: выбор группы → срок/подтверждение → выполнение ──
    if section == "pick":
        act = parts[3] if len(parts) > 3 else ""
        if act not in _ACTIONS:
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return
        chats = get_known_chats()
        if not chats:
            await query.answer("Бот пока не знает ни одной группы — напиши в неё что-нибудь.",
                               show_alert=True)
            return
        if len(chats) == 1:
            # Группа одна — лишний экран выбора не показываем. На кнопку
            # ответит сам следующий шаг (см. _action_step).
            await _action_step(query, context, target_id, act, chats[0]["chat_id"], admin_id)
            return
        await query.answer()
        rows = [[InlineKeyboardButton(c.get("title") or str(c["chat_id"]),
                                      callback_data=f"usr:step:{target_id}:{act}:{c['chat_id']}")]
                for c in chats]
        await _screen(query, f"{_ACTIONS[act]['label']}: <b>{html.escape(_target_name(target_id))}</b>\n\n"
                             f"В какой группе?", rows, target_id)
        return

    if section == "step":
        act = parts[3] if len(parts) > 3 else ""
        try:
            chat_id = int(parts[4])
        except (IndexError, ValueError):
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return
        if act not in _ACTIONS:
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return
        # На кнопку отвечает сам шаг — здесь этого делать нельзя (см. _action_step).
        await _action_step(query, context, target_id, act, chat_id, admin_id)
        return

    if section == "do":
        act = parts[3] if len(parts) > 3 else ""
        try:
            chat_id = int(parts[4])
            arg = int(parts[5])
        except (IndexError, ValueError):
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return
        if act not in _ACTIONS:
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return
        await _run_action(query, context, target_id, act, chat_id, arg, admin_id)
        return

    # ── Очистка контекста диалога ────────────────────────────────────
    if section == "clr":
        await query.answer()
        rows = [[InlineKeyboardButton("✅ Да, очистить", callback_data=f"usr:clrgo:{target_id}")]]
        await _screen(query, f"🧹 Очистить историю диалога с ИИ у "
                             f"<b>{html.escape(_target_name(target_id))}</b>?\n\n"
                             f"<i>Бот забудет разговор — то же самое, что команда /clear "
                             f"от самого человека.</i>", rows, target_id)
        return

    if section == "clrgo":
        from database.history import clear_history
        clear_history(target_id)
        logger.info("👥 Админ %s очистил контекст диалога: %s", admin_id, _target_label(target_id))
        _audit(admin_id, "clear_ctx", target_id)
        await query.answer("🧹 История диалога очищена.", show_alert=False)
        await _redraw_card(query, context.bot, target_id)
        return

    # ── Обнуление нарушений в личном деле ────────────────────────────
    if section == "viol":
        await query.answer()
        rows = [[InlineKeyboardButton("✅ Да, обнулить", callback_data=f"usr:violgo:{target_id}")]]
        await _screen(query, f"🧾 Обнулить нарушения у "
                             f"<b>{html.escape(_target_name(target_id))}</b>?\n\n"
                             f"<i>Счётчики мутов и удалённых ссылок обнулятся, взыскание "
                             f"снимется досрочно. Стаж и число сообщений останутся.</i>",
                      rows, target_id)
        return

    if section == "violgo":
        from database.history import dossier_reset_violations
        dossier_reset_violations(target_id)
        logger.info("👥 Админ %s обнулил нарушения: %s", admin_id, _target_label(target_id))
        _audit(admin_id, "reset_viol", target_id)
        await query.answer("🧾 Нарушения обнулены.", show_alert=False)
        await _redraw_card(query, context.bot, target_id)
        return

    # ── Роль и права (только владелец — см. таблицу в services/roles.py) ──
    if section == "rights":
        if not roles.is_moderator(target_id):
            await query.answer("Сначала назначь человека модератором.", show_alert=True)
            return
        await query.answer()
        await _show_rights(query, target_id)
        return

    if section == "role":
        action = parts[3] if len(parts) > 3 else ""
        if roles.is_owner(target_id):
            await query.answer("👑 Владелец задаётся в config.py — из панели его не меняют.",
                               show_alert=True)
            return
        if action == "on":
            roles.make_moderator(target_id, admin_id)
            await _sync_staff_menu(context.bot, target_id, True)
            logger.info("🛡 Владелец %s назначил модератора: %s", admin_id, _target_label(target_id))
            _audit(admin_id, "role_on", target_id)
            await query.answer("🛡 Назначен модератором. Права выдай кнопкой «⚙️Настройки Прав Модератора».",
                               show_alert=True)
            await _redraw_card(query, context.bot, target_id)
        elif action == "off":
            # Снятие сбрасывает все галочки разом, а кнопка стоит вплотную
            # к ним — спрашиваем. Отмена возвращает на экран прав, а не в
            # карточку: владелец никуда уходить не собирался.
            await query.answer()
            rows = [
                [InlineKeyboardButton("✅ Да, снять", callback_data=f"usr:role:{target_id}:offgo")],
                [InlineKeyboardButton("⬅️ Отмена", callback_data=f"usr:rights:{target_id}")],
            ]
            try:
                await query.edit_message_text(
                    f"🚫 <b>Снять модератора: {html.escape(_target_name(target_id))}?</b>\n\n"
                    f"<i>Все права будут сняты, выдавать их заново придётся вручную. "
                    f"Команда /adm из меню человека исчезнет.</i>",
                    parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
            except Exception as e:
                logger.debug("🛡 Не удалось показать подтверждение снятия: %s", e)
        elif action == "offgo":
            roles.unmake_moderator(target_id)
            await _sync_staff_menu(context.bot, target_id, False)
            logger.info("🛡 Владелец %s снял модератора: %s", admin_id, _target_label(target_id))
            _audit(admin_id, "role_off", target_id)
            await query.answer("🚫 Модератор снят вместе со всеми правами.", show_alert=False)
            # Экран прав снятому уже не о чем — возвращаемся в карточку.
            await _redraw_card(query, context.bot, target_id)
        else:
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
        return

    if section == "perm":
        code = parts[3] if len(parts) > 3 else ""
        if code not in roles.PERMS:
            await query.answer("❌ Неизвестное право.", show_alert=True)
            return
        if not roles.is_moderator(target_id):
            await query.answer("Сначала назначь человека модератором.", show_alert=True)
            return
        new_val = not roles.perms_of(target_id)[code]
        roles.grant_perm(target_id, code, new_val)
        title = roles.PERMS[code]["title"]
        logger.info("🛡 Владелец %s: право «%s» %s → %s", admin_id, title,
                    "выдано" if new_val else "снято", _target_label(target_id))
        _audit(admin_id, "perm", target_id, f"{title}: {'выдано' if new_val else 'снято'}")
        await query.answer(f"{title}: {'✅ выдано' if new_val else '⬜ снято'}")
        # Остаёмся на экране прав — галочки накликиваются подряд.
        await _show_rights(query, target_id)
        return

    # ── Почётное звание ──────────────────────────────────────────────
    if section == "rank":
        from config import QUIZ_RANKS
        from services.user_settings import honorary_rank
        await query.answer()
        cur = honorary_rank(target_id)
        rows, pair = [], []
        for idx, r in enumerate(QUIZ_RANKS):
            mark = "✅ " if r["name"] == cur else ""
            pair.append(InlineKeyboardButton(f"{mark}{r['icon']} {r['name']}",
                                             callback_data=f"usr:rankset:{target_id}:{idx}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        if cur:
            rows.append([InlineKeyboardButton("🚫 Убрать почётное звание",
                                              callback_data=f"usr:rankset:{target_id}:-1")])
        await _screen(query, f"🎖️ <b>Почётное звание: {html.escape(_target_name(target_id))}</b>\n\n"
                             f"Сейчас: {html.escape(cur) if cur else '<i>не задано</i>'}\n\n"
                             f"<i>Почётное звание видно в /rank вместо заработанного и не "
                             f"меняется от викторины. Пока оно стоит, бот не объявляет "
                             f"этому человеку повышения.</i>", rows, target_id)
        return

    if section == "rankset":
        from config import QUIZ_RANKS
        from services.user_settings import set_field
        try:
            idx = int(parts[3])
        except (IndexError, ValueError):
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return
        if idx == -1:
            set_field(target_id, "honorary_rank", None)
            logger.info("👥 Админ %s убрал почётное звание: %s", admin_id, _target_label(target_id))
            _audit(admin_id, "rank", target_id, "убрано")
            await query.answer("🚫 Почётное звание убрано.", show_alert=False)
        elif 0 <= idx < len(QUIZ_RANKS):
            rank_name = QUIZ_RANKS[idx]["name"]
            set_field(target_id, "honorary_rank", rank_name)
            logger.info("👥 Админ %s присвоил звание «%s»: %s",
                        admin_id, rank_name, _target_label(target_id))
            _audit(admin_id, "rank", target_id, rank_name)
            await query.answer(f"🎖️ Присвоено звание: {rank_name}", show_alert=False)
        else:
            await query.answer("❌ Неизвестное звание.", show_alert=True)
            return
        await _redraw_card(query, context.bot, target_id)
        return

    if section == "resetok":
        from services.user_settings import clear as _us_clear
        _us_clear(target_id)
        logger.info("👥 Админ %s сбросил ВСЕ персональные настройки: %s",
                    admin_id, _target_label(target_id))
        _audit(admin_id, "reset", target_id, "все персональные настройки")
        await query.answer("↩️ Персональные настройки сброшены — работают общие.", show_alert=True)
        await _redraw_card(query, context.bot, target_id)
        return

    await query.answer()


# ─── команда ────────────────────────────────────────────────────────

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Панель «👥 Пользователи». Только в личке; нужно право «👥 Карточки»."""
    await delete_user_message_safe(update.message)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not await _require(update, context, "cards"):
        return

    # Панель — только в личке. В группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    await _end_kb_test(context.bot, chat_id, context)  # выход из проверки поиска — с уборкой
    await send_users_panel(context.bot, chat_id, user_id)
