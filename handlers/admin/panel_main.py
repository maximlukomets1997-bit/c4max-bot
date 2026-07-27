# ───────────────────────────────────────────────
#  handlers/admin/panel_main.py — главная панель /adm, панель статистики /stats и панель моделей «📡 Настройки API» (раскладка кнопок моделей).
#  Выделен из монолитного admin.py 2026-07-13 разрезом БЕЗ изменения логики.
# ───────────────────────────────────────────────
import html
import logging
import os

import logging_setup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, ADMIN_IDS, GEMINI_MODEL, PROVIDER_ICONS
from database.history import set_setting, get_setting, append_prompt_addition, get_active_system_prompt, get_bot_stats, get_news_system_prompt, get_rag_instruction, get_qwen_tokens
from utils import register_and_clean_bot_message, delete_user_message_safe
from utils import mention, schedule_delete


logger = logging.getLogger(__name__)
from .common import (_adm_back_row, _filter_keyboard, _is_group_chat, _reject_non_admin,
                     _require, _send_panel_message)
from .panel_rag import _end_kb_test




# Раскладка кнопок текстовых моделей: 4 ряда по 2 колонки. Собрана ВРУЧНУЮ
# по конкретным ключам AVAILABLE_MODELS: добавил модель в config.py — добавь
# её ключ сюда, иначе кнопка НЕ появится.
# 2026-07-19: было 6 рядов; модели qwen3.6-max-preview и qwen3.6-plus удалены
# по решению владельца (не использовались) — ряды Qwen сжаты в один.
# 2026-07-24: было 5 рядов; удалены gemini-2.5-flash-lite, gemini-2.5-flash
# и gemma-4-26b-a4b-it, добавлена gemini-3.6-flash. Порядок Gemini задан
# Максимом «по столбцам»: слева 3 Flash (v3) и 3.1 Flash-Lite, справа
# 3.5 Flash и 3.6 Flash — в рядах это даёт пары ниже.
# 2026-07-25: добавлен ряд Xiaomi MiMo (mimo-v2.5 / mimo-v2.5-pro) — стало 5 рядов.
_MODEL_BUTTON_ROWS = [
    ["gemini-3-flash-preview", "gemini-3.5-flash"],
    ["gemini-3.1-flash-lite", "gemini-3.6-flash"],
    ["qwen3.7-plus", "qwen3.7-max"],
    ["deepseek-v4-flash", "deepseek-v4-pro"],
    ["mimo-v2.5", "mimo-v2.5-pro"],
]

# Ряд моделей картинок (Nano Banana, gemini-*-image).
_IMAGE_BUTTON_ROW = ["gemini-3.1-flash-lite-image", "gemini-3.1-flash-image"]


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


def _build_api_keyboard(user_id):
    """
    Клавиатура панели «📡 Настройки API»: кнопки выбора моделей + отчёты
    о расходах + возврат в /adm.
    Вызывается при открытии панели и при обновлении клавиатуры после смены модели.
    """
    active_model = get_setting("active_model", GEMINI_MODEL)
    active_image_model = get_setting("active_image_model", "gemini-3.1-flash-image")
    _, model_markup = _build_model_panel_text_and_keyboard(active_model, active_image_model, user_id)
    # inline_keyboard у собранной клавиатуры — кортеж (tuple), складывать его
    # со списком нельзя — приводим всё к спискам.
    rows = list(model_markup.inline_keyboard) + [list(_REPORT_BUTTON_ROW), _adm_back_row()]
    return InlineKeyboardMarkup(rows)


async def send_stats_panel(bot, chat_id: int, user_id: int):
    """Панель «💬 СТАТИСТИКА»: три сводные цифры (кнопка в /adm и команда /stats)."""
    stats = get_bot_stats()
    text = (
        f"💬 <b>СТАТИСТИКА</b>\n"
        f"───────────────────────────\n"
        # «за этот месяц» — user_token_usage обнуляется месячным сбросом
        # (jobs.py::_monthly_stats_reset); уберёшь сброс — верни «за всё время».
        f"💬 <b>Обменов «вопрос-ответ» за этот месяц: {stats['lifetime_requests']}</b>\n"
        f"👥 <b>Подписчиков на новости: {stats['subscriptions']}</b>\n"
        # «10 дней» = срок автоочистки архива в jobs.py::cleanup_loop;
        # меняешь срок там — поменяй подпись здесь.
        f"📝 <b>Архив группы за последние 10 дней: {stats['group_msg_count']}</b>"
    )
    await _send_panel_message(bot, chat_id, text, InlineKeyboardMarkup([_adm_back_row()]))


async def send_api_panel(bot, chat_id: int, user_id: int):
    """Панель «🤖 УПРАВЛЕНИЕ МОДЕЛЯМИ»: вызовы API по провайдерам + кнопки моделей."""
    stats = get_bot_stats()

    # Раскладываем счётчики вызовов по провайдерам (поле "provider" в config.py).
    # Модели картинок — в ОТДЕЛЬНУЮ группу "image" (не смешивать с бесплатным
    # текстовым Gemini); модели, которых уже нет в конфиге, попадают только в
    # общий счётчик «Общие вызовы API».
    groups = {"gemini": "", "qwen": "", "deepseek": "", "xiaomi": "", "image": ""}
    for model_name, cnt in stats["api_calls_by_model"]:
        if model_name in AVAILABLE_IMAGE_MODELS:
            provider = "image"
        else:
            provider = AVAILABLE_MODELS.get(model_name, {}).get("provider")
        if provider in groups and provider != "qwen":
            groups[provider] += f"  • <code>{model_name}</code>: <b>{cnt}</b>\n"

    # Блок Qwen собирается ОТДЕЛЬНО от остальных провайдеров: вызовы месячные
    # (обнуляются 1-го числа вместе с api_calls), а ОСТАТОК бесплатной квоты
    # Alibaba — вечный (ключи qwen_tokens_<модель>, тают в spend_qwen_tokens).
    # Поэтому строку модели рисуем ВСЕГДА, даже если вызовов в этом месяце ещё
    # не было: иначе остаток квоты пропадал бы из панели сразу после месячного
    # сброса. Модели, удалённые из конфига, но с заведённой квотой, дописываются
    # следом — счёт квоты не должен теряться вместе с моделью.
    # Ключа в settings нет — пишем «квота не задана», а не 0: ноль означает
    # «квота кончилась», это разные вещи.
    qw_tokens = get_qwen_tokens()
    qw_calls = {m: c for m, c in stats["api_calls_by_model"]}
    for model_name, meta in AVAILABLE_MODELS.items():
        if meta.get("provider") != "qwen":
            continue
        _left = qw_tokens.pop(model_name, None)
        if _left is None:
            _left_str = "квота не задана"
        else:
            _left_str = "осталось <b>" + f"{_left:,}".replace(",", " ") + "</b>"
        groups["qwen"] += (f"  • <code>{model_name}</code>: "
                           f"<b>{qw_calls.get(model_name, 0)}</b> · {_left_str}\n")
    for model_name, tokens in qw_tokens.items():
        _tok = f"{tokens:,}".replace(",", " ")
        groups["qwen"] += (f"  • <code>{html.escape(model_name)}</code> (нет в конфиге): "
                           f"осталось <b>{_tok}</b>\n")

    for provider in groups:
        if not groups[provider]:
            groups[provider] = "  • нет данных\n"

    # Накопленный расход на DeepSeek: копится в services/gemini.py по точным
    # токенам из API и ценам DEEPSEEK_PRICES. settings хранит строки — приводим
    # к числу с запасным нулём.
    try:
        ds_spent = float(get_setting("deepseek_cost_usd", "0") or 0)
    except (TypeError, ValueError):
        ds_spent = 0.0

    # Остаток баланса аккаунта DeepSeek: заводится вручную в settings
    # (deepseek_balance_usd), дальше тает сам — add_deepseek_cost вычитает
    # стоимость каждого запроса. Пополнил счёт — поправь значение в settings.
    try:
        ds_balance = float(get_setting("deepseek_balance_usd", "0") or 0)
    except (TypeError, ValueError):
        ds_balance = 0.0

    # Накопленный расход на Qwen — считается так же (services/gemini.py::_qwen_cost,
    # цены QWEN_PRICES). Пока действует бесплатная квота Alibaba, это «расчётная»
    # стоимость по прайсу — реальные списания начнутся после исчерпания квоты.
    try:
        qw_spent = float(get_setting("qwen_cost_usd", "0") or 0)
    except (TypeError, ValueError):
        qw_spent = 0.0

    # Накопленный расход на Xiaomi MiMo (services/gemini.py::_xiaomi_cost, цены
    # XIAOMI_PRICES) и остаток счёта: остаток заводится вручную в settings
    # (xiaomi_balance_usd), дальше тает сам — add_xiaomi_cost вычитает стоимость
    # каждого запроса. Пополнил счёт Xiaomi — поправь значение в settings.
    try:
        xm_spent = float(get_setting("xiaomi_cost_usd", "0") or 0)
    except (TypeError, ValueError):
        xm_spent = 0.0
    try:
        xm_balance = float(get_setting("xiaomi_balance_usd", "0") or 0)
    except (TypeError, ValueError):
        xm_balance = 0.0

    # Накопленный расход на генерацию картинок (Nano Banana): считается по точным
    # токенам из usageMetadata и ценам IMAGE_PRICES (services/gemini.py::_image_cost).
    try:
        img_spent = float(get_setting("image_cost_usd", "0") or 0)
    except (TypeError, ValueError):
        img_spent = 0.0

    # Остаток баланса картинок: заводится вручную в settings (image_balance_usd),
    # дальше тает сам — add_image_cost вычитает стоимость каждой картинки.
    # Пополнил счёт Google — поправь значение в settings.
    try:
        img_balance = float(get_setting("image_balance_usd", "0") or 0)
    except (TypeError, ValueError):
        img_balance = 0.0

    # Значки провайдеров берём из общего списка PROVIDER_ICONS (config.py) —
    # того же, что метит строки лога в services/gemini.py. Так панель и лог
    # не разъезжаются: сменил значок в конфиге — поменялось в обоих местах.
    text = (
        f"🤖 <b>УПРАВЛЕНИЕ МОДЕЛЯМИ</b>\n"
        f"───────────────────────────\n"
        f"{PROVIDER_ICONS['gemini']} <b>Вызовы Gemini:</b>\n{groups['gemini']}"
        f"───────────────────────────\n"
        f"{PROVIDER_ICONS['image']} <b>Генерация Картинок:</b>\n{groups['image']}"
        f"💰 <b>Расход Картинок:</b> <a href=\"https://aistudio.google.com/spend\">${img_spent:.6f}</a> / <b>${img_balance:.6f}</b>\n"
        f"───────────────────────────\n"
        f"{PROVIDER_ICONS['qwen']} <b>Вызовы Qwen:</b>\n{groups['qwen']}"
        f"💰 <b>Расход Qwen:</b> <a href=\"https://modelstudio.console.alibabacloud.com/\">${qw_spent:.6f}</a>\n"
        f"───────────────────────────\n"
        f"{PROVIDER_ICONS['deepseek']} <b>Вызовы DeepSeek:</b>\n{groups['deepseek']}"
        # Сумма — ссылкой на страницу расходов DeepSeek (превью отключено в
        # _send_panel_message); после «/» — остаток баланса аккаунта.
        f"💰 <b>Расход DeepSeek:</b> <a href=\"https://platform.deepseek.com/usage\">${ds_spent:.6f}</a> / <b>${ds_balance:.6f}</b>\n"
        f"───────────────────────────\n"
        f"{PROVIDER_ICONS['xiaomi']} <b>Вызовы Xiaomi:</b>\n{groups['xiaomi']}"
        # Как у DeepSeek: расход ссылкой на кабинет, после «/» — остаток счёта.
        f"💰 <b>Расход Xiaomi:</b> <a href=\"https://platform.xiaomimimo.com/\">${xm_spent:.6f}</a> / <b>${xm_balance:.6f}</b>\n"
        f"───────────────────────────\n"
        f"📡 <b>Общие вызовы API:</b>\n"
        f"  • Всего: <b>{stats['api_calls_total']}</b>\n"
        f"  • Сегодня: <b>{stats['api_calls_today']}</b>"
    )
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


def _adm_rows():
    """
    Ряды кнопок главной панели ДО фильтра по правам. Вынесены отдельно, чтобы
    ту же раскладку могла собрать ветка, которая ВОЗВРАЩАЕТ клавиатуру /adm
    после действия (очистка разговоров — router.py), не отправляя панель заново.
    """
    return [
        [InlineKeyboardButton("💬 Статистика", callback_data="adm_open_stats")],
        [InlineKeyboardButton("⚙️ Управление PROMPTами", callback_data="adm_open_prompts")],
        # Отчёты о расходах (за вчера и за неделю) живут ВНУТРИ панели API —
        # рядом со счётчиками, из которых собраны (решение Максима 2026-07-25).
        [InlineKeyboardButton("📡 Настройки API", callback_data="adm_open_api")],
        [InlineKeyboardButton("🛠Управление DDoS-Guard", callback_data="adm_open_mod")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="adm_open_users")],
        [InlineKeyboardButton("📚 База знаний (RAG)", callback_data="adm_open_rag")],
        [InlineKeyboardButton("📜 Логи бота", callback_data="adm_logs")],
        # ⚠️ ВРЕМЕННЫЙ РЯД (2026-07-26, просьба Максима на время тестов новой
        # карточки и справки об авторе): дубликаты двух уборочных кнопок под
        # рукой, чтобы между проверками не ходить по панелям.
        #   • «Очистить РАЗГОВОРЫ» — тот же wipe, что в панели промптов, но со
        #     СВОИМ callback: после подтверждения нужно вернуть клавиатуру /adm,
        #     а не панели промптов (иначе в админ-панели окажутся чужие кнопки);
        #   • «Очистить мой диалог» — существующая ветка clear_history_btn
        #     (та же, что кнопка в /rank): чистит контекст НАЖАВШЕГО, панель
        #     не трогает, подтверждения не спрашивает — как команда /clear.
        # Убрать оба, когда тесты закончатся.
        [
            InlineKeyboardButton("🧹 Очистить РАЗГОВОРЫ", callback_data="adm:wipe"),
            InlineKeyboardButton("🗑 Очистить мой диалог", callback_data="clear_history_btn"),
        ],
        [InlineKeyboardButton("🔄 ПЕРЕЗАПУСТИТЬ БОТА", callback_data="system_restart")],
    ]


def build_adm_keyboard(user_id: int):
    """Клавиатура /adm по правам смотрящего — для веток, которые возвращают
    панель на место после действия."""
    return InlineKeyboardMarkup(_filter_keyboard(_adm_rows(), user_id))


async def send_adm_panel(bot, chat_id: int, user_id: int | None = None):
    """
    Главная панель — единая точка входа во все разделы управления ботом.

    Собирается ПО ПРАВАМ смотрящего: список разделов один на всех, а лишнее
    отсекает фильтр по таблице прав (services/roles.py). Владельческие разделы
    в таблице не значатся вовсе — им достаётся запрет по умолчанию.
    user_id по умолчанию = chat_id: панель всегда открывается в личке, где
    это одно и то же число.
    """
    from services import roles
    uid = chat_id if user_id is None else user_id
    owner = roles.is_owner(uid)

    keyboard = _filter_keyboard(_adm_rows(), uid)

    if owner:
        text = ("🎛 <b>АДМИН-ПАНЕЛЬ</b>\n"
                "───────────────────────────\n"
                "<i>Выберите раздел управления:</i>")
    elif keyboard:
        text = ("🛡 <b>ПАНЕЛЬ МОДЕРАТОРА</b>\n"
                "───────────────────────────\n"
                "<i>Доступны разделы по твоим правам:</i>")
    else:
        # Модератор, у которого сняли все галочки: не молчим, чтобы человек
        # понимал, что дело в правах, а не в поломке бота.
        text = ("🛡 <b>ПАНЕЛЬ МОДЕРАТОРА</b>\n"
                "───────────────────────────\n"
                "<i>Прав пока не выдано — обратись к владельцу бота.</i>")

    sent_msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
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
