# ───────────────────────────────────────────────
#  database/settings.py — настройки бота и тексты промптов (02.09.2026).
#
#  Шаг 6 разреза history.py. ⚠️ ПОРЯДОК ШАГОВ ИЗМЕНЁН НА ХОДУ: по плану здесь
#  должен был ехать архив групп, но разбор перед правкой показал, что он зовёт
#  настройки — и уехал бы раньше того, от чего зависит. Кольцо `history` ↔
#  `groups` пришлось бы разрывать импортом внутри функции. Настройки же не
#  зависят НИ ОТ ЧЕГО (проверено обходом всех вызовов), поэтому встали раньше,
#  а не позже. Зависимых у них ровно три: `add_messages` (переписка),
#  `set_proactive_reset_mark` и `get_recent_group_messages` (архив групп) —
#  это и есть те самые три перекрёстные связи, найденные при замере в самом
#  начале разреза.
#
#  Что здесь:
#    get_setting / set_setting / delete_setting — таблица settings, всё
#      настраиваемое хозяйство бота: тумблеры, пороги, копилки, выбранные
#      модели, сохранённые тексты отчётов
#    шесть читалок ПРОМПТОВ — системный, дополнения, новостной, RAG,
#      участие в разговоре, справка об авторе
#
#  ⚠️ SETTINGS ХРАНИТ ТОЛЬКО СТРОКИ. Забыл int() — получил тихую ошибку
#  сравнения («10» < «9» — правда для строк). Пределы и шаги простых настроек
#  живут в services/settings_spec.py, а НЕ здесь: их читают и кнопки бота, и
#  сайт, и второй копии быть не должно.
#
#  ⚠️ У get_setting ЕСТЬ ПОБОЧНОЕ ДЕЙСТВИЕ, и это не опечатка: для двух ключей
#  (active_model, active_image_model) она СБРАСЫВАЕТ настройку в заводскую,
#  если выбранной модели больше нет в списке доступных, — то есть читалка
#  пишет в базу. Иначе бот слал бы запросы на снятую с публикации модель.
#  Перенесено как есть; менять поведение при переезде нельзя.
#
#  ⚠️ ЗАВОДСКИЕ ТЕКСТЫ ПРОМПТОВ БЕРУТСЯ ИЗ config ВНУТРИ функций, а не в
#  шапке. Так было и в history.py: часть из них — пустые строки, и импорт в
#  шапке связал бы порядок загрузки модулей ради значений, которые нужны
#  раз в сутки.
# ───────────────────────────────────────────────

from ._core import _lock, _get_connection

def get_setting(key: str, default: str = "") -> str:
    """Возвращает текстовое значение настройки из БД."""
    with _lock:
        conn = _get_connection()
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    val = row["value"] if row else default
    if key == "active_model":
        from config import AVAILABLE_MODELS
        if val not in AVAILABLE_MODELS:
            # Если выбранной модели больше нет в списке доступных, сбрасываем на дефолтную
            set_setting("active_model", default)
            return default
    if key == "active_image_model":
        from config import AVAILABLE_IMAGE_MODELS
        # Старый выбор Imagen (отключён 17.08.2026) больше не в списке — сбрасываем
        # на дефолт (Nano Banana), иначе бот слал бы запросы на мёртвую модель.
        if val not in AVAILABLE_IMAGE_MODELS:
            set_setting("active_image_model", default)
            return default
    return val


def set_setting(key: str, value: str):
    """Сохраняет текстовое значение настройки в БД."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def delete_setting(key: str) -> None:
    """Удаляет настройку ЦЕЛИКОМ (строку из settings), а не обнуляет её.

    Нужна там, где «значение не задано» и «значение равно нулю» — РАЗНЫЕ вещи:
    остаток счёта и остаток квоты Qwen. Пока ключа нет, вычитающие UPDATE
    в add_*_cost и spend_qwen_tokens молча не находят строку и ничего не портят;
    если же вместо удаления записать пустоту или ноль, CAST('' AS REAL) даст 0
    и остаток начнёт уходить в минус с первого же запроса.
    Зовётся из кнопки «убрать значение» на экране «💰 Счета и квоты».
    """
    with _lock:
        conn = _get_connection()
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
        conn.commit()


def get_active_system_prompt() -> tuple:
    """
    Возвращает текущий активный системный промпт.
    
    Логика:
      1. Если в БД есть custom_system_prompt → используем его как базу
      2. Иначе → используем SYSTEM_PROMPT из config.py
      3. Если в БД есть prompt_additions → дописываем их в конец базы
    
    Возвращает кортеж (prompt: str, is_custom: bool, additions: str)
    """
    from config import SYSTEM_PROMPT
    
    custom = get_setting("custom_system_prompt", "")
    additions = get_setting("prompt_additions", "")
    
    if custom:
        base = custom
        is_custom = True
    else:
        base = SYSTEM_PROMPT
        is_custom = False
    
    if additions:
        base += "\n\n" + additions
    
    return (base, is_custom, additions)


def append_prompt_addition(text: str):
    """
    Дописывает дополнительные правила/инструкции к промпту.
    Несколько вызовов накапливаются через перевод строки.
    """
    current = get_setting("prompt_additions", "")
    if current:
        new_value = current + "\n" + text
    else:
        new_value = text
    set_setting("prompt_additions", new_value)


def get_news_system_prompt() -> str:
    """
    Возвращает системный промпт для форматирования новостей (format_news_as_colonel).
    Хранится в settings под ключом 'news_system_prompt'.
    Если не задан — возвращает пустую строку (бот работает без системного промпта).
    """
    return get_setting("news_system_prompt", "")


def get_rag_instruction() -> str:
    """
    Возвращает «шапку»-инструкцию, которая уходит модели перед найденными
    статьями RAG (services/gemini.py дописывает под неё сами статьи).

    Живой текст хранится в settings под ключом 'rag_instruction'.

    ⚠️ ЗАВОДСКОГО ТЕКСТА БОЛЬШЕ НЕТ (2026-08-16, решение Максима):
    config.RAG_INSTRUCTION пуст, как SYSTEM_PROMPT. Не задал или сбросил —
    вернётся пустая строка, и статьи уйдут модели без всякой шапки
    (services/gemini.py::_rag_block такую пустоту умеет). Фолбэк на константу
    оставлен на случай, если заводской текст однажды вернут.
    """
    from config import RAG_INSTRUCTION
    return get_setting("rag_instruction", "").strip() or RAG_INSTRUCTION


def get_proactive_instruction() -> str:
    """
    Возвращает инструкцию участия в разговоре групп (проактивный режим,
    services/proactive.py): по ней модель решает, вступить ли в беседу.

    Живой текст хранится в settings под ключом 'proactive_instruction'.

    ⚠️ ЗАВОДСКОГО ТЕКСТА БОЛЬШЕ НЕТ (2026-08-16, решение Максима):
    config.PROACTIVE_INSTRUCTION пуст, как SYSTEM_PROMPT. Не задал или
    сбросил — вернётся пустая строка: правил участия и блока рук у модели не
    будет вовсе (молчать она всё равно умеет — слово ПРОПУСК называет сам
    запрос). Фолбэк на константу оставлен на случай возврата заводского текста.
    """
    from config import PROACTIVE_INSTRUCTION
    return get_setting("proactive_instruction", "").strip() or PROACTIVE_INSTRUCTION


def get_author_brief_instruction() -> str:
    """
    Возвращает вступление справки об авторе — текст, который уходит модели
    ПЕРЕД данными участника (services/gemini.py::_who_is_talking дописывает
    под ним имя, ник, роль и звание).

    Живой текст хранится в settings под ключом 'author_brief_instruction'.

    ⚠️ ЗАВОДСКОГО ТЕКСТА БОЛЬШЕ НЕТ (2026-08-16, решение Максима):
    config.AUTHOR_BRIEF_INSTRUCTION пуст, как SYSTEM_PROMPT. Не задал или
    сбросил — вернётся пустая строка, и модель получит голую строку «Имя —
    Владелец» без пояснения, что это и зачем (зачитает вслух). Фолбэк на
    константу оставлен на случай возврата заводского текста.
    """
    from config import AUTHOR_BRIEF_INSTRUCTION
    return get_setting("author_brief_instruction", "").strip() or AUTHOR_BRIEF_INSTRUCTION
