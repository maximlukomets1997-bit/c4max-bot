# ───────────────────────────────────────────────
#  web/actions.py — что происходит, когда на сайте меняют настройку
#  (30.08.2026, этап 1).
#
#  ⚠️ ГЛАВНОЕ ПРАВИЛО ЭТОГО ФАЙЛА: правка с сайта обязана делать ВСЁ ТО ЖЕ,
#  что делает нажатие соответствующей кнопки в Telegram. Не «записать
#  значение», а именно всё: строку в лог бота, запись в журнал персонала и
#  побочные действия. Иначе одна и та же настройка, изменённая двумя путями,
#  оставляла бы разные следы — и журнал перестал бы отвечать на вопрос
#  «кто это сделал».
#
#  Само значение пишет services/settings_spec.py (пределы и шаги там же,
#  общие с кнопками). Здесь — только обвязка вокруг записи.
#
#  Известное побочное действие ровно одно: при выключении «Сам в разговор»
#  бот объявляет об этом во все известные группы, при включении — убирает
#  объявление. Делается тем же кодом, что и у кнопки
#  (handlers/admin/panel_prompts.py), а не своей копией.
# ───────────────────────────────────────────────

import logging

from database.history import get_setting, set_setting
from services import settings_spec as spec

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """Правка не принята: неизвестное имя, негодное значение, нет прав."""


# ⚠️ Коды действий для журнала персонала взяты ТЕ ЖЕ, что у кнопок
# (handlers/admin/panel_mod.py, panel_prompts.py, router.py) — иначе одна и
# та же правка называлась бы в журнале по-разному в зависимости от того, где
# её сделали. Настройки, которых здесь нет, кнопки тоже не журналируют —
# добавлять их «заодно» значит менять поведение мимо задачи.
_AUDIT_CODES = {
    "antispam_enabled":   ("antispam", "антиспам"),
    "linkfilter_enabled": ("antispam", "фильтр ссылок"),
    "antispam_msg_count": ("antispam", "общий порог"),
    "antispam_window_sec": ("antispam", "общее окно"),
    "antispam_mute_sec":  ("antispam", "общая длительность мута"),
    "greet_enabled":      ("greet", "приветствие новичков"),
    "greet_captcha":      ("greet", "проверка «я не бот»"),
    "greet_kick":         ("greet", "кик не прошедших проверку"),
    "greet_timeout_sec":  ("greet", "срок проверки"),
    "thoughts_enabled":   ("thoughts", "мысли под капотом"),
    "proactive_hands":    ("proactive_hands", "руки «Сам в разговор»"),
}


def _audit(user_id: int, key: str, shown: str) -> None:
    """Запись в журнал персонала — тем же кодом, что у кнопки."""
    entry = _AUDIT_CODES.get(key)
    if not entry:
        return
    code, title = entry
    from handlers.admin.common import _audit as write_audit
    write_audit(user_id, code, 0, f"{title}: {shown} (с сайта)")


# ─── простые настройки: тумблеры и числа ────────────────────────────

async def apply_setting(user_id: int, key: str, raw, application=None) -> str:
    """
    Меняет одну простую настройку и возвращает её новое значение для показа.
    raw = None означает «переключить» (тумблер), иначе — поставить значение.
    """
    if key not in spec.SPEC:
        raise ActionError(f"неизвестная настройка «{key}»")

    item = spec.SPEC[key]
    before = spec.read(key)

    try:
        if item["kind"] == "toggle" and raw is None:
            spec.toggle(key)
        else:
            spec.write(key, raw)
    except ValueError as e:
        raise ActionError(str(e))

    shown = spec.display(key)
    logger.info("🌐 Сайт: %s = %s (админ %s)", item["title"], shown, user_id)
    _audit(user_id, key, shown)

    # Единственное побочное действие среди простых настроек.
    if key == "proactive_enabled" and spec.read(key) != before:
        await _announce_proactive(application, spec.read(key))

    return shown


async def _announce_proactive(application, now_on: bool) -> None:
    """
    Объявление группам о смене режима «Сам в разговор» — тем же кодом, что у
    кнопки в панели промптов.

    ⚠️ Тихое: сорвалась рассылка — настройка всё равно уже изменена, и
    сообщать об этом ошибкой значит соврать. В лог пишем.
    ⚠️ Без application (сайт поднят без бота — так бывает только в проверках)
    объявление пропускается молча.
    """
    if application is None:
        logger.warning("🌐 Объявление о «Сам в разговор» пропущено: нет доступа к боту")
        return
    try:
        from handlers.admin.panel_prompts import (_announce_proactive_off,
                                                  _remove_proactive_off_announce)
        if now_on:
            await _remove_proactive_off_announce(application.bot)
        else:
            await _announce_proactive_off(application.bot)
    except Exception as e:
        logger.warning("⚠️ Объявление о смене режима «Сам в разговор» не отработало: %s", e)


# ─── промпты ────────────────────────────────────────────────────────

def apply_prompt(user_id: int, key: str, text: str) -> int:
    """
    Сохраняет промпт. Возвращает длину сохранённого текста.

    ⚠️ Делает ровно то же, что команда /X_prompt_set в боте: пишет настройку
    и строку в лог. В журнал персонала промпты НЕ пишутся — их не пишет туда
    и команда (сверено по handlers/admin/panel_prompts.py). Добавлять журнал
    «заодно» здесь нельзя: тогда правка с сайта оставляла бы след, а та же
    правка из бота — нет.

    ⚠️ Пустой текст = промпт стёрт. Заводского значения у промптов НЕТ,
    поэтому подтверждение спрашивает страница, до вызова этой функции.
    """
    from services import prompts_spec
    if key not in prompts_spec.BY_KEY:
        raise ActionError(f"неизвестный промпт «{key}»")
    length = prompts_spec.write(key, text)
    title = prompts_spec.BY_KEY[key]["title"]
    if length:
        logger.info("🌐 Сайт: промпт «%s» заменён, %d символов (админ %s)",
                    title, length, user_id)
    else:
        logger.info("🌐 Сайт: промпт «%s» СТЁРТ (админ %s)", title, user_id)
    return length


# ─── люди: карточка участника ───────────────────────────────────────
#
#  ⚠️ ВСЁ ЗДЕСЬ ЗОВЁТ ТЕ ЖЕ ФУНКЦИИ, ЧТО КНОПКИ КАРТОЧКИ в
#  handlers/admin/panel_users.py — включая запись в журнал персонала тем же
#  кодом действия. Карточка в боте и карточка на сайте обязаны оставлять
#  одинаковый след: журнал отвечает на вопрос «кто это сделал», и ответ не
#  должен зависеть от того, откуда нажали.
#
#  ⚠️ На сайт пускают ТОЛЬКО владельца (web/auth.is_allowed), поэтому проверок
#  «а можно ли этому человеку» здесь нет — в отличие от панели, где кнопки
#  отсеиваются по правам. Появятся на сайте модераторы — эти проверки
#  придётся завести, и это записано в auth.is_allowed.

def _staff_audit(actor_id: int, code: str, target_id: int, details: str = "") -> None:
    from handlers.admin.common import _audit as write_audit
    write_audit(actor_id, code, target_id, details)


def _target_label(target_id: int) -> str:
    from handlers.admin.panel_users import _target_label as label
    return label(target_id)


def user_adjust(actor_id: int, target_id: int, code: str, delta: int):
    """Шаг персональной настройки. Возвращает (новое значение, изменилось ли).
    None означает «вернулся на общую настройку бота»."""
    from handlers.admin.panel_users import _USER_LIMITS, _adjust_user_setting
    if code not in _USER_LIMITS:
        raise ActionError(f"неизвестная настройка «{code}»")
    new_val, changed = _adjust_user_setting(target_id, code, delta)
    if changed:
        shown = "общая настройка" if new_val is None else new_val
        logger.info("🌐 Сайт: персональная настройка %s = %s у %s (админ %s)",
                    code, shown, _target_label(target_id), actor_id)
        _staff_audit(actor_id, "setting", target_id, f"{code} = {shown}")
    return new_val, changed


# Названия и слова тумблеров карточки — ДОСЛОВНО как в панели
# (handlers/admin/panel_users.py, ветка «tog»): их видно в журнале персонала,
# и две разные формулировки одного тумблера читались бы как разные действия.
USER_TOGGLE_WORDS = {
    "immune": ("Иммунитет от мута", {0: "снят", 1: "выдан"}),
    "links":  ("Ссылки", {0: "запрещены", 1: "разрешены"}),
    "ai":     ("Ответы ИИ", {0: "включены", 1: "выключены (игнор)"}),
}


def user_toggle(actor_id: int, target_id: int, code: str) -> int:
    """Тумблер персональной настройки. Возвращает новое значение (0/1)."""
    from handlers.admin.panel_users import _USER_TOGGLES, _toggle_user_setting
    if code not in _USER_TOGGLES:
        raise ActionError(f"неизвестный тумблер «{code}»")
    new_val = _toggle_user_setting(target_id, code)
    title, words = USER_TOGGLE_WORDS[code]
    logger.info("🌐 Сайт: %s → %s = %s (админ %s)",
                _target_label(target_id), title, words[new_val], actor_id)
    _staff_audit(actor_id, "setting", target_id, f"{title}: {words[new_val]}")
    return new_val


def user_reset_settings(actor_id: int, target_id: int) -> None:
    """Сброс ВСЕХ персональных настроек — человек возвращается на общие правила."""
    from services.user_settings import clear as us_clear
    us_clear(target_id)
    logger.info("🌐 Сайт: сброшены персональные настройки %s (админ %s)",
                _target_label(target_id), actor_id)
    # Код действия и текст — те же, что у кнопки в карточке (иначе одна и та
    # же правка называлась бы в журнале по-разному).
    _staff_audit(actor_id, "reset", target_id, "все персональные настройки")


def user_reset_violations(actor_id: int, target_id: int) -> None:
    """Обнуление счётчиков нарушений в личном деле."""
    from database.history import dossier_reset_violations
    dossier_reset_violations(target_id)
    logger.info("🌐 Сайт: обнулены нарушения %s (админ %s)",
                _target_label(target_id), actor_id)
    _staff_audit(actor_id, "reset_viol", target_id)


def user_clear_history(actor_id: int, target_id: int) -> None:
    """Очистка истории диалога — то же, что команда /clear от самого человека."""
    from database.history import clear_history
    clear_history(target_id)
    logger.info("🌐 Сайт: очищена история диалога %s (админ %s)",
                _target_label(target_id), actor_id)
    _staff_audit(actor_id, "clear_ctx", target_id)


def user_rank(actor_id: int, target_id: int, idx: int) -> str:
    """Почётное звание по НОМЕРУ в config.QUIZ_RANKS; −1 — убрать."""
    from config import QUIZ_RANKS
    from services.user_settings import set_field
    if idx == -1:
        set_field(target_id, "honorary_rank", None)
        _staff_audit(actor_id, "rank", target_id, "убрано")
        logger.info("🌐 Сайт: убрано почётное звание у %s (админ %s)",
                    _target_label(target_id), actor_id)
        return ""
    if not (0 <= idx < len(QUIZ_RANKS)):
        raise ActionError(f"звания №{idx} не существует")
    name = QUIZ_RANKS[idx]["name"]
    set_field(target_id, "honorary_rank", name)
    _staff_audit(actor_id, "rank", target_id, name)
    logger.info("🌐 Сайт: присвоено звание «%s» %s (админ %s)",
                name, _target_label(target_id), actor_id)
    return name


async def user_role(actor_id: int, target_id: int, make: bool, application=None) -> None:
    """
    Назначает или снимает модератора.

    ⚠️ Снятие сбрасывает ВСЕ галочки прав разом — подтверждение спрашивает
    страница, как и кнопка в боте.
    ⚠️ Меню команд Telegram у человека обновляется тем же помощником, что и в
    панели: без него у снятого модератора в меню осталась бы команда /adm.
    """
    from services import roles
    if roles.is_owner(target_id):
        raise ActionError("владелец задаётся в config.py — из админки его не меняют")

    if make:
        roles.make_moderator(target_id, actor_id)
    else:
        roles.unmake_moderator(target_id)

    if application is not None:
        try:
            from handlers.admin.panel_users import _sync_staff_menu
            await _sync_staff_menu(application.bot, target_id, make)
        except Exception as e:
            logger.warning("⚠️ Меню команд для %s не обновлено: %s", target_id, e)
    else:
        logger.warning("🌐 Меню команд не обновлено: нет доступа к боту")

    _staff_audit(actor_id, "role_on" if make else "role_off", target_id)
    logger.info("🌐 Сайт: %s модератора %s (владелец %s)",
                "назначил" if make else "снял", _target_label(target_id), actor_id)


def user_perm(actor_id: int, target_id: int, code: str) -> bool:
    """Переключает одну галочку права модератора. Возвращает новое состояние."""
    from services import roles
    if code not in roles.PERMS:
        raise ActionError(f"неизвестное право «{code}»")
    if not roles.is_moderator(target_id):
        raise ActionError("сначала назначьте человека модератором")
    new_val = not roles.perms_of(target_id)[code]
    roles.grant_perm(target_id, code, new_val)
    title = roles.PERMS[code]["title"]
    _staff_audit(actor_id, "perm", target_id,
                 f"{title}: {'выдано' if new_val else 'снято'}")
    logger.info("🌐 Сайт: право «%s» %s → %s (владелец %s)", title,
                "выдано" if new_val else "снято", _target_label(target_id), actor_id)
    return new_val


# Ручные меры. Ключ — код действия, значение — что писать человеку об успехе.
USER_ACTIONS = {
    "mute":   "🔇 Мут выдан",
    "unmute": "🔓 Мут снят",
    "kick":   "👢 Выгнан из группы",
    "ban":    "⛔ Забанен",
    "unban":  "🔙 Бан снят",
}


async def user_moderate(actor_id: int, target_id: int, act: str, chat_id: int,
                        seconds: int = 0, application=None) -> str:
    """
    Ручная мера в конкретной группе: мут, размут, кик, бан, разбан.

    Возвращает строку о том, что вышло. Ошибку от Telegram (нет прав у бота,
    человека нет в чате) поднимает как ActionError — она человеку и нужна.

    ⚠️ Журнал персонала пишется ТОЛЬКО ПОСЛЕ УСПЕХА: неудачная попытка
    действием не была. Ровно как в панели.
    ⚠️ Уведомления владельцу здесь НЕТ намеренно: в панели оно шлётся только
    про действия МОДЕРАТОРА, а на сайт пускают лишь владельца — сообщать ему
    о том, что он сам только что сделал, незачем.
    """
    if act not in USER_ACTIONS:
        raise ActionError(f"неизвестное действие «{act}»")
    if application is None:
        raise ActionError("нет доступа к боту — действие не выполнено")

    from services import antispam
    from handlers.admin.panel_users import _target_name, _chat_title

    bot = application.bot
    name = _target_name(target_id)
    admin_name = "админка"

    if act == "mute":
        if seconds <= 0:
            raise ActionError("не задан срок мута")
        err = await antispam.mute_user(bot, chat_id, target_id, seconds, name,
                                       admin_name, actor_id=actor_id)
        done = f"🔇 Мут выдан на {seconds // 60} мин."
    elif act == "unmute":
        ok = await antispam.unmute(bot, chat_id, target_id, name, admin_name)
        err = "" if ok else "не удалось снять мут — проверьте права бота в группе"
        done = "🔓 Мут снят."
    elif act == "kick":
        err = await antispam.kick_user(bot, chat_id, target_id, name, admin_name,
                                       actor_id=actor_id)
        done = "👢 Выгнан из группы."
    elif act == "ban":
        err = await antispam.ban_user(bot, chat_id, target_id, name, admin_name,
                                      actor_id=actor_id)
        done = "⛔ Забанен."
    else:
        err = await antispam.unban_user(bot, chat_id, target_id, name, admin_name,
                                        actor_id=actor_id)
        done = "🔙 Бан снят."

    if err:
        raise ActionError(str(err))

    where = _chat_title(chat_id)
    detail = f"на {seconds // 60} мин · {where}" if act == "mute" else where
    _staff_audit(actor_id, act, target_id, detail)
    logger.info("🌐 Сайт: %s — %s (%s), админ %s", act, _target_label(target_id),
                where, actor_id)
    return done


# ─── выбор модели ───────────────────────────────────────────────────

def apply_model(user_id: int, key: str) -> str:
    """Смена активной текстовой модели. Возвращает её название для показа."""
    from config import AVAILABLE_MODELS
    if key not in AVAILABLE_MODELS:
        raise ActionError(f"неизвестная модель «{key}»")
    set_setting("active_model", key)
    name = AVAILABLE_MODELS[key]["name"]
    logger.info("🌐 Сайт: модель переключена на %s (админ %s)", name, user_id)
    return name


def apply_image_model(user_id: int, key: str) -> str:
    """Смена модели картинок."""
    from config import AVAILABLE_IMAGE_MODELS
    if key not in AVAILABLE_IMAGE_MODELS:
        raise ActionError(f"неизвестная модель картинок «{key}»")
    set_setting("active_image_model", key)
    name = AVAILABLE_IMAGE_MODELS[key]["name"]
    logger.info("🌐 Сайт: модель картинок переключена на %s (админ %s)", name, user_id)
    return name


# ─── глубина раздумий ───────────────────────────────────────────────

def apply_thinking(user_id: int, provider: str, code: str) -> str:
    """
    Ставит глубину раздумий провайдера. В отличие от кнопки в Telegram, которая
    ЛИСТАЕТ положения по кругу, на сайте видно всю шкалу — поэтому здесь
    значение задаётся сразу, а не шагом.

    Ключ настройки и разбор значения — те же, что у кнопки
    (config.THINKING_SETTING_PREFIX, services/gemini.thinking_level).
    """
    from config import THINKING_LEVELS, THINKING_SETTING_PREFIX, PROVIDERS
    levels = THINKING_LEVELS.get(provider)
    if not levels:
        raise ActionError(f"неизвестный провайдер «{provider}»")
    labels = dict(levels)
    if code not in labels:
        raise ActionError(f"у провайдера «{provider}» нет ступени «{code}»")

    set_setting(THINKING_SETTING_PREFIX + provider, code)
    title = PROVIDERS.get(provider, {}).get("title", provider)
    logger.info("🌐 Сайт: глубина раздумий %s → %s (админ %s)",
                title, labels[code], user_id)
    from handlers.admin.common import _audit as write_audit
    write_audit(user_id, "thinking", 0, f"глубина {title}: {labels[code]} (с сайта)")
    return labels[code]
