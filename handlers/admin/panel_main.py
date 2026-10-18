# ───────────────────────────────────────────────
#  handlers/admin/panel_main.py — главная панель /adm, панель статистики /stats и панель моделей «📡 Настройки API» (раскладка кнопок моделей).
#  Выделен из монолитного admin.py 2026-07-13 разрезом БЕЗ изменения логики.
# ───────────────────────────────────────────────
import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import (AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, GEMINI_MODEL, PROVIDERS,
                    BOT_VERSION_HTML, THINKING_LEVELS, THINKING_PHASES)
from database.history import get_setting, get_bot_stats
from utils import register_and_clean_bot_message, delete_user_message_safe


logger = logging.getLogger(__name__)
from .common import (_adm_back_row, _filter_keyboard, _is_group_chat, _onoff,
                     _require, _send_panel_message)
from .panel_rag import _end_kb_test




# Раскладка кнопок текстовых моделей: 4 ряда, по 2 колонки или одной длинной
# кнопкой. Собрана ВРУЧНУЮ
# по конкретным ключам AVAILABLE_MODELS: добавил модель в config.py — добавь
# её ключ сюда, иначе кнопка НЕ появится.
# Правила раскладки — решения Максима (как ряды переставляли по датам — в
# references/history.md навыка project-map):
#   • Gemini разложены «по столбцам» — в рядах это даёт пары ниже;
#   • новинка без пары ставится своей строкой: ряд из одной кнопки Telegram
#     растягивает на всю ширину, и она читается как «самая новая» (так стоят
#     Gemini 3.8 Flash и DeepSeek V4.1 Flash; «строго парами» и «новинки
#     первым рядом» Максим пробовал и отменил);
#   • удалённая модель уходит вместе со своим рядом, остальные ряды и порядок
#     кнопок не двигаются.
# 2026-10-10: удалены gemini-3.5-flash и gemini-3.7-flash (Google снял обе).
# 2026-10-18: удалён ряд Qwen (qwen3.8-max-0902 / qwen3.8-flash) — из бота
# убран весь провайдер Alibaba, решение Максима. Стало 4 ряда.
_MODEL_BUTTON_ROWS = [
    ["gemini-3.5-flash-lite", "gemini-3.6-flash"],
    ["gemini-3.8-flash"],
    ["deepseek-flash"],
    ["mimo-v2.6-flash", "mimo-v2.6-pro"],
]

# Ряд моделей картинок (Nano Banana, gemini-*-image).
_IMAGE_BUTTON_ROW = ["gemini-3.1-flash-lite-image", "gemini-nano-banana-2.1"]


def _build_model_panel_text_and_keyboard(active_model, active_image_model, user_id=None):
    # Текстовая часть панели моделей убрана: активная модель видна прямо на
    # кнопке (✅). Возвращаем только клавиатуру, текст — пустой.
    # Надписи кнопок = человекочитаемое имя модели (поле "name" в config) — то же
    # имя показывается в уведомлении о смене модели. Значки 🧠/🖼 убраны 2026-07-05.
    keyboard = []
    for row in _MODEL_BUTTON_ROWS:
        buttons = []
        for key in row:
            name = AVAILABLE_MODELS.get(key, {}).get("name", key)
            label = f"✅ {name}" if key == active_model else name
            buttons.append(InlineKeyboardButton(label, callback_data=f"set_model:{key}"))
        keyboard.append(buttons)

    img_buttons = []
    for key in _IMAGE_BUTTON_ROW:
        name = AVAILABLE_IMAGE_MODELS.get(key, {}).get("name", key)
        label = f"✅ {name}" if key == active_image_model else name
        img_buttons.append(InlineKeyboardButton(label, callback_data=f"set_image_model:{key}"))
    keyboard.append(img_buttons)

    return "", InlineKeyboardMarkup(keyboard)


# Ряд отчётов о расходах. Стоит именно в панели API (решение Максима
# 2026-07-25, раньше «📊 Отчёт за вчера» жила в /adm): отчёты собраны ровно из
# счётчиков этой панели и читаются с ней один в один. В _CALLBACK_RULES этих
# кнопок НЕТ намеренно — запрет по умолчанию оставляет их владельцу, как и всю
# панель API.
_REPORT_BUTTON_ROW = [
    InlineKeyboardButton("📊 Отчёт за вчера", callback_data="adm_daily_report"),
    InlineKeyboardButton("📅 Отчёт за неделю", callback_data="adm_weekly_report"),
]


def _thinking_rows():
    """
    Ряды кнопок «<значок провайдера> <имя> <фаза луны> <глубина>» — по одной
    на провайдера (29.08.2026, решение Максима «по кнопке на джемини, квен,
    дипсик и мимо»; кнопка Qwen ушла 18.10.2026 вместе с провайдером). Пример
    живой надписи: «🐋 DeepSeek 🌔 Высокая».

    ⚠️ ПОЧЕМУ НЕ ОДНА ОБЩАЯ КНОПКА. Провайдеры умеют разное, замерено живыми
    запросами: у Gemini четыре ступени, у DeepSeek свои три (low/high/max),
    а у MiMo ступеней нет вовсе, только «думает / не думает». Общая кнопка на четыре
    положения обещала бы четыре, а выполняла бы два на MiMo. Подробности и
    числа — config.THINKING_LEVELS.

    Порядок провайдеров и набор положений берутся ИЗ РЕЕСТРА (config), имена и
    значки — из PROVIDERS: второго списка ни того, ни другого здесь нет, иначе
    добавление провайдера пришлось бы помнить в двух местах.

    ⚠️ ЗНАЧКОВ НА КНОПКЕ ДВА, И У КАЖДОГО СВОЯ РАБОТА (выбор Максима
    29.08.2026 из трёх раскладок). Первый — значок ПРОВАЙДЕРА из PROVIDERS,
    тот же, что стоит в строках лога: по нему видно, чья кнопка, и кнопка
    связывается с «♊️ Ответ от …» в журнале. Второй — фаза ЛУНЫ, шкала
    заполнения: новолуние «не думает», полнолуние «в полную силу». Так
    положение всех четырёх читается взглядом, без чтения слов.

    ⚠️ 🧠 здесь не годится, хотя просится: под ним в этой же панели живёт
    тумблер показа мыслей, а он про совсем другое — прячет цитату, а не
    меняет глубину. Два разных смысла под одним значком в одной панели —
    готовая путаница.
    """
    from services.gemini import thinking_level

    rows, pair = [], []
    for provider, levels in THINKING_LEVELS.items():
        meta = PROVIDERS.get(provider, {})
        title = meta.get("title", provider)
        icon = meta.get("icon", "")
        codes = [code for code, _ in levels]
        cur = thinking_level(provider)
        pos = codes.index(cur) if cur in codes else 0
        # Фаза по МЕСТУ в шкале: у провайдеров разное число ступеней и разные
        # их названия, а полнолуние обязано означать верх у каждого.
        # Незнакомая длина шкалы (новый провайдер) — не повод падать: тогда
        # рисуем только края, новолуние и полнолуние.
        phases = THINKING_PHASES.get(len(codes))
        if phases:
            phase = phases[pos]
        else:
            phase = "🌑" if pos == 0 else "🌕"
        pair.append(InlineKeyboardButton(
            f"{icon} {title} {phase} {dict(levels)[codes[pos]]}",
            callback_data=f"think:{provider}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return rows


# ─── Переключатели показа ответа и склейка в «📡 Настройках API» ───
# Общие шаги кнопок под панелью (router.py) и нижнего подменю владельца
# (2026-10-18, panel_keys.py): надпись, переключение, строка лога, журнал и
# ответ — одни на обе кнопки. Второго набора правил «как переключать» быть
# не должно.

def api_toggle_text(code: str) -> str:
    """
    Надпись переключателя с состоянием — одна под панелью и внизу.
    code: thoughts | live_answer | live_thoughts | batch_info.
    """
    if code == "thoughts":
        # Мысли под капотом (2026-08-03; 2026-08-04 переехал сюда из панели
        # промптов по просьбе Максима — его место рядом с выбором модели).
        # Настройка ОБЩАЯ: выключена — свёрнутой цитаты с рассуждениями нет ни
        # в личке, ни в группах, ни в режиме «Сам в разговор». Состояние
        # читаем через utils_format — там же, где оно и применяется.
        from utils_format import thoughts_enabled
        return f"🧠 МЫСЛИ ПОД КАПОТОМ: {_onoff(thoughts_enabled())}"
    if code == "live_answer":
        # Ответ на глазах (2026-10-08) — тот же выключатель, что на сайте.
        from services.live_answer import live_answer_enabled
        return f"✍️ ОТВЕТ НА ГЛАЗАХ: {_onoff(live_answer_enabled())}"
    if code == "live_thoughts":
        # Мысли в черновике лички (2026-10-14) — свой выключатель, чтобы
        # вернуть «как было» без выкатки; тот же, что на сайте.
        from services.live_answer import live_thoughts_enabled
        return f"🧠 МЫСЛИ В ЧЕРНОВИКЕ: {_onoff(live_thoughts_enabled())}"
    if code == "batch_info":
        # Склейка сообщений подряд (2026-10-09, services/message_batch.py).
        from services.message_batch import wait_sec
        batch = wait_sec()
        return f"🧩 СКЛЕЙКА: {f'{batch} с' if batch else '🔴ВЫКЛ'}"
    raise ValueError(f"нет такого переключателя: {code}")


def flip_api_toggle(code: str, user_id: int) -> str:
    """
    Переключить выключатель показа (code: thoughts | live_answer |
    live_thoughts). Возвращает, что сказать: под панелью — всплывающим
    окном, внизу — коротким сообщением. Коды журнала — те же, что у сайта.

    ⚠️ «Мысли под капотом» — про ПОКАЗ, а не про расход: модели думают и
    тратят токены по-прежнему — обещать здесь экономию нельзя.
    """
    from database.history import set_setting
    from .common import _audit
    if code == "thoughts":
        from utils_format import THOUGHTS_SETTING_KEY, thoughts_enabled
        new_val = "0" if thoughts_enabled() else "1"
        set_setting(THOUGHTS_SETTING_KEY, new_val)
        state = "показываются" if new_val == "1" else "скрыты"
        logger.info("🔧 Админ %s: мысли под капотом %s (для всех)", user_id, state)
        _audit(user_id, "thoughts", 0, f"мысли под капотом {state}")
        return (f"🧠 Мысли под капотом {state}."
                + ("" if new_val == "1" else " Модели всё равно думают — прячется только цитата."))
    if code == "live_answer":
        from services.live_answer import SETTING_KEY, live_answer_enabled
        new_val = "0" if live_answer_enabled() else "1"
        set_setting(SETTING_KEY, new_val)
        state = "включён" if new_val == "1" else "выключен"
        logger.info("🔧 Админ %s: ответ на глазах %s", user_id, state)
        _audit(user_id, "live_answer", 0, f"ответ на глазах {state}")
        return ("✍️ Ответ на глазах включён: в личке и в группах ответ дописывается по ходу."
                if new_val == "1" else
                "✍️ Ответ на глазах выключен: везде снова «печатает…» и готовый ответ.")
    if code == "live_thoughts":
        from services.live_answer import THOUGHTS_SETTING_KEY, live_thoughts_enabled
        new_val = "0" if live_thoughts_enabled() else "1"
        set_setting(THOUGHTS_SETTING_KEY, new_val)
        state = "включены" if new_val == "1" else "выключены"
        logger.info("🔧 Админ %s: мысли в черновике %s", user_id, state)
        _audit(user_id, "live_thoughts", 0, f"мысли в черновике {state}")
        return ("🧠 Мысли в черновике включены: в личке мысли видны по ходу над ответом. "
                "Нужны включённые «Мысли под капотом» и «Ответ на глазах»."
                if new_val == "1" else
                "🧠 Мысли в черновике выключены: мысли снова только в готовом ответе.")
    raise ValueError(f"нет такого переключателя: {code}")


BATCH_INFO_TEXT = ("🧩 Склейка: бот ждёт столько секунд, не допишет ли человек ещё, и "
                   "отвечает одним сообщением на всё. 0 — выключено: ответ на каждое сразу.")


def step_batch(user_id: int, steps: int) -> str | None:
    """
    Шаг регулятора склейки (+1 / −1). Пределы и шаг — services/settings_spec.py,
    тот же регулятор на сайте. Возвращает, что сказать; None — упёрлись в
    предел и ничего не поменялось (перерисовка «в то же самое» дала бы ошибку
    Telegram «Message is not modified»).
    """
    from services.message_batch import SETTING_KEY, wait_sec
    from services.settings_spec import adjust
    from .common import _audit
    before = wait_sec()
    after = adjust(SETTING_KEY, steps)
    if after == before:
        return None
    logger.info("🔧 Админ %s: склейка сообщений %s → %s с", user_id, before, after)
    _audit(user_id, "batch", 0, f"склейка сообщений: {after} с" if after else "склейка выключена")
    return f"🧩 Склейка: {after} с" if after else "🧩 Склейка выключена"


def api_toggles_below(user_id: int) -> bool:
    """
    Переключатели раздела стоят в нижнем подменю владельца (2026-10-18)?
    Тогда под панелью их нет. С 18.10.2026 нижние кнопки у владельца есть
    всегда, значит и это — всегда; под панелью переключатели остаются только
    в полном варианте (_build_api_keyboard(…, full=True)) для preflight.
    """
    from services.roles import is_owner
    return is_owner(user_id)


def _build_api_keyboard(user_id, full: bool = False):
    """
    Клавиатура панели «📡 Настройки API»: кнопки выбора моделей + глубина
    раздумий по провайдерам + переключатели показа и склейка + отчёты о
    расходах + вход на экран «💰 Счета» + возврат в /adm.
    Вызывается при открытии панели и при обновлении клавиатуры после смены
    модели или глубины.

    ⚠️ У владельца с нижними кнопками переключателей под панелью НЕТ — они в
    нижнем подменю (api_toggles_below). full=True — полный вариант всегда:
    его меряет preflight и сторожат проверки надписей.
    """
    active_model = get_setting("active_model", GEMINI_MODEL)
    active_image_model = get_setting("active_image_model", next(iter(AVAILABLE_IMAGE_MODELS)))
    _, model_markup = _build_model_panel_text_and_keyboard(active_model, active_image_model, user_id)
    # inline_keyboard у собранной клавиатуры — кортеж (tuple), складывать его
    # со списком нельзя — приводим всё к спискам.
    rows = list(model_markup.inline_keyboard) + _thinking_rows()
    if full or not api_toggles_below(user_id):
        # Своими рядами: надписи длинные, в половину ширины не влезают.
        # Все про то, КАК показывается ответ, — потому рядом друг с другом.
        rows += [
            [InlineKeyboardButton(api_toggle_text("thoughts"), callback_data="toggle_thoughts")],
            [InlineKeyboardButton(api_toggle_text("live_answer"),
                                  callback_data="toggle_live_answer")],
            [InlineKeyboardButton(api_toggle_text("live_thoughts"),
                                  callback_data="toggle_live_thoughts")],
            [
                InlineKeyboardButton("➖", callback_data="batch_dec"),
                InlineKeyboardButton(api_toggle_text("batch_info"), callback_data="batch_info"),
                InlineKeyboardButton("➕", callback_data="batch_inc"),
            ],
        ]
    rows += [
        list(_REPORT_BUTTON_ROW),
        [InlineKeyboardButton("💰 СЧЕТА", callback_data="bal:panel")],
        _adm_back_row(),
    ]
    return InlineKeyboardMarkup(rows)


def stats_lines() -> str:
    """
    Три сводные цифры — одни в сообщении с версией у владельца
    (panel_keys.carrier_text, с 18.10.2026) и в панели статистики.
    """
    stats = get_bot_stats()
    return (
        # «за этот месяц» — user_token_usage обнуляется месячным сбросом
        # (jobs/cleanup.py::_monthly_stats_reset); уберёшь сброс — верни «за всё время».
        f"💬 <b>Обменов «вопрос-ответ» за этот месяц: {stats['lifetime_requests']}</b>\n"
        f"👥 <b>Подписчиков на новости: {stats['subscriptions']}</b>\n"
        # «10 дней» = срок автоочистки архива в jobs/cleanup.py::cleanup_loop;
        # меняешь срок там — поменяй подпись здесь.
        f"📝 <b>Архив группы за последние 10 дней: {stats['group_msg_count']}</b>"
    )


async def send_stats_panel(bot, chat_id: int, user_id: int):
    """
    «💬 СТАТИСТИКА»: три сводные цифры и кнопки обновлений, дайджеста и копии
    базы (команда /stats, нижняя «🎛 Админ-панель», «⬅️ Назад» с экранов
    обновлений и дайджеста, тумблер дайджеста).

    ⚠️ У ВЛАДЕЛЬЦА В ЛИЧКЕ — СООБЩЕНИЕ С ВЕРСИЕЙ (18.10.2026, решение Максима,
    вариант Б: «🎛 Админ-панель» и «💬 Статистика» — одно). Цифры — в нём
    (panel_keys.carrier_text), кнопки этой панели — внизу, подменю service;
    открытый раздел убирается, как у /adm. Прежняя панель ниже — для всех
    остальных случаев; на деле статистика владельческая и туда никто не
    попадает, но её кнопки сверяет с роутером preflight.
    """
    from services import roles
    if chat_id == user_id and roles.is_owner(user_id):
        from utils import clean_bot_messages
        from .panel_keys import send_owner_keys
        await send_owner_keys(bot, chat_id, user_id, menu="service")
        await clean_bot_messages(bot, chat_id)
        return
    text = (
        f"💬 <b>СТАТИСТИКА</b>\n"
        f"───────────────────────────\n"
        + stats_lines()
    )
    await _send_panel_message(bot, chat_id, text, _stats_keyboard())


def _stats_keyboard():
    """Кнопки прежней панели статистики — те же, что в подменю «🎛 Админ-панель»."""
    # 📊 Недельный дайджест группы (2026-08-04) живёт ИМЕННО ЗДЕСЬ, а не в
    # /mod: он про жизнь чата, а не про наказания, и собирается ровно из тех
    # данных, о которых эта панель и рассказывает.
    # ⬇️ «Обновления» (2026-08-04, просьба Максима) — тоже здесь: это
    # статистика про самого бота (сколько правок и когда), а каждая строка
    # экрана открывается ссылкой на GitHub.
    # 💾 «Копия базы» (2026-08-10, просьба Максима) переехала СЮДА из главной
    # панели /adm — снимает копию прямо сейчас и присылает файлом. Кнопка
    # владельческая (в _CALLBACK_RULES её нет), как и вся эта панель, поэтому
    # отбор по правам ей не нужен. Снимает копию send_db_backup ниже — она же
    # у нижней кнопки «💾 Копия базы» владельца.
    # Надпись тумблера дайджеста — panel_digest.digest_toggle_text, одна с
    # нижней кнопкой.
    from .panel_digest import digest_toggle_text
    keyboard = [
        [InlineKeyboardButton("⬇️ Обновления", callback_data="upd:list")],
        [InlineKeyboardButton("📊 Дайджест недели", callback_data="dig:show")],
        [InlineKeyboardButton(digest_toggle_text(), callback_data="dig:toggle")],
        [InlineKeyboardButton("💾 Копия базы", callback_data="adm_backup")],
        _adm_back_row(),
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_db_backup(bot, chat_id: int, user_id: int) -> None:
    """
    Снять копию базы прямо сейчас и прислать файлом, следом — архив статей
    базы знаний. Общая для кнопки «💾 Копия базы» под панелью (router.py,
    ветка adm_backup) и нижней кнопки владельца (18.10.2026, panel_keys.py).
    """
    import asyncio
    import os
    from services import backup
    loop = asyncio.get_running_loop()
    try:
        path, size = await loop.run_in_executor(None, backup.make_backup)
    except Exception as e:
        logger.error("⚠️ Копия базы по кнопке не сделана: %s", e)
        await bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Не удалось сделать копию базы: <code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    logger.info("🔧 Админ %s сделал копию базы вручную", user_id)
    with open(path, "rb") as f:
        blob = f.read()
    fname = os.path.basename(path)
    kept = len(backup.list_backups())
    await bot.send_document(
        chat_id=chat_id,
        document=blob,
        filename=fname,
        caption=(
            f"💾 <b>Копия базы бота</b>\n"
            f"<code>{html.escape(fname)}</code> · {backup.human_size(size)}\n"
            f"<i>Сохрани у себя: промпты, настройки, счета, личные "
            f"дела, звания и журналы — этого нет на GitHub. "
            f"На сервере хранится копий: {kept}.</i>"
        ),
        parse_mode=ParseMode.HTML,
    )
    # Статьи базы знаний — вторым файлом (2026-08-12), как и ночью.
    # Под своим try: копия базы уже у владельца, и сорвавшийся архив
    # статей не должен превратить удачное нажатие в сообщение об ошибке.
    try:
        kb_path, kb_size, kb_ok, kb_wait = await loop.run_in_executor(None, backup.make_kb_backup)
        with open(kb_path, "rb") as f:
            kb_blob = f.read()
        kb_name = os.path.basename(kb_path)
        await bot.send_document(
            chat_id=chat_id,
            document=kb_blob,
            filename=kb_name,
            caption=backup.kb_caption(kb_name, kb_size, kb_ok, kb_wait),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("⚠️ Архив статей базы знаний по кнопке не сделан: %s", e)


async def send_api_panel(bot, chat_id: int, user_id: int):
    """Панель «🤖 УПРАВЛЕНИЕ МОДЕЛЯМИ»: вызовы API по провайдерам + кнопки моделей."""
    from services import daily_report

    stats = get_bot_stats()

    # Раскладываем счётчики вызовов по провайдерам (поле "provider" в config.py).
    # Модели картинок — в ОТДЕЛЬНУЮ группу "image" (не смешивать с бесплатным
    # текстовым Gemini); модели, которых уже нет в конфиге, попадают только в
    # общий счётчик «Общие вызовы API».
    groups = {pid: "" for pid in PROVIDERS}   # порядок блоков задаёт реестр
    for model_name, cnt in stats["api_calls_by_model"]:
        if model_name in AVAILABLE_IMAGE_MODELS:
            provider = "image"
        else:
            provider = AVAILABLE_MODELS.get(model_name, {}).get("provider")
        if provider in groups:
            groups[provider] += f"  • <code>{model_name}</code>: <b>{cnt}</b>\n"

    for provider in groups:
        if not groups[provider]:
            groups[provider] = "  • нет данных\n"

    # Блоки провайдеров собираются ИЗ РЕЕСТРА `config.PROVIDERS` (2026-08-03):
    # порядок блоков, значки, заголовки, ключи копилок и ссылки на кабинеты —
    # всё оттуда. Раньше шесть блоков были выписаны здесь руками, и провайдер,
    # забытый в этом тексте, просто не показывал своих вызовов — молча.
    # Правила рисования, одинаковые для всех:
    #   • нет `cost_key` — денежной строки нет вовсе (Gemini на бесплатном ключе);
    #   • сумма расхода — ССЫЛКОЙ на кабинет провайдера (превью ссылок гасит
    #     `_send_panel_message`);
    #   • есть `balance_key` — после «/» дописывается остаток на счету.
    def _money(key: str) -> float:
        """settings хранит строки — приводим к числу с запасным нулём."""
        try:
            return float(get_setting(key, "0") or 0)
        except (TypeError, ValueError):
            return 0.0

    text = f"🤖 <b>УПРАВЛЕНИЕ МОДЕЛЯМИ</b>\n───────────────────────────\n"
    for pid, meta in PROVIDERS.items():
        text += f"{meta['icon']} <b>{meta['calls_label']}:</b>\n{groups[pid]}"
        if meta["cost_key"]:
            money = (f"💰 <b>{meta['money_label']}:</b> "
                     f"<a href=\"{meta['console_url']}\">${_money(meta['cost_key']):.6f}</a>")
            if meta["balance_key"]:
                money += f" / <b>${_money(meta['balance_key']):.6f}</b>"
                # Приписка «сверено в 14:05» — только у провайдеров, чей
                # остаток бот спрашивает у платформы (14.09.2026). Остальным
                # она не полагается: их счёт вписывается руками.
                note = daily_report.balance_sync_note(pid)
                if note:
                    money += f" <i>({note})</i>"
            text += money + "\n"
        text += "───────────────────────────\n"
    text += (
        f"📡 <b>Общие вызовы API:</b>\n"
        f"  • Всего: <b>{stats['api_calls_total']}</b>\n"
        f"  • Сегодня: <b>{stats['api_calls_today']}</b>"
    )
    if api_toggles_below(user_id):
        # Переключателей под панелью нет — сказать, где они (2026-10-18).
        text += "\n\n⌨️ <i>Мысли, ответ на глазах и склейка — в нижних кнопках.</i>"
    await _send_panel_message(bot, chat_id, text, _build_api_keyboard(user_id))


def _report_back_rows():
    """Возврат с экранов отчётов: сначала в панель API (откуда пришли — там
    живут обе кнопки отчётов), следом обычный выход в /adm."""
    return [[InlineKeyboardButton("⬅️ Настройки API", callback_data="adm_open_api")],
            _adm_back_row()]


async def send_daily_report_panel(bot, chat_id: int, user_id: int):
    """
    Кнопка «📊 Отчёт за вчера»: показывает ТОТ ЖЕ текст, что пришёл ночью
    (он сохранён в settings при отправке), плюс живой хвост «сколько набежало
    с последней полуночи по сейчас».

    ⚠️ Отчёт здесь НЕ пересобирается: месячное обнуление стирает вызовы за
    прошедшие дни, и пересборка показала бы нули. Единственный источник —
    сохранённый текст (services/daily_report.py::last_report_text).
    """
    from services import daily_report

    text = daily_report.last_report_text()
    if not text:
        text = ("📊 <b>ОТЧЁТ ЗА ВЧЕРА</b>\n"
                "───────────────────────────\n"
                "<i>Отчёта пока нет — первый придёт в ближайшую полночь по Киеву.</i>")
    try:
        text += daily_report.today_so_far()
    except Exception as e:
        logger.warning("⚠️ Не удалось посчитать расход с последней полуночи: %s", e)

    await _send_panel_message(bot, chat_id, text, InlineKeyboardMarkup(_report_back_rows()))


async def send_weekly_report_panel(bot, chat_id: int, user_id: int):
    """
    Кнопка «📅 Отчёт за неделю»: показывает текст последнего понедельничного
    отчёта (сохранён в settings при отправке) плюс живой хвост «сколько набежало
    с начала текущей недели по сейчас».

    ⚠️ Как и суточный, отчёт здесь НЕ пересобирается — единственный источник
    сохранённый текст (services/daily_report.py::last_weekly_text).
    """
    from services import daily_report

    text = daily_report.last_weekly_text()
    if not text:
        text = ("📅 <b>ОТЧЁТ ЗА НЕДЕЛЮ</b>\n"
                "───────────────────────────\n"
                "<i>Отчёта пока нет — первый придёт в ближайший понедельник в 00:00 по Киеву.</i>")
    try:
        text += daily_report.week_so_far()
    except Exception as e:
        logger.warning("⚠️ Не удалось посчитать расход с начала недели: %s", e)

    await _send_panel_message(bot, chat_id, text, InlineKeyboardMarkup(_report_back_rows()))




async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает панель статистики (только владелец)."""
    await delete_user_message_safe(update.message)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not await _require(update, context, "owner"):
        return

    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    # logger.info("📊 Админ %s открыл панель /stats", user_id)  # скрыто по просьбе
    await _end_kb_test(context.bot, chat_id, context)  # выход из проверки поиска — с уборкой
    await send_stats_panel(context.bot, chat_id, user_id)


def _web_row():
    """
    Ряд входа в веб-админку (30.08.2026, этап 0). Пусто, если сайт не поднят
    или не настроен адрес — тогда ряда в панели просто нет.

    Две разные двери, и это намеренно:
      • «🌐 Админка» — мини-приложение, открывается ВНУТРИ Telegram. Вход
        происходит молча: Telegram сам передаёт странице, кто её открыл.
        ⚠️ Кнопка web_app работает ТОЛЬКО в личке — панели бота и так живут
        только в личке (_is_group_chat), но помнить об этом надо.
      • «🔗 Ссылка в браузер» — бот присылает ссылку на пять
        минут, чтобы открыть админку в настоящем браузере (на большом экране
        удобнее, а внутри Telegram нет ни адресной строки, ни вкладок).

    ⚠️ У web_app-кнопки НЕТ callback_data — она не проходит ни через роутер,
    ни через проверку прав (_filter_keyboard пропускает такие как есть).
    Поэтому ряд целиком владельческий по второй кнопке, а сам адрес сайта
    ничего не открывает без подписи Telegram (см. web/auth.py).
    """
    from config import WEB_ENABLED, WEB_PUBLIC_URL
    if not (WEB_ENABLED and WEB_PUBLIC_URL.startswith("https://")):
        return []
    from telegram import WebAppInfo
    return [[
        InlineKeyboardButton("🌐 Админка", web_app=WebAppInfo(url=WEB_PUBLIC_URL)),
        InlineKeyboardButton("🔗 Ссылка в браузер", callback_data="web:link"),
    ]]


def _adm_rows():
    """
    Ряды кнопок главной панели ДО фильтра по правам: из них собирают полную
    клавиатуру для preflight (build_adm_keyboard) и /adm модератора (adm_view).
    ⚠️ Владелец этих кнопок с 18.10.2026 в /adm не видит — его разделы в
    нижней панели (panel_keys.py); владельческие ряды здесь остались ради
    сверки проводки кнопок и роутера.
    """
    return _web_row() + [
        [InlineKeyboardButton("💬 Статистика", callback_data="adm_open_stats")],
        [InlineKeyboardButton("⚙️ Управление PROMPTами", callback_data="adm_open_prompts")],
        # Отчёты о расходах (за вчера и за неделю) живут ВНУТРИ панели API —
        # рядом со счётчиками, из которых собраны (решение Максима 2026-07-25).
        [InlineKeyboardButton("📡 Настройки API", callback_data="adm_open_api")],
        [InlineKeyboardButton("🛠Управление DDoS-Guard", callback_data="adm_open_mod")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="adm_open_users")],
        [InlineKeyboardButton("📚 База знаний (RAG)", callback_data="adm_open_rag")],
        # Викторина (2026-08-05): вопросы пишутся по статьям базы знаний,
        # приезжают файлом и ждут одобрения. Стоит СРАЗУ ЗА базой знаний намеренно — это её
        # продолжение: сначала статьи, потом вопросы по ним.
        [InlineKeyboardButton("🎮 Настройки Викторины", callback_data="quiz:panel")],
        # Логи бота — владельческая кнопка (в _CALLBACK_RULES её нет), поэтому
        # у модератора ряд пропадает целиком.
        # 2026-08-10: соседкой в этом ряду стояла «💾 Копия базы» — по просьбе
        # Максима она переехала в панель «💬 СТАТИСТИКА» (send_stats_panel).
        # Ряд остался в один столбец; обратно кнопку не возвращать.
        [InlineKeyboardButton("📜 Логи бота", callback_data="adm_logs")],
        # Очистка памяти и перезапуск — одним рядом (18.10.2026, решение
        # Максима; внизу с 19.10.2026 они разошлись: перезапуск — в подменю
        # «🎛 Админ-панель»). Этот ряд не видит никто — у владельца /adm без
        # кнопок; он здесь ради сверки проводки. Обе владельческие (в
        # _CALLBACK_RULES их нет), у модератора ряда нет.
        #   • «🧹 Очистить ПАМЯТЬ» — та же panel_prompts.wipe_memory, что у
        #     нижней кнопки: память групп и личная переписка ВСЕХ людей, без
        #     подтверждения. До 18.10.2026 на её месте стояли две уборочные
        #     («🧹 Очистить РАЗГОВОРЫ» — adm:wipe, «🗑 Очистить мой диалог» —
        #     clear_history_btn); их слили в одну, ветки adm:wipe больше нет.
        #     clear_history_btn осталась — это публичная «🗑️ Очистить историю»
        #     в /start, она чистит только нажавшего;
        #   • перезапуск. До 18.10.2026 рядом стоял тумблер «⬇️ ОБНОВЛЕНИЕ»;
        #     самообновление теперь включено всегда (jobs/update.py), тумблер
        #     убран отовсюду — не возвращать.
        [
            InlineKeyboardButton("🧹 Очистить ПАМЯТЬ", callback_data="adm:memory"),
            InlineKeyboardButton("🔄 ПЕРЕЗАПУСК", callback_data="system_restart"),
        ],
        # До 18.10.2026 последним рядом стоял выключатель «⌨️ НИЖНИЕ КНОПКИ»;
        # по решению Максима нижние кнопки теперь есть всегда — не возвращать.
    ]


def build_adm_keyboard(user_id: int):
    """
    ПОЛНАЯ клавиатура /adm по правам смотрящего. Её меряет preflight (самый
    полный вариант панели) и сторожат проверки поведения. Что /adm показывает
    на самом деле — решает adm_view: владельцу — ни одной кнопки (разделы
    внизу), модератору — его часть этой клавиатуры.
    """
    return InlineKeyboardMarkup(_filter_keyboard(_adm_rows(), user_id))


def adm_view(user_id: int):
    """
    Что показывает /adm этому человеку: (текст, клавиатура или None).

    ⚠️ У ВЛАДЕЛЬЦА — БЕЗ КНОПОК ПОД СООБЩЕНИЕМ (2026-10-18, решения Максима):
    все разделы стоят внизу под полем ввода (panel_keys.py), выключателя
    нижних кнопок нет. Текст — заголовок с версией, ОДИН с сообщением нижних
    кнопок (panel_keys.CARRIER_TEXT): в личке /adm владельца и есть это
    сообщение (send_adm_panel). Модераторов это не касается: нижней панели у
    них нет, /adm у них — по правам.

    Версия в заголовке — способ увидеть, какая сборка РЕАЛЬНО работает (на
    сервере она меняется только после того, как он забрал правки). Синей
    ссылкой на код этой версии — обычный текст в Телеграме покрасить нельзя,
    синим рисуются только ссылки.
    """
    from services import roles
    if roles.is_owner(user_id):
        from .panel_keys import carrier_text
        return carrier_text(), None

    keyboard = _filter_keyboard(_adm_rows(), user_id)
    if keyboard:
        text = (f"🛡 <b>ПАНЕЛЬ МОДЕРАТОРА</b> · {BOT_VERSION_HTML}\n"
                "───────────────────────────\n"
                "<i>Доступны разделы по твоим правам:</i>")
    else:
        # Модератор, у которого сняли все галочки: не молчим, чтобы человек
        # понимал, что дело в правах, а не в поломке бота.
        text = (f"🛡 <b>ПАНЕЛЬ МОДЕРАТОРА</b> · {BOT_VERSION_HTML}\n"
                "───────────────────────────\n"
                "<i>Прав пока не выдано — обратись к владельцу бота.</i>")
    return text, (InlineKeyboardMarkup(keyboard) if keyboard else None)


async def send_adm_panel(bot, chat_id: int, user_id: int | None = None):
    """
    Главная панель — единая точка входа во все разделы управления ботом.

    Собирается ПО ПРАВАМ смотрящего: список разделов один на всех, а лишнее
    отсекает фильтр по таблице прав (services/roles.py). Владельческие разделы
    в таблице не значатся вовсе — им достаётся запрет по умолчанию. Вид — по
    adm_view.
    user_id по умолчанию = chat_id: панель всегда открывается в личке, где
    это одно и то же число.

    ⚠️ У ВЛАДЕЛЬЦА В ЛИЧКЕ /adm — ЭТО СООБЩЕНИЕ НИЖНИХ КНОПОК (18.10.2026,
    решение Максима: два одинаковых сообщения — одно). Оно приходит заново, а
    прежнее удаляется (panel_keys.send_owner_keys). В гигиену панелей его НЕ
    кладём — открытие любого раздела стёрло бы его вместе с нижними кнопками;
    открытый раздел (и «✅ Бот запущен» при старте) убираем той же уборкой,
    что и раньше, но без регистрации нового.
    """
    uid = chat_id if user_id is None else user_id
    from services import roles
    if chat_id == uid and roles.is_owner(uid):
        from utils import clean_bot_messages
        from .panel_keys import send_owner_keys
        await send_owner_keys(bot, chat_id, uid)
        await clean_bot_messages(bot, chat_id)
        return
    text, markup = adm_view(uid)
    sent_msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        # Без этого под панелью развернётся карточка GitHub со ссылки версии.
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=markup,
    )
    if sent_msg:
        await register_and_clean_bot_message(bot, chat_id, sent_msg.message_id)


async def cmd_adm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главная панель. Только в личке; открывается любому из персонала —
    владельцу целиком, модератору по его правам."""
    await delete_user_message_safe(update.message)
    user_id = update.effective_user.id

    if not await _require(update, context, "any"):
        return

    # Панель — только в личке. В группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    await _end_kb_test(context.bot, update.effective_chat.id, context)  # выход из проверки поиска — с уборкой
    await send_adm_panel(context.bot, update.effective_chat.id, user_id)
    # ⚠️ Нижних кнопок владельца /adm НЕ присылает (v5.72, 18.10.2026): их
    # сообщение остаётся в чате и прыгало бы под панель на каждое /adm.
    # Пропали — вернёт /start (см. handlers/admin/panel_keys.py).
