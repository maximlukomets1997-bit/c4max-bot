# ─────────────────────────────────────────────
#  services/user_settings.py — персональные настройки участников (карточка
#  пользователя в админ-панели, /users).
#
#  Контракт:
#  • ЕДИНСТВЕННЫЙ источник правды о персональных настройках. Кто хочет знать
#    «какой порог у этого человека», «разрешены ли ему ссылки» — спрашивает
#    здесь, а не лезет в БД сам.
#  • Настройки живут В ПАМЯТИ процесса. Они нужны на КАЖДОЕ сообщение группы
#    (антифлуд, фильтр ссылок), а поход в БД на горячем пути недопустим —
#    до этого бот там в базу не ходил вовсе, и так должно остаться.
#    Таблица крошечная (одна строка на человека, у кого вообще есть «своё»),
#    поэтому держим её целиком.
#  • Кэш загружается один раз при старте (main.py, сразу после init_db) и
#    обновляется при КАЖДОЙ правке из панели — писать в БД мимо set_field
#    нельзя, иначе бот не заметит изменения до перезапуска.
#  • ПУСТОЕ значение (None) означает «работает общая настройка бота», а не
#    ноль. Проверять только через «is None»: персональный ноль — это значение
#    (например, лимит картинок 0 = полный запрет).
#  • Модуль «тихий»: любая ошибка глушится и трактуется как «персональных
#    настроек нет» — сбой в этом модуле не должен ронять обработку сообщений.
# ─────────────────────────────────────────────

import logging

from config import IMAGE_DAILY_LIMIT, ADMIN_IDS
from database.history import (
    get_all_user_settings,
    get_user_settings,
    set_user_settings,
    clear_user_settings,
)

logger = logging.getLogger(__name__)

# user_id -> словарь настроек (только те, у кого есть своя строка в БД)
_cache: dict[int, dict] = {}
_loaded = False


# ─── загрузка и обновление кэша ─────────────────────────────────────

def load() -> int:
    """
    Загружает ВСЕ персональные настройки в память. Зовётся один раз при старте
    бота (main.py, после init_db). Возвращает число загруженных записей.
    """
    global _loaded
    try:
        rows = get_all_user_settings()
        _cache.clear()
        for row in rows:
            _cache[row["user_id"]] = row
        _loaded = True
        logger.info("👥 Персональные настройки загружены: %d чел.", len(_cache))
        return len(_cache)
    except Exception as e:
        logger.warning("⚠️ Не удалось загрузить персональные настройки: %s", e)
        _loaded = True   # не пытаемся снова на каждое сообщение
        return 0


def refresh(user_id: int) -> None:
    """Перечитывает настройки одного человека из БД в кэш (после правки извне)."""
    try:
        row = get_user_settings(user_id)
        if row:
            _cache[user_id] = row
        else:
            _cache.pop(user_id, None)
    except Exception as e:
        logger.debug("👥 Не удалось обновить настройки %s: %s", user_id, e)


def get(user_id: int) -> dict:
    """Настройки человека из кэша. Пустой словарь = всё общее. Никогда не падает."""
    if not _loaded:
        # Подстраховка на случай, если load() забыли позвать при старте:
        # грузим один раз здесь, а не при каждом обращении.
        load()
    return _cache.get(user_id) or {}


def set_field(user_id: int, field: str, value) -> None:
    """
    Записывает ОДНУ персональную настройку и сразу обновляет кэш.
    value=None означает «вернуть на общую настройку бота».
    ЕДИНСТВЕННЫЙ разрешённый способ менять настройки: запись мимо него
    оставит бота со старым значением в памяти до перезапуска.
    """
    set_user_settings(user_id, **{field: value})
    refresh(user_id)


def clear(user_id: int) -> None:
    """Убирает все персональные настройки — человек возвращается на общие."""
    clear_user_settings(user_id)
    _cache.pop(user_id, None)


# ─── смысловые помощники (ими пользуется остальной код) ─────────────

def thresholds_for(user_id: int, base: tuple[int, int, int]) -> tuple[int, int, int]:
    """
    Пороги антифлуда для конкретного человека: (сообщений, окно, длительность мута).
    base — общие пороги из /mod; персональное значение перекрывает своё поле.
    """
    s = get(user_id)
    count, window, mute = base
    if s.get("antispam_msg_count") is not None:
        count = s["antispam_msg_count"]
    if s.get("antispam_window_sec") is not None:
        window = s["antispam_window_sec"]
    if s.get("antispam_mute_sec") is not None:
        mute = s["antispam_mute_sec"]
    return count, window, mute


def is_immune(user_id: int) -> bool:
    """True — человека нельзя мутить автоматикой (персональный иммунитет)."""
    return bool(get(user_id).get("antispam_immune"))


def links_allowed(user_id: int) -> bool:
    """True — этому человеку разрешены ссылки (фильтр ссылок его не трогает)."""
    return bool(get(user_id).get("links_allowed"))


def ai_ignored(user_id: int) -> bool:
    """True — бот не отвечает этому человеку (ни в личке, ни на упоминание)."""
    return bool(get(user_id).get("ai_ignored"))


def image_limit_for(user_id: int):
    """
    Суточный лимит картинок: None — безлимит (админы бота), иначе число
    (персональное, если задано, иначе общее IMAGE_DAILY_LIMIT).
    """
    if user_id in ADMIN_IDS:
        return None
    personal = get(user_id).get("image_limit")
    return IMAGE_DAILY_LIMIT if personal is None else personal


def honorary_rank(user_id: int) -> str:
    """Почётное звание, назначенное вручную (пустая строка — обычное из викторины)."""
    return get(user_id).get("honorary_rank") or ""
