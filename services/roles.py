# ─────────────────────────────────────────────
#  services/roles.py — роли и права: кто чем может управлять.
#
#  Контракт:
#  • ЕДИНСТВЕННЫЙ источник правды о доступе. Никакой код не должен проверять
#    ADMIN_IDS напрямую, чтобы решить «пускать ли в панель» — он спрашивает
#    здесь: can(user_id, "мод"), is_owner(...), can_act_on(...).
#    (ADMIN_IDS по-прежнему используется НЕ для доступа: личные привилегии
#    владельца — безлимит картинок, тумблеры промпта — это другое.)
#  • Три уровня: владелец (config.ADMIN_IDS, из панели НЕ меняется — иначе
#    управление ботом можно потерять), модератор (таблица staff + галочки
#    прав), обычный участник.
#  • Права живут В ПАМЯТИ: проверка идёт на КАЖДОЕ нажатие кнопки и каждую
#    команду, ходить за этим в БД нельзя. Кэш грузится при старте (main.py)
#    и обновляется при каждой правке — писать в БД мимо этого модуля нельзя.
#  • ЗАПРЕТ ПО УМОЛЧАНИЮ. Кнопка, которой нет в таблице `_CALLBACK_RULES`,
#    доступна ТОЛЬКО владельцу. Забыли вписать новую кнопку — она станет
#    недоступна модераторам, а не откроется всем: ошибка в безопасную сторону.
# ─────────────────────────────────────────────

import logging

from config import ADMIN_IDS
from database.history import (
    get_all_staff,
    get_staff,
    add_staff,
    remove_staff,
    set_staff_perm,
)

logger = logging.getLogger(__name__)

# Права. Ключ — короткий код (им пользуется весь остальной код), значение —
# графа в таблице staff и человекочитаемое название для панели.
#
# ПОДПИСЬ hint ОБЯЗАНА совпадать с тем, что кнопки реально позволяют делать:
# её читает владелец на экране «⚙️Настройки Прав Модератора», решая, что выдать.
# Меняешь раскладку в `_CALLBACK_RULES` ниже — перечитай подписи.
#
# Права «🔧 Антиспам» больше НЕТ (2026-07-20, решение Максима): общие пороги
# и тумблеры на все группы — только владелец. Графа p_antispam осталась в БД
# (старым модераторам она не мешает), но нигде не читается.
PERMS = {
    "mod":        {"field": "p_mod",        "title": "🛡DDoS-Guard",        "hint": "мут, размут, журнал и улики"},
    "ban":        {"field": "p_ban",        "title": "⛔ Бан",              "hint": "бан, разбан и кик участников"},
    "cards":      {"field": "p_cards",      "title": "👥 Карточки",        "hint": "смотреть список и карточки людей"},
    "cards_edit": {"field": "p_cards_edit", "title": "⚙️ Правка карточек", "hint": "картинки, нарушения, звание"},
}

# Право «менять карточки» бессмысленно без права «смотреть карточки» —
# выдаём и снимаем их согласованно (см. grant_perm).
_IMPLIES = {"cards_edit": "cards"}

# user_id -> словарь прав из БД
_cache: dict[int, dict] = {}
_loaded = False


# ─── загрузка и правка ──────────────────────────────────────────────

def load() -> int:
    """Загружает всех модераторов в память. Зовётся при старте бота (main.py)."""
    global _loaded
    try:
        rows = get_all_staff()
        _cache.clear()
        for row in rows:
            _cache[row["user_id"]] = row
        _loaded = True
        logger.info("🛡 Модераторы загружены: %d чел.", len(_cache))
        return len(_cache)
    except Exception as e:
        logger.warning("⚠️ Не удалось загрузить модераторов: %s", e)
        _loaded = True
        return 0


def _refresh(user_id: int) -> None:
    """Перечитывает права одного человека из БД в кэш."""
    try:
        row = get_staff(user_id)
        if row:
            _cache[user_id] = row
        else:
            _cache.pop(user_id, None)
    except Exception as e:
        logger.debug("🛡 Не удалось обновить права %s: %s", user_id, e)


def _perms(user_id: int) -> dict:
    """Строка прав из кэша ({} — не модератор). Никогда не падает."""
    if not _loaded:
        load()
    return _cache.get(user_id) or {}


def make_moderator(user_id: int, granted_by: int) -> None:
    """Назначает модератором (без прав — галочки выдаются отдельно)."""
    add_staff(user_id, granted_by)
    _refresh(user_id)


def unmake_moderator(user_id: int) -> None:
    """Снимает звание модератора вместе со всеми правами."""
    remove_staff(user_id)
    _cache.pop(user_id, None)


def grant_perm(user_id: int, perm: str, value: bool) -> None:
    """
    Ставит или снимает одну галочку права. Следит за связкой прав:
    включаешь «менять карточки» — автоматически включается «смотреть»;
    снимаешь «смотреть» — снимается и «менять» (иначе право повисло бы
    в воздухе: менять то, чего не видишь, нельзя).
    """
    if perm not in PERMS:
        return
    set_staff_perm(user_id, PERMS[perm]["field"], 1 if value else 0)
    if value and perm in _IMPLIES:
        set_staff_perm(user_id, PERMS[_IMPLIES[perm]]["field"], 1)
    if not value:
        for dependent, base in _IMPLIES.items():
            if base == perm:
                set_staff_perm(user_id, PERMS[dependent]["field"], 0)
    _refresh(user_id)


# ─── вопросы о правах ───────────────────────────────────────────────

def is_owner(user_id: int) -> bool:
    """Владелец бота (config.ADMIN_IDS). Может всё, снять права нельзя."""
    return user_id in ADMIN_IDS


def is_moderator(user_id: int) -> bool:
    """Модератор (владельцы сюда НЕ входят — для «и тот, и другой» есть is_staff)."""
    return bool(_perms(user_id))


def is_staff(user_id: int) -> bool:
    """Владелец или модератор — то есть «свой», кого не трогает автоматика."""
    return is_owner(user_id) or is_moderator(user_id)


def role_of(user_id: int) -> str:
    """'owner' | 'moderator' | 'user' — для показа в карточке."""
    if is_owner(user_id):
        return "owner"
    return "moderator" if is_moderator(user_id) else "user"


def can(user_id: int, perm: str) -> bool:
    """
    Главная проверка: есть ли у человека это право.
    Владельцу разрешено всё. Особый код 'owner' — только владельцу.
    """
    if is_owner(user_id):
        return True
    if perm == "owner" or perm not in PERMS:
        return False
    return bool(_perms(user_id).get(PERMS[perm]["field"]))


def has_any_perm(user_id: int) -> bool:
    """Есть ли хоть одно право — то есть открывать ли ему панель вообще."""
    if is_owner(user_id):
        return True
    p = _perms(user_id)
    return any(p.get(cfg["field"]) for cfg in PERMS.values())


def perms_of(user_id: int) -> dict:
    """Словарь {код права: включено ли} — для галочек в карточке."""
    p = _perms(user_id)
    return {code: bool(p.get(cfg["field"])) for code, cfg in PERMS.items()}


def list_moderators() -> list[int]:
    """id всех модераторов (для рассылки уведомлений персоналу)."""
    if not _loaded:
        load()
    return list(_cache.keys())


def can_act_on(actor_id: int, target_id: int) -> bool:
    """
    Иерархия: можно ли actor'у применять меры к target'у (мут, кик, бан,
    правка карточки). Владелец может ко всем. Модератор — только к обычным
    участникам: ни к владельцу, ни к другому модератору, ни к самому себе.
    """
    if is_owner(actor_id):
        return True
    if actor_id == target_id:
        return False
    return not is_staff(target_id)


# ─── какое право нужно для кнопки ───────────────────────────────────
# ЕДИНАЯ таблица доступа к кнопкам. Проверяется по порядку, первое
# совпадение по началу callback_data выигрывает. Чего здесь нет —
# доступно ТОЛЬКО владельцу (запрет по умолчанию, см. шапку файла).
#
# С 2026-07-20 эта же таблица решает, что модератор УВИДИТ: панели рисуют
# только те кнопки, которые он вправе нажать (`_filter_keyboard` в
# handlers/admin/common.py). Отдельного списка «что показывать» больше нет —
# правишь строку здесь, панель подстраивается сама.
#
# Особый случай — ветки usr:pick / usr:step / usr:do и usr:set: нужное право
# зависит от ДЕЙСТВИЯ или НАСТРОЙКИ внутри данных кнопки (мут — одно право,
# бан — другое), поэтому они разбираются отдельно в perm_for_callback ниже.
_CALLBACK_RULES = (
    # Навигация: своя панель доступна любому, у кого есть хоть одно право.
    ("adm_back",          "any"),
    # Разделы главной панели
    ("adm_open_mod",      "mod"),
    ("adm_open_users",    "cards"),
    # Модерация. Общие пороги и тумблеры на ВСЕ группы — только владелец:
    # модератор видит их значения в тексте панели, но не крутит.
    ("mod:antispam:",     "owner"),
    # Приветствие новичков: тумблеры и срок — общие на все группы, как и
    # пороги антифлуда выше, поэтому уровень владельца.
    ("mod:greet:",        "owner"),
    ("mod:unmute:",       "mod"),
    ("mod:evidence:",     "mod"),      # улики: за что бот выдал мут
    ("mod:clearlog",      "owner"),   # покрывает и mod:clearlog_yes
    # Панель промптов целиком владельческая (adm_open_prompts в таблице нет —
    # запрет по умолчанию). «Очистить РАЗГОВОРЫ» вписана ЯВНО: действие общее
    # на все группы сразу, пусть его уровень видно в таблице.
    ("proactive:wipe",    "owner"),   # покрывает и wipe_yes / wipe_no
    # Карточки пользователей: просмотр
    ("usr:list",          "cards"),
    ("usr:card:",         "cards"),
    ("usr:cancel:",       "cards"),
    ("usr:noop",          "cards"),
    # Карточки пользователей: правка. Модератору с правом «⚙️ Правка карточек»
    # достаются только лимит картинок, обнуление нарушений и почётное звание.
    # Пороги антифлуда, тумблеры и сбросы — владельцу: этими настройками можно
    # тихо выключить человека из жизни бота, и заметить это трудно.
    ("usr:tog:",          "owner"),
    ("usr:reset:",        "owner"),
    ("usr:resetok:",      "owner"),
    ("usr:clr:",          "owner"),
    ("usr:clrgo:",        "owner"),
    ("usr:viol:",         "cards_edit"),
    ("usr:violgo:",       "cards_edit"),
    ("usr:rank:",         "cards_edit"),
    ("usr:rankset:",      "cards_edit"),
    # Роли, права и надзор — ТОЛЬКО владелец (в таблице явно, чтобы было видно).
    ("usr:role:",         "owner"),   # покрывает и usr:role:<id>:offgo
    ("usr:perm:",         "owner"),
    ("usr:rights:",       "owner"),   # экран «⚙️Настройки Прав Модератора»
    ("usr:slog",          "owner"),   # покрывает и usr:slogclear
)

# Действие внутри usr:pick / usr:step / usr:do → нужное право.
# Кик живёт под правом «⛔ Бан» (решение Максима 2026-07-20): в Telegram это
# и есть бан с немедленным снятием, и держать его в «Модерации» рядом с мутом
# значило бы отдать изгнание людей всякому, кому дали право размучивать.
_ACTION_PERMS = {
    "mute": "mod", "unmute": "mod",
    "kick": "ban", "ban": "ban", "unban": "ban",
}

# Регулятор внутри usr:set:<id>:<код>:inc|dec → нужное право. Чего здесь нет
# (порог, окно, длительность мута) — владельцу.
_SETTING_PERMS = {"img": "cards_edit"}


def perm_for_callback(data: str) -> str:
    """
    Какое право нужно, чтобы нажать эту кнопку: код права, 'any' (любой из
    персонала) или 'owner'. Неизвестная кнопка → 'owner' (запрет по умолчанию).
    """
    if not data:
        return "owner"

    # Действия над участником: право зависит от самого действия.
    # Формат: usr:pick:<id>:<действие> | usr:step:<id>:<действие>:<чат> |
    #         usr:do:<id>:<действие>:<чат>:<аргумент>
    if data.startswith(("usr:pick:", "usr:step:", "usr:do:")):
        parts = data.split(":")
        action = parts[3] if len(parts) > 3 else ""
        return _ACTION_PERMS.get(action, "owner")

    # Регуляторы ➖/➕ карточки: право зависит от самой настройки.
    # Формат: usr:set:<id>:<код>:inc|dec
    if data.startswith("usr:set:"):
        parts = data.split(":")
        code = parts[3] if len(parts) > 3 else ""
        return _SETTING_PERMS.get(code, "owner")

    for prefix, perm in _CALLBACK_RULES:
        if data.startswith(prefix):
            return perm
    return "owner"


def may_press(user_id: int, data: str) -> bool:
    """Можно ли этому человеку нажать эту кнопку."""
    perm = perm_for_callback(data)
    if perm == "any":
        return has_any_perm(user_id)
    return can(user_id, perm)
