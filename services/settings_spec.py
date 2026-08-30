# ───────────────────────────────────────────────
#  services/settings_spec.py — ЕДИНЫЙ список простых настроек бота
#  (30.08.2026, этап 1 веб-админки).
#
#  Зачем он появился. Одну и ту же настройку теперь крутят ДВА места: кнопки
#  в Telegram и страница сайта. Пределы («порог от 2 до 50, шаг 1») жили
#  внутри панелей — тремя отдельными табличками, а у базы знаний прямо в теле
#  ветки. Сайт со своей четвёртой копией разъехался бы с кнопками через
#  месяц-другой, и заметить это было бы нечем.
#
#  Поэтому пределы, шаги и начальные значения ТЕПЕРЬ ЗДЕСЬ, а панели и сайт
#  их только читают. Правишь предел — правишь в одном месте.
#
#  ⚠️ ЭТОТ МОДУЛЬ НЕ ЗНАЕТ О ПОБОЧНЫХ ДЕЙСТВИЯХ. Он умеет только прочитать и
#  записать значение. Всё, что должно случиться ВОКРУГ правки — запись в
#  журнал персонала, объявление в группы при выключении «Сам в разговор»,
#  перерисовка панели — остаётся у того, кто зовёт. Иначе сюда переехала бы
#  половина бота.
#
#  ⚠️ ЗДЕСЬ НЕ ВСЕ НАСТРОЙКИ БОТА, а только «простые»: тумблер и число.
#  Выбор модели, глубина раздумий, промпты и всё, у чего своя шкала или свой
#  разбор, живут по своим местам — приводить их к общему виду ради
#  единообразия значит спрятать различия, которые важны.
#
#  ⚠️ Начальные значения обязаны СОВПАДАТЬ с теми, что стоят у читателей
#  настройки (services/antispam.py, services/greeter.py, utils_format.py …).
#  Разъедутся — бот и панель будут показывать разное до первого нажатия.
#  За этим следит selftest.py::check_settings_spec: он сверяет каждый тумблер
#  с его настоящей читалкой, а не верит комментарию.
# ───────────────────────────────────────────────

import logging

from config import (ANTISPAM_ENABLED_DEFAULT, ANTISPAM_MSG_COUNT,
                    ANTISPAM_MUTE_SEC, ANTISPAM_WINDOW_SEC,
                    AUTO_UPDATE_ENABLED_DEFAULT, GREET_CAPTCHA_DEFAULT,
                    GREET_ENABLED_DEFAULT, GREET_KICK_DEFAULT,
                    GREET_TIMEOUT_SEC, LINKFILTER_ENABLED_DEFAULT,
                    PROACTIVE_CONTEXT_MSGS, PROACTIVE_ENABLED_DEFAULT,
                    PROACTIVE_HANDS_DEFAULT, PROACTIVE_MIN_MSGS,
                    RAG_MIN_SIMILARITY, RAG_PEAK_MARGIN, RAG_TOP_K)
from database.history import get_setting, set_setting

logger = logging.getLogger(__name__)


# Разделы — порядок здесь задаёт порядок блоков на странице сайта.
SECTIONS = (
    ("answers",   "💬 Ответы бота"),
    ("antispam",  "🛡 Антиспам"),
    ("greet",     "👋 Приветствие новичков"),
    ("rag",       "📚 База знаний"),
    ("proactive", "🗣 Сам в разговор"),
    ("system",    "⚙️ Обслуживание"),
)


# ⚠️ Ключ словаря = ключ в таблице settings. Совпадение обязательно: по нему
# и панель, и сайт, и читатели настройки находят одно и то же значение.
#
#   kind    — "toggle" (да/нет), "int" (целое), "float" (дробное)
#   default — начальное значение В ТОМ ЖЕ ВИДЕ, что у читателя настройки
#   min/max/step — пределы и шаг кнопок ➖/➕ (у тумблеров их нет)
#   digits  — сколько знаков после запятой писать в settings (только float)
#   unit    — подпись к числу на сайте
SPEC = {
    # ─── 💬 Ответы бота ───
    "ai_replies_enabled": {
        "section": "answers", "kind": "toggle", "default": "1",
        "title": "Ответы ИИ",
        "hint": "выключено — бот молчит на обычные сообщения",
    },
    "thoughts_enabled": {
        "section": "answers", "kind": "toggle", "default": "1",
        "title": "Показывать мысли модели",
        # ⚠️ Тумблер про ПОКАЗ, а не про расход: модели думают в любом случае.
        "hint": "свёрнутая цитата рассуждений под ответом",
    },

    # ─── 🛡 Антиспам ───
    "antispam_enabled": {
        "section": "antispam", "kind": "toggle", "default": ANTISPAM_ENABLED_DEFAULT,
        "title": "Антиспам",
        "hint": "мут за флуд в группах",
    },
    "linkfilter_enabled": {
        "section": "antispam", "kind": "toggle", "default": LINKFILTER_ENABLED_DEFAULT,
        "title": "Фильтр ссылок",
        "hint": "удалять чужие ссылки в группах",
    },
    "antispam_msg_count": {
        "section": "antispam", "kind": "int", "default": ANTISPAM_MSG_COUNT,
        "min": 2, "max": 50, "step": 1, "unit": "сообщ.",
        "title": "Порог флуда",
        "hint": "сколько сообщений за окно считается флудом",
    },
    "antispam_window_sec": {
        "section": "antispam", "kind": "int", "default": ANTISPAM_WINDOW_SEC,
        "min": 2, "max": 60, "step": 1, "unit": "сек",
        "title": "Окно засчёта",
        "hint": "за какой срок считаются те сообщения",
    },
    "antispam_mute_sec": {
        "section": "antispam", "kind": "int", "default": ANTISPAM_MUTE_SEC,
        "min": 30, "max": 86400, "step": 60, "unit": "сек",
        "title": "Длительность мута",
        "hint": "насколько бот затыкает нарушителя",
    },

    # ─── 👋 Приветствие новичков ───
    "greet_enabled": {
        "section": "greet", "kind": "toggle", "default": GREET_ENABLED_DEFAULT,
        "title": "Приветствие новичков",
        "hint": "здороваться с теми, кто зашёл в группу",
    },
    "greet_captcha": {
        "section": "greet", "kind": "toggle", "default": GREET_CAPTCHA_DEFAULT,
        "title": "Проверка «я не бот»",
        "hint": "новичок должен нажать кнопку",
    },
    "greet_kick": {
        "section": "greet", "kind": "toggle", "default": GREET_KICK_DEFAULT,
        "title": "Кикать не прошедших",
        "hint": "выгонять тех, кто кнопку не нажал",
    },
    "greet_timeout_sec": {
        "section": "greet", "kind": "int", "default": GREET_TIMEOUT_SEC,
        # От минуты (успеет только живой человек, уже открывший чат) до часа
        # (человек мог зайти и отложить телефон).
        "min": 60, "max": 3600, "step": 60, "unit": "сек",
        "title": "Срок на проверку",
        "hint": "сколько ждём нажатия кнопки",
    },

    # ─── 📚 База знаний ───
    "rag_enabled": {
        # ⚠️ Начальное «1» взято из кнопки kb_myrag, а НЕ из config.RAG_ENABLED:
        # тот приходит из .env и означает «есть ли база вообще», а этот тумблер —
        # «подмешивать ли статьи прямо сейчас».
        "section": "rag", "kind": "toggle", "default": "1",
        "title": "База знаний",
        "hint": "подмешивать статьи в ответ",
    },
    "rag_min_similarity": {
        "section": "rag", "kind": "float", "default": RAG_MIN_SIMILARITY,
        # Шаг 0.02: зазор между болтовнёй и настоящими вопросами узкий
        # (~0.59 против ~0.60 по замерам 05.07.2026), 0.05 слишком грубо.
        "min": 0.05, "max": 0.95, "step": 0.02, "digits": 2, "unit": "",
        "title": "Порог сходства",
        "hint": "насколько статья должна быть похожа на вопрос",
    },
    "rag_top_k": {
        "section": "rag", "kind": "int", "default": RAG_TOP_K,
        "min": 1, "max": 10, "step": 1, "unit": "шт.",
        "title": "Статей в ответ",
        "hint": "сколько кусков подмешивать модели",
    },
    "rag_peak_margin": {
        "section": "rag", "kind": "float", "default": RAG_PEAK_MARGIN,
        # 0 = правило выключено, проходит всё, что выше порога сходства.
        "min": 0.0, "max": 0.30, "step": 0.01, "digits": 2, "unit": "",
        "title": "Запас над фоном",
        "hint": "насколько лучшая статья должна обойти остальные",
    },

    # ─── 🗣 Сам в разговор ───
    "proactive_enabled": {
        "section": "proactive", "kind": "toggle", "default": PROACTIVE_ENABLED_DEFAULT,
        "title": "Сам в разговор",
        # ⚠️ У этого тумблера ЕСТЬ побочное действие: при выключении бот шлёт
        # объявление во все известные группы, при включении — убирает его.
        # Делает это вызывающий (панель промптов и web/actions.py), не этот файл.
        "hint": "бот сам вступает в беседу; при выключении объявляет об этом группам",
    },
    "proactive_hands": {
        "section": "proactive", "kind": "toggle", "default": PROACTIVE_HANDS_DEFAULT,
        "title": "Руки",
        "hint": "разрешить боту самому выдавать мут",
    },
    "proactive_min_msgs": {
        "section": "proactive", "kind": "int", "default": PROACTIVE_MIN_MSGS,
        "min": 1, "max": 20, "step": 1, "unit": "сообщ.",
        "title": "Порог вмешательства",
        "hint": "сколько новых сообщений нужно между проверками",
    },
    "proactive_context_msgs": {
        "section": "proactive", "kind": "int", "default": PROACTIVE_CONTEXT_MSGS,
        "min": 5, "max": 50, "step": 5, "unit": "сообщ.",
        "title": "Стенограмма",
        "hint": "сколько последних сообщений видит модель",
    },

    # ─── ⚙️ Обслуживание ───
    "auto_update_enabled": {
        "section": "system", "kind": "toggle", "default": AUTO_UPDATE_ENABLED_DEFAULT,
        "title": "Самообновление",
        "hint": "забирать новый код с GitHub раз в 5 минут",
    },
}


# ─── чтение ─────────────────────────────────────────────────────────

def read(key: str):
    """
    Текущее значение настройки в её собственном виде: bool / int / float.
    Мусор в базе (правка руками) = начальное значение, а не падение.
    """
    item = SPEC[key]
    kind = item["kind"]
    raw = get_setting(key, _default_str(item))
    if kind == "toggle":
        return raw == "1"
    try:
        return int(raw) if kind == "int" else float(raw)
    except (TypeError, ValueError):
        logger.warning("⚙️ Настройка %s испорчена (%r) — беру начальное", key, raw)
        return item["default"]


def _default_str(item) -> str:
    """Начальное значение строкой — таблица settings хранит только строки."""
    default = item["default"]
    if item["kind"] == "float":
        return f"{default:.{item.get('digits', 2)}f}"
    return str(default)


def display(key: str) -> str:
    """Значение так, как его показывают человеку (с единицей измерения)."""
    item = SPEC[key]
    value = read(key)
    if item["kind"] == "toggle":
        return "включено" if value else "выключено"
    unit = item.get("unit", "")
    if item["kind"] == "float":
        text = f"{value:.{item.get('digits', 2)}f}"
    else:
        text = str(value)
    return f"{text} {unit}".strip()


# ─── запись ─────────────────────────────────────────────────────────

def _store(key: str, value) -> None:
    """Кладёт значение в settings в том виде, в каком его ждут читатели."""
    item = SPEC[key]
    if item["kind"] == "float":
        set_setting(key, f"{value:.{item.get('digits', 2)}f}")
    elif item["kind"] == "toggle":
        set_setting(key, "1" if value else "0")
    else:
        set_setting(key, str(value))


def _clamp(item, value):
    """Загоняет число в пределы. Дробное заодно округляем до своих знаков —
    иначе арифметика с плавающей точкой оставит 0.13999999999999999."""
    value = max(item["min"], min(item["max"], value))
    if item["kind"] == "float":
        value = round(value, item.get("digits", 2))
    return value


def toggle(key: str) -> bool:
    """Переключает тумблер и возвращает НОВОЕ состояние."""
    if SPEC[key]["kind"] != "toggle":
        raise ValueError(f"{key} — не тумблер")
    new_value = not read(key)
    _store(key, new_value)
    return new_value


def adjust(key: str, steps: int):
    """
    Меняет число на steps шагов (кнопки ➖/➕) в пределах спецификации.
    Возвращает НОВОЕ значение — оно может совпасть со старым, если упёрлись
    в границу, и вызывающий обязан это учитывать: перерисовка «в то же самое»
    даёт ошибку Telegram «Message is not modified».
    """
    item = SPEC[key]
    if item["kind"] == "toggle":
        raise ValueError(f"{key} — тумблер, шагами не меняется")
    new_value = _clamp(item, read(key) + item["step"] * steps)
    _store(key, new_value)
    return new_value


def write(key: str, raw) -> object:
    """
    Ставит значение НАПРЯМУЮ (поле или ползунок на сайте, а не шаг кнопкой).
    Возвращает то, что реально записано: значение подрезается по пределам и
    прижимается к ближайшему шагу.

    ⚠️ Прижатие к шагу обязательно. Иначе через сайт можно поставить порог
    2.37 при шаге 0.02, и кнопки ➖/➕ в Telegram потом всю жизнь ходили бы
    по сдвинутой сетке — расхождение, которое ищут часами.

    ⚠️ СЕТКА СЧИТАЕТСЯ ОТ НАЧАЛЬНОГО ЗНАЧЕНИЯ, А НЕ ОТ МИНИМУМА. Это не
    придирка: кнопки ➖/➕ шагают от того, что стоит сейчас, а стоит там
    поначалу начальное значение. У мута начальное 300 при минимуме 30 и шаге
    60 — от минимума сетка дала бы 30/90/150, от начального 300/360/240.
    Сетка от минимума разошлась бы с кнопками у двух настроек из девяти,
    и поймала это проверка selftest, а не глаз.
    """
    item = SPEC[key]
    kind = item["kind"]

    if kind == "toggle":
        value = str(raw).strip().lower() in ("1", "true", "on", "да")
        _store(key, value)
        return value

    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: «{raw}» — не число")

    anchor = float(item["default"])
    steps = round((value - anchor) / item["step"])
    value = anchor + steps * item["step"]
    if kind == "int":
        value = int(round(value))
    value = _clamp(item, value)
    _store(key, value)
    return value


# ─── помощники для тех, кто рисует экраны ───────────────────────────

def keys_of(section: str) -> list:
    """Ключи одного раздела в порядке объявления в SPEC."""
    return [k for k, v in SPEC.items() if v["section"] == section]


def title(key: str) -> str:
    return SPEC[key]["title"]
