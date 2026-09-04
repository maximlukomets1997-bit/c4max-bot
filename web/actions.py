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


def user_quiz_score(actor_id: int, target_id: int, correct=None,
                    attempts=None, what: str = "правка") -> str:
    """
    Ручная правка счёта викторины со страницы участника. Возвращает строку
    «было → стало» для зелёной полосы над карточкой.

    ⚠️ САМИ ПРАВИЛА НЕ ЗДЕСЬ. И запреты («верных не больше попыток», потолок,
    отрицательные), и запись зовутся у панели бота — иначе сайт и бот начнут
    принимать разные числа, а поймёт это только тот, кому счёт перепишут не
    так. `what` — что писать в журнал персонала («промахи убраны», «обнулён»).
    """
    from handlers.admin.panel_users import _set_quiz_score, quiz_score_summary
    try:
        before, after = _set_quiz_score(target_id, correct=correct, attempts=attempts)
    except ValueError as e:
        raise ActionError(str(e))
    summary = quiz_score_summary(before, after)
    logger.info("🌐 Сайт: счёт викторины %s — %s (%s, админ %s)",
                _target_label(target_id), summary, what, actor_id)
    _staff_audit(actor_id, "quiz_score", target_id, f"{what}: {summary}")
    return summary


def user_quiz_fix(actor_id: int, target_id: int) -> str:
    """«Обнулить промахи» со страницы участника. Пусто — промахов и не было."""
    from handlers.admin.panel_users import fix_quiz_misses, quiz_score_summary
    try:
        changed = fix_quiz_misses(target_id)
    except ValueError as e:
        raise ActionError(str(e))
    if changed is None:
        return ""
    summary = quiz_score_summary(*changed)
    logger.info("🌐 Сайт: убраны промахи викторины %s — %s (админ %s)",
                _target_label(target_id), summary, actor_id)
    _staff_audit(actor_id, "quiz_score", target_id, f"промахи убраны: {summary}")
    return summary


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


# ─── база знаний ────────────────────────────────────────────────────
#
#  ⚠️ Статьи — ФАЙЛЫ на сервере (папка knowledge/), а не строки в базе. Через
#  GitHub они не ездят: новая статья появляется ТОЛЬКО там, где её добавили.
#  Значит, добавленное с сайта живёт на сервере и в домашней копии не
#  появится — так же, как у кнопки в боте.

KB_REBUILD_LATCH = "kb_rebuild_running"


def kb_add(actor_id: int, file_name: str, text: str) -> str:
    """Новая статья из присланного файла. Возвращает имя, под которым легла."""
    from services.knowledge_store import add_article
    from database.history import add_kb_action
    if not file_name.lower().endswith((".md", ".txt")):
        raise ActionError("нужен файл .md или .txt")
    if not text.strip():
        raise ActionError("файл пустой")
    fname = add_article(file_name, text)
    add_kb_action("добавлена статья", fname, actor_id)
    logger.info("🌐 Сайт: добавлена статья %s (админ %s)", fname, actor_id)
    return fname


def kb_replace(actor_id: int, folder: str, fname: str, text: str) -> None:
    """Замена текста существующей статьи."""
    from services.knowledge_store import replace_article
    from database.history import add_kb_action
    if not text.strip():
        raise ActionError("пустым текстом статью не заменяют")
    replace_article(folder, fname, text)
    add_kb_action("заменена статья", fname, actor_id)
    logger.info("🌐 Сайт: заменена статья %s (админ %s)", fname, actor_id)


def kb_approve(actor_id: int, fname: str) -> str:
    """Одобрение статьи: из очереди в базу."""
    from services.knowledge_store import approve_article
    from database.history import add_kb_action
    new_path = approve_article(fname)
    add_kb_action("одобрена статья", fname, actor_id)
    logger.info("🌐 Сайт: одобрена статья %s (админ %s)", fname, actor_id)
    return new_path


def kb_delete(actor_id: int, folder: str, fname: str) -> None:
    """Удаление статьи. Файл стирается насовсем — подтверждение спрашивает страница."""
    from services.knowledge_store import delete_article
    from database.history import add_kb_action
    delete_article(folder, fname)
    add_kb_action("удалена статья", fname, actor_id)
    logger.info("🌐 Сайт: удалена статья %s/%s (админ %s)", folder, fname, actor_id)


def kb_clear_log(actor_id: int) -> int:
    """Очистка журнала действий базы знаний."""
    from database.history import clear_kb_log
    deleted = clear_kb_log()
    logger.info("🌐 Сайт: очищен журнал базы знаний, %d записей (админ %s)",
                deleted, actor_id)
    return deleted


def kb_rebuild(actor_id: int, application) -> str:
    """
    Полная пересборка поискового указателя, фоном.

    ⚠️ Защёлка ОБЩАЯ с кнопкой в боте — два прогона разом дали бы половину
    указателя. См. web/longjobs.py.
    """
    from . import longjobs
    from services.rag import rebuild_knowledge_base
    from database.history import add_kb_action

    def describe(result):
        if result is None or result[1] == 0:
            return "⚠️ Не удалось пересобрать — база пуста или произошла ошибка."
        indexed, total = result
        add_kb_action("пересборка базы", f"проиндексировано {indexed} из {total}",
                      actor_id)
        if indexed >= total:
            return f"✅ Указатель пересобран: {indexed} из {total}."
        return (f"⚠️ Упёрлись в лимит Google: {indexed} из {total}. "
                f"Недостающее бот доберёт сам или нажмите ещё раз позже.")

    logger.info("🌐 Сайт: запущена пересборка базы знаний (админ %s)", actor_id)
    return longjobs.start(application, KB_REBUILD_LATCH, rebuild_knowledge_base,
                          describe)


def kb_test_search(text: str) -> dict:
    """
    Проверка поиска: что нашлось бы по такому вопросу. Ничего не меняет.
    Сетевой вызов (эмбеддинг запроса) — зовущий обязан увести его в поток.
    """
    from services import rag
    return rag.test_search(text)


# ─── викторина ──────────────────────────────────────────────────────

QUIZ_GEN_LATCH = "quiz_gen_running"


def quiz_generate(actor_id: int, application, retry: bool = False) -> str:
    """
    Сборка вопросов по статьям, фоном. retry=True — только те статьи, на
    которых сборка раньше сорвалась.

    ⚠️ Защёлка ОБЩАЯ с кнопкой в боте: два прогона разом дали бы вопросы-дубли.
    """
    from . import longjobs
    from services import quiz_bank

    kb = quiz_bank.stats()
    todo = kb["failed"] if retry else kb["articles_left"]
    if not todo:
        return ("✅ Список неудачных пуст — повторять нечего." if retry else
                "✅ По всем статьям вопросы уже собраны.")

    def describe(result):
        if result is None:
            return "⚠️ Сборка сорвалась. Подробности в логе бота."
        # ⚠️ КЛЮЧ ИМЕННО «saved» (services/quiz_bank.py::_run_over). С «added»
        # и страница, и журнал бодро сообщали «вопросов добавлено 0» после
        # удачной сборки сорока вопросов — ничего не падало, просто цифра была
        # ложью. Тот же класс ошибки, что «correct» вместо «correct_idx».
        # В журнал пишем ПОСЛЕ завершения и тем же кодом, что у кнопки в боте.
        _staff_audit(actor_id, "quiz_retry" if retry else "quiz_generate", 0,
                     f"статей {result.get('articles', 0)}, "
                     f"вопросов {result.get('saved', 0)}")
        return (f"✅ Готово: статей {result.get('articles', 0)}, "
                f"вопросов добавлено {result.get('saved', 0)}, "
                f"не вышло {result.get('failed', 0)}.")

    logger.info("🌐 Сайт: запущена %s вопросов (статей %d, админ %s)",
                "ПОВТОРНАЯ сборка" if retry else "сборка", todo, actor_id)
    work = quiz_bank.retry_failed if retry else quiz_bank.generate_batch
    return longjobs.start(application, QUIZ_GEN_LATCH, work, describe)


def quiz_approve(actor_id: int, qid: int) -> None:
    """Одобрение вопроса: черновик уходит в игру."""
    from database.history import get_quiz_question, set_quiz_question_approved
    if not get_quiz_question(qid):
        raise ActionError(f"вопроса №{qid} нет")
    set_quiz_question_approved(qid, True)
    _staff_audit(actor_id, "quiz_approve", 0, f"вопрос #{qid}")
    logger.info("🌐 Сайт: одобрен вопрос викторины №%s (админ %s)", qid, actor_id)


def quiz_delete(actor_id: int, qid: int) -> None:
    """Удаление вопроса — насовсем."""
    from database.history import delete_quiz_question, get_quiz_question
    if not get_quiz_question(qid):
        raise ActionError(f"вопроса №{qid} нет")
    delete_quiz_question(qid)
    _staff_audit(actor_id, "quiz_delete", 0, f"вопрос #{qid}")
    logger.info("🌐 Сайт: удалён вопрос викторины №%s (админ %s)", qid, actor_id)


def quiz_forget_fails(actor_id: int) -> int:
    """Забыть список статей, на которых сборка срывалась."""
    from database.history import clear_quiz_failures
    removed = clear_quiz_failures()
    _staff_audit(actor_id, "quiz_forget_fails", 0, f"забыто статей: {removed}")
    logger.info("🌐 Сайт: забыты неудачные статьи (%d, админ %s)", removed, actor_id)
    return removed


def quiz_seed(actor_id: int) -> dict:
    """Загрузка эталонного набора вопросов В ЧЕРНОВИКИ (не сразу в игру)."""
    from services import quiz_bank
    result = quiz_bank.load_seed(approved=False)
    _staff_audit(actor_id, "quiz_seed", 0, f"добавлено {result.get('added', 0)}")
    logger.info("🌐 Сайт: загружен эталонный набор вопросов (%s, админ %s)",
                result, actor_id)
    return result


def quiz_reseed(actor_id: int) -> dict:
    """
    Догнать банк до эталонного файла: переписать варианты, верный ответ и
    разбор там, где они разошлись (2026-09-01).

    ⚠️ Это НЕ «загрузить ещё раз». Загрузка добавляет новые вопросы и молча
    пропускает знакомые; здесь чинится ровно то, что она пропускает.
    """
    from services import quiz_bank
    result = quiz_bank.seed_apply()
    if result["updated"]:
        _staff_audit(actor_id, "quiz_reseed", 0,
                     f"догнано до файла: {result['updated']}")
    logger.info("🌐 Сайт: банк догнан до эталонного файла (%s, админ %s)",
                result, actor_id)
    return result


def quiz_wipe_drafts(actor_id: int) -> int:
    """Стереть все черновики вопросов."""
    from database.history import delete_quiz_drafts
    removed = delete_quiz_drafts()
    _staff_audit(actor_id, "quiz_wipe", 0, f"черновиков удалено: {removed}")
    logger.info("🌐 Сайт: стёрты черновики вопросов (%d, админ %s)", removed, actor_id)
    return removed


def quiz_nuke(actor_id: int) -> int:
    """Стереть ВСЕ вопросы — и черновики, и те, что в игре."""
    from database.history import delete_all_quiz_questions
    removed = delete_all_quiz_questions()
    _staff_audit(actor_id, "quiz_nuke", 0, f"стёрто вопросов: {removed}")
    logger.info("🌐 Сайт: СТЁРТЫ ВСЕ вопросы викторины (%d, админ %s)",
                removed, actor_id)
    return removed


def quiz_zero(actor_id: int) -> int:
    """Обнулить статистику ИГРОКОВ. Вопросы не трогает."""
    from database.history import reset_all_quiz_stats
    removed = reset_all_quiz_stats()
    _staff_audit(actor_id, "quiz_zero", 0, f"обнулено игроков: {removed}")
    logger.info("🌐 Сайт: обнулена статистика игроков викторины (%d, админ %s)",
                removed, actor_id)
    return removed


def quiz_auto_toggle(actor_id: int) -> bool:
    """Тумблер «Вопрос дня». Возвращает новое состояние."""
    from database.history import get_setting, set_setting
    from services import quiz_daily
    new_val = "0" if get_setting(quiz_daily.ENABLED_KEY, "0") == "1" else "1"
    set_setting(quiz_daily.ENABLED_KEY, new_val)
    _staff_audit(actor_id, "quiz_auto", 0,
                 f"вопрос дня {'включён' if new_val == '1' else 'выключен'}")
    logger.info("🌐 Сайт: вопрос дня %s (админ %s)",
                "включён" if new_val == "1" else "выключен", actor_id)
    return new_val == "1"


# ─── деньги, отчёты, обслуживание (этап 5) ──────────────────────────

def balance_set(actor_id: int, field_id: str, raw: str) -> str:
    """
    Правка остатка на счету, счётчика «потрачено» или квоты токенов.

    ⚠️ Разбор числа и описание полей взяты ИЗ ПАНЕЛИ БОТА
    (`panel_balance._balance_field`, `_parse_number`) — второй разборщик
    «5,32», «$ 5.32» и «1 000 000» разъехался бы с первым.

    ⚠️ ПРОЧЕРК УБИРАЕТ ЗНАЧЕНИЕ СОВСЕМ, а не обнуляет. Разница
    принципиальная: пока ключа нет, вычитание расхода его не находит и ничего
    не портит, а с нулём остаток ушёл бы в минус с первого же запроса.
    """
    from handlers.admin.panel_balance import (_balance_field, _money_str,
                                              _parse_number, _tokens_str,
                                              _value_str)
    from database.history import delete_setting

    info = _balance_field(field_id)
    if not info:
        raise ActionError(f"неизвестное поле «{field_id}»")

    was = _value_str(info["key"], info["kind"], info["absent"])
    raw = (raw or "").strip()

    if raw in ("-", "–", "—"):
        delete_setting(info["key"])
        _staff_audit(actor_id, "balance", 0, f"{info['short']}: убрано")
        logger.info("🌐 Сайт: убрано значение %s (было %s, админ %s)",
                    info["key"], was, actor_id)
        return f"{info['short']}: было {was} → стало «{info['absent']}»"

    value, err = _parse_number(raw, info["kind"])
    if value is None:
        raise ActionError(err)

    set_setting(info["key"],
                str(int(value)) if info["kind"] == "tokens" else f"{value:.6f}")
    shown = _tokens_str(value) if info["kind"] == "tokens" else _money_str(value)
    _staff_audit(actor_id, "balance", 0, f"{info['short']}: стало {shown}")
    logger.info("🌐 Сайт: %s = %s (было %s, админ %s)",
                info["key"], value, was, actor_id)
    return f"{info['short']}: было {was} → стало {shown}"


def report_text(kind: str) -> str:
    """
    Сохранённый текст отчёта: «вчера» или «неделя». Разметку Telegram убираем —
    страница показывает обычный текст.
    """
    from services import daily_report
    from .pages import plain
    text = (daily_report.last_report_text() if kind == "day"
            else daily_report.last_weekly_text()) or ""
    return plain(text)


def make_backup(actor_id: int) -> tuple:
    """
    Свежая копия базы. Возвращает (путь, размер).

    ⚠️ Снимается ЧЕРЕЗ sqlite3.Connection.backup (services/backup.py), а не
    копированием файла: база в режиме WAL, и часть свежих записей лежит в
    журнале — простой `cp` дал бы неполную копию.
    """
    from services import backup
    path, size = backup.make_backup()
    logger.info("🌐 Сайт: снята копия базы %s (%s, админ %s)",
                path, backup.human_size(size), actor_id)
    return path, size


def digest_text(chat_id: int) -> tuple:
    """Текст недельного дайджеста группы. Возвращает (текст, название группы)."""
    from services import group_digest
    from handlers.admin.panel_users import _chat_title
    title = _chat_title(chat_id)
    data = group_digest.collect(chat_id)
    return group_digest.render(data, title), title


async def digest_send(actor_id: int, chat_id: int, text: str, application) -> str:
    """
    Отправка дайджеста В ГРУППУ.

    ⚠️ УХОДИТ ИМЕННО ТОТ ТЕКСТ, КОТОРЫЙ ЧЕЛОВЕК ВИДЕЛ, а не пересчитанный
    заново. Это осознанное решение из кнопки в боте
    (handlers/admin/panel_digest.py, ветка «send»): неделя скользящая, и
    пересчёт в момент отправки дал бы ДРУГИЕ цифры — в группу ушло бы не то,
    что владелец одобрил.
    Сначала я написал здесь пересчёт и заодно позволил отправить, ни разу не
    нажав «Показать»; поймано разбором ошибок 30.08.2026.

    ⚠️ Это ПУБЛИЧНОЕ сообщение всем участникам чата от имени бота —
    подтверждение спрашивает страница, как и кнопка в боте.
    """
    if application is None:
        raise ActionError("нет доступа к боту — дайджест не отправлен")
    if not (text or "").strip():
        raise ActionError("нечего отправлять — сначала нажмите «Показать»")
    from telegram.constants import ParseMode
    from handlers.admin.panel_users import _chat_title

    await application.bot.send_message(chat_id=chat_id, text=text,
                                       parse_mode=ParseMode.HTML)
    # Подробность в журнале — дословно как у кнопки в боте.
    _staff_audit(actor_id, "digest", 0, f"дайджест отправлен в чат {chat_id}")
    logger.info("🌐 Сайт: дайджест отправлен в чат %s (админ %s)", chat_id, actor_id)
    return f"📤 Дайджест отправлен в «{_chat_title(chat_id)}»."


def digest_toggle(actor_id: int) -> bool:
    """Тумблер понедельничной отправки дайджеста владельцу."""
    from services import group_digest
    new_val = "0" if group_digest.is_enabled() else "1"
    set_setting(group_digest.ENABLED_KEY, new_val)
    _staff_audit(actor_id, "digest", 0,
                 f"еженедельный дайджест {'включён' if new_val == '1' else 'выключен'}")
    logger.info("🌐 Сайт: еженедельный дайджест %s (админ %s)",
                "включён" if new_val == "1" else "выключен", actor_id)
    return new_val == "1"


def wipe_conversations(actor_id: int) -> str:
    """
    «Очистить РАЗГОВОРЫ» — бот забывает переписку во ВСЕХ группах.

    ⚠️ ЗДЕСЬ ТРИ ШАГА, И НИ ОДИН НЕ ЛИШНИЙ (сверено с
    handlers/admin/panel_prompts.py::_handle_proactive_wipe):
      1. черта в базе — с неё бот начинает считать разговор заново;
      2. сброс счётчиков В ПАМЯТИ — без него проверка сработает по уже
         пустой стенограмме;
      3. закрытие файла записи разговора — текущий уезжает в архив, чтобы
         запись совпадала с той памятью, которая у бота осталась.
    Пропустишь любой — очистка будет неполной и молча.
    """
    from database.history import set_proactive_reset_mark
    from services.proactive import forget_conversations
    from services import chat_log

    mark = set_proactive_reset_mark()
    forget_conversations()
    chat_log.close_session()   # тихий шаг: не переложилось — очистка всё равно была
    _staff_audit(actor_id, "proactive_wipe", 0, "бот забыл разговоры во всех группах")
    logger.info("🌐 Сайт: бот забыл разговоры во всех группах (черта %s UTC, админ %s)",
                mark, actor_id)
    return "🧹 Готово: бот забыл разговоры во всех группах."


def toggle_personal_prompt(actor_id: int) -> bool:
    """
    Личный тумблер «применять ли ко мне промпт» (этап 7, 01.09.2026).
    Возвращает новое состояние ПРОМПТА (True — применяется).

    ⚠️ НАСТРОЙКА ХРАНИТСЯ НАОБОРОТ: "1" значит «промпт выключен». Так же
    в панели бота (`handlers/admin/router.py`, ветка `toggle_admin_prompt`),
    и трогать это хранение ради красоты нельзя — у живых админов уже лежат
    выставленные значения.

    ⚠️ В журнал персонала НЕ пишется — кнопка бота тоже не пишет: настройка
    личная и на других людей не влияет.
    """
    from database.history import get_setting, set_setting

    off_now = get_setting(f"admin_no_prompt_{actor_id}", "0") == "1"
    set_setting(f"admin_no_prompt_{actor_id}", "0" if off_now else "1")
    logger.info("🌐 Сайт: личный промпт админа %s %s",
                actor_id, "включён" if off_now else "выключен")
    return off_now


def clear_moderation_journal(actor_id: int) -> int:
    """
    Очистка журнала модерации — вместе с уликами (01.09.2026).

    ⚠️ СЛЕД В ЖУРНАЛЕ ПЕРСОНАЛА ОБЯЗАТЕЛЕН И КОД У НЕГО ТОТ ЖЕ, что у кнопки
    бота (`panel_mod::_handle_clearlog_callback` пишет `modlog_clear`). Это
    надзорная запись: она отвечает на вопрос «кто стёр улики», и разойдись
    коды — журнал начал бы называть одно и то же действие по-разному.
    """
    from database.history import clear_moderation_log

    deleted = clear_moderation_log()
    _staff_audit(actor_id, "modlog_clear", 0, f"журнал модерации: {deleted} записей")
    logger.info("🌐 Сайт: очищен журнал модерации (%d записей, админ %s)",
                deleted, actor_id)
    return deleted


def clear_staff_journal(actor_id: int) -> int:
    """
    Очистка журнала персонала (01.09.2026).

    ⚠️ В ЖУРНАЛ НЕ ПИШЕТ НИЧЕГО — и это не забывчивость, а повторение кнопки
    бота (`panel_users`, ветка `slogclear`): запись «журнал очищен» легла бы в
    тот самый журнал, который только что стёрли, и осталась бы там
    единственной строкой. Заводить её здесь значит развести следы одного
    действия по месту нажатия.
    """
    from database.history import clear_staff_log

    deleted = clear_staff_log()
    logger.info("🌐 Сайт: очищен журнал персонала (%d записей, админ %s)",
                deleted, actor_id)
    return deleted


async def restart_bot(actor_id: int, application) -> str:
    """
    Перезапуск бота.

    ⚠️ САЙТ ЖИВЁТ ВНУТРИ БОТА и умрёт вместе с ним — страница обязана об этом
    предупредить до нажатия. Через несколько секунд бот поднимется, и сайт
    вернётся сам.
    ⚠️ Пометка `shutdown_reason` обязательна: по ней хук остановки не шлёт
    «остановлен вручную», а main.py поднимает новую копию.
    """
    if application is None:
        raise ActionError("нет доступа к боту — перезапуск невозможен")
    import asyncio

    application.bot_data["shutdown_reason"] = "restart"
    # ⚠️ В журнал персонала перезапуск НЕ пишется — его не пишет туда и кнопка
    # «🔄 ПЕРЕЗАПУСК» в боте (сверено по handlers/admin/router.py). Завести
    # запись «заодно» значит сделать так, что одно и то же действие оставляет
    # след из одного места и не оставляет из другого.
    logger.info("🌐 Сайт: запрошен перезапуск бота (админ %s)", actor_id)

    async def _stop():
        # Даём странице уйти к человеку до того, как процесс остановится.
        await asyncio.sleep(1.5)
        try:
            application.stop_running()
        except Exception as e:
            logger.error("⚠️ Не удалось остановить бота для перезапуска: %s", e)

    application.create_task(_stop())
    return ("🔄 Перезапускаюсь. Сайт сейчас пропадёт на несколько секунд — "
            "это нормально, он поднимется сам.")


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


def apply_theme(user_id: int, code: str) -> str:
    """
    Выбор темы оформления сайта. Настройка общая (владелец один), поэтому
    лежит в settings, а не в куке: открыл админку с другого устройства —
    вид тот же.
    """
    from .pages import THEMES, THEME_SETTING_KEY
    known = {c: label for c, label, _ in THEMES}
    if code not in known:
        raise ActionError(f"неизвестная тема «{code}»")
    set_setting(THEME_SETTING_KEY, code)
    logger.info("🌐 Сайт: тема оформления → %s (админ %s)", code, user_id)
    return known[code]


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
