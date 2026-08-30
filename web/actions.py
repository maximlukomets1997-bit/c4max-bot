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
