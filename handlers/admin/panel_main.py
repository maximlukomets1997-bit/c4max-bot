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
from config import AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, ADMIN_IDS, GEMINI_MODEL, PROVIDER_ICONS, BOT_VERSION_HTML, AUTO_UPDATE_ENABLED_DEFAULT
from database.history import set_setting, get_setting, delete_setting, append_prompt_addition, get_active_system_prompt, get_bot_stats, get_news_system_prompt, get_rag_instruction, get_qwen_tokens
from utils import register_and_clean_bot_message, delete_user_message_safe
from utils import mention, schedule_delete


logger = logging.getLogger(__name__)
from .common import (_adm_back_row, _audit, _filter_keyboard, _is_group_chat, _onoff, _reject_non_admin,
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
    о расходах + вход на экран «💰 Счета и квоты» + возврат в /adm.
    Вызывается при открытии панели и при обновлении клавиатуры после смены модели.
    """
    active_model = get_setting("active_model", GEMINI_MODEL)
    active_image_model = get_setting("active_image_model", "gemini-3.1-flash-image")
    _, model_markup = _build_model_panel_text_and_keyboard(active_model, active_image_model, user_id)
    # inline_keyboard у собранной клавиатуры — кортеж (tuple), складывать его
    # со списком нельзя — приводим всё к спискам.
    rows = list(model_markup.inline_keyboard) + [
        list(_REPORT_BUTTON_ROW),
        [InlineKeyboardButton("💰 СЧЕТА И КВОТЫ", callback_data="bal:panel")],
        _adm_back_row(),
    ]
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

    # Остаток баланса аккаунта DeepSeek: заводится кнопкой «💵 Счёт DeepSeek»
    # на экране «💰 Счета и квоты» (ниже в этом файле), дальше тает сам —
    # add_deepseek_cost вычитает стоимость каждого запроса.
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
    # XIAOMI_PRICES) и остаток счёта: остаток заводится кнопкой «💵 Счёт Xiaomi»
    # на экране «💰 Счета и квоты», дальше тает сам — add_xiaomi_cost вычитает
    # стоимость каждого запроса.
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

    # Остаток баланса картинок: заводится кнопкой «💵 Счёт Картинок» на экране
    # «💰 Счета и квоты», дальше тает сам — add_image_cost вычитает стоимость
    # каждой картинки.
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


# ─────────────────────────────────────────────
#  Экран «💰 Счета и квоты» (2026-07-27, просьба Максима)
#
#  До него остатки на счетах и остаток бесплатной квоты Qwen правились только
#  руками в базе. Здесь они правятся кнопками: нажал — бот просит прислать
#  число — прислал, и значение на месте (перезапуск не нужен, все счётчики
#  читаются из settings на каждый показ панели).
#
#  Три правила, которые нельзя «чинить»:
#   • «не задано» ≠ «ноль». Пустой остаток УДАЛЯЕТСЯ (delete_setting), а не
#     обнуляется: пока ключа нет, вычитающий UPDATE в add_*_cost его не находит
#     и ничего не портит; с нулём остаток ушёл бы в минус с первого запроса.
#   • Кнопки квот Qwen собираются ИЗ AVAILABLE_MODELS сами (в отличие от
#     _MODEL_BUTTON_ROWS) — новая модель Qwen получит кнопку без правки кода.
#     Квота модели, удалённой из конфига, показывается в тексте отдельной
#     строкой, чтобы её остаток не потерялся вместе с моделью.
#   • Ожидание числа живёт в user_data["balance_edit"] и гаснет от ЛЮБОЙ другой
#     кнопки (одна проверка в router.py) и от ЛЮБОЙ команды (одна строка
#     в log_incoming_command) — иначе следующий вопрос боту в личке был бы
#     съеден как «не число».
# ─────────────────────────────────────────────

# Настраиваемые ОСТАТКИ на счетах. Ключи те же, что читает панель API и
# из которых вычитают add_deepseek_cost / add_xiaomi_cost / add_image_cost.
# Денежного остатка у Qwen намеренно нет (решение Максима 2026-07-27): там
# бесплатная квота в токенах, а не счёт в долларах.
_BALANCE_FIELDS = {
    "deepseek": {"key": "deepseek_balance_usd", "provider": "deepseek",
                 "name": "DeepSeek", "btn": "💵 Счёт DeepSeek"},
    "xiaomi":   {"key": "xiaomi_balance_usd", "provider": "xiaomi",
                 "name": "Xiaomi", "btn": "💵 Счёт Xiaomi"},
    "image":    {"key": "image_balance_usd", "provider": "image",
                 "name": "Картинки", "btn": "💵 Счёт Картинок"},
}

# Счётчики «потрачено» — их можно обнулить (при смене ключа или рабочего
# пространства). Все четыре ВЕЧНЫЕ: месячный сброс их не трогает, поэтому
# единственный способ начать счёт заново — эта кнопка.
_COST_FIELDS = {
    "deepseek": {"key": "deepseek_cost_usd", "provider": "deepseek", "name": "DeepSeek"},
    "qwen":     {"key": "qwen_cost_usd",     "provider": "qwen",     "name": "Qwen"},
    "xiaomi":   {"key": "xiaomi_cost_usd",   "provider": "xiaomi",   "name": "Xiaomi"},
    "image":    {"key": "image_cost_usd",    "provider": "image",    "name": "Картинки"},
}

_MAX_MONEY = 1_000_000.0          # больше — почти наверняка опечатка
_MAX_TOKENS = 100_000_000_000     # то же для квоты токенов


def _icon(provider: str) -> str:
    """Значок провайдера из общего списка config.PROVIDER_ICONS (тот же, что
    в логах и в панели API — чтобы экраны не разъезжались)."""
    return PROVIDER_ICONS.get(provider, "🤖")


def _qwen_model_keys() -> list:
    """Модели Qwen из конфига — по ним собираются кнопки квот."""
    return [m for m, meta in AVAILABLE_MODELS.items() if meta.get("provider") == "qwen"]


def _money_str(value: float) -> str:
    """Деньги — тем же видом, что в панели API (6 знаков)."""
    return f"${value:.6f}"


def _tokens_str(value: int) -> str:
    """Токены с пробелами по три разряда: 1 000 000."""
    return f"{int(value):,}".replace(",", " ")


def _read_number(key: str, kind: str):
    """
    Значение настройки как число: (число, задано ли).
    «Задано ли» важнее самого числа: остаток $0.000000 и «остаток не задан» —
    разные вещи, и на экране они пишутся по-разному.
    """
    raw = get_setting(key, "")
    if raw is None or str(raw).strip() == "":
        return (0 if kind == "tokens" else 0.0), False
    try:
        return (int(float(raw)) if kind == "tokens" else float(raw)), True
    except (TypeError, ValueError):
        return (0 if kind == "tokens" else 0.0), False


def _value_str(key: str, kind: str, absent: str) -> str:
    """Готовая строка значения для экрана: число или подпись «не задано»."""
    value, is_set = _read_number(key, kind)
    if not is_set:
        return f"<i>{absent}</i>"
    return f"<b>{_tokens_str(value) if kind == 'tokens' else _money_str(value)}</b>"


def _balance_field(field_id: str):
    """
    Описание настраиваемого значения по его коду из кнопки.
    Коды: deepseek | xiaomi | image (остатки на счетах) и qwen:<модель>
    (остаток квоты токенов). Неизвестный код → None (кнопка из старого,
    ещё не затёртого сообщения).
    """
    if field_id in _BALANCE_FIELDS:
        cfg = _BALANCE_FIELDS[field_id]
        return {
            "key": cfg["key"],
            "kind": "money",
            "title": f"{_icon(cfg['provider'])} ОСТАТОК НА СЧЕТУ: {cfg['name'].upper()}",
            "short": f"Остаток {cfg['name']}",
            "example": "5.32",
            "clear": "убрать значение совсем",
            "absent": "не задан",
        }
    if field_id.startswith("qwen:"):
        model = field_id[len("qwen:"):]
        # Модель может быть уже удалена из конфига, но с заведённой квотой —
        # такую тоже разрешаем править, иначе её остаток не поправить ничем.
        if model and (model in AVAILABLE_MODELS or model in get_qwen_tokens()):
            return {
                "key": f"qwen_tokens_{model}",
                "kind": "tokens",
                "title": f"{_icon('qwen')} КВОТА ТОКЕНОВ: {model}",
                "short": f"Квота {model}",
                "example": "1000000",
                "clear": "убрать квоту («не задана»)",
                "absent": "квота не задана",
            }
    return None


def _parse_number(raw: str, kind: str):
    """
    Число из того, что прислал человек: (значение, текст ошибки).
    Понимает «5,32», «$ 5.32», «1 000 000». Отрицательное и заведомо
    нереальное отбивает с пояснением, а не молча — человек должен понимать,
    почему бот не принял.
    """
    s = (raw or "").replace("$", "").replace(" ", "").replace(" ", "").replace(",", ".").strip()
    if not s:
        return None, "пустое сообщение"
    try:
        value = float(s)
    except ValueError:
        return None, f"«{html.escape(raw[:30])}» — это не число"
    if value < 0:
        return None, "отрицательное число — так не бывает"
    if kind == "tokens":
        if value > _MAX_TOKENS:
            return None, "слишком много токенов — проверь, не лишний ли разряд"
        return int(value), ""
    if value > _MAX_MONEY:
        return None, "слишком большая сумма — проверь, не лишний ли разряд"
    return round(value, 6), ""


def _build_balance_panel(flash: str = ""):
    """
    Текст и кнопки экрана «💰 Счета и квоты». flash — строка-итог последнего
    действия («было → стало»), показывается сверху один раз.
    """
    sep = "───────────────────────────\n"
    parts = [f"💰 <b>СЧЕТА И КВОТЫ</b>\n{sep}"]
    if flash:
        parts.append(f"{flash}\n{sep}")

    # Три денежных блока — в том же порядке, что кнопки ниже.
    for field_id in ("deepseek", "xiaomi", "image"):
        cfg = _BALANCE_FIELDS[field_id]
        spent, _ = _read_number(_COST_FIELDS[field_id]["key"], "money")
        parts.append(
            f"{_icon(cfg['provider'])} <b>{cfg['name']}</b>\n"
            f"  • Потрачено: <b>{_money_str(spent)}</b>\n"
            f"  • Остаток на счету: {_value_str(cfg['key'], 'money', 'не задан')}\n"
            f"{sep}"
        )

    # Qwen: денег на счету нет, зато есть остаток бесплатной квоты по каждой
    # модели. Строки берём из конфига, а не из вызовов, — иначе после месячного
    # сброса модель без вызовов пропала бы вместе с остатком квоты.
    qw_spent, _ = _read_number("qwen_cost_usd", "money")
    block = (f"{_icon('qwen')} <b>Qwen</b> — бесплатная квота в токенах\n"
             f"  • Потрачено: <b>{_money_str(qw_spent)}</b>\n")
    known = get_qwen_tokens()
    for model in _qwen_model_keys():
        known.pop(model, None)
        block += f"  • {model}: осталось {_value_str(f'qwen_tokens_{model}', 'tokens', 'квота не задана')}\n"
    for model, tokens in known.items():
        block += (f"  • {html.escape(model)} (нет в конфиге): "
                  f"осталось <b>{_tokens_str(tokens)}</b>\n")
    parts.append(block + sep)

    parts.append("Нажми кнопку — бот попросит прислать число.\n"
                 "Правка сразу видна в панели API и в отчётах о расходах.")

    rows = [
        [InlineKeyboardButton(_BALANCE_FIELDS["deepseek"]["btn"], callback_data="bal:set:deepseek"),
         InlineKeyboardButton(_BALANCE_FIELDS["xiaomi"]["btn"], callback_data="bal:set:xiaomi")],
        [InlineKeyboardButton(_BALANCE_FIELDS["image"]["btn"], callback_data="bal:set:image")],
    ]
    # Кнопки квот — по две в ряд, собираются из конфига сами.
    qw_buttons = [InlineKeyboardButton(f"🎫 Квота {m}", callback_data=f"bal:set:qwen:{m}")
                  for m in _qwen_model_keys()]
    rows += [qw_buttons[i:i + 2] for i in range(0, len(qw_buttons), 2)]
    rows.append([InlineKeyboardButton("♻️ Обнулить «потрачено»", callback_data="bal:zero")])
    # Обе кнопки возврата — ОДНИМ рядом (решение Максима 2026-07-27).
    rows.append([InlineKeyboardButton("⬅️ Настройки API", callback_data="adm_open_api")]
                + _adm_back_row())
    return "".join(parts), InlineKeyboardMarkup(rows)


def _build_zero_panel(flash: str = ""):
    """Экран «♻️ Обнулить «потрачено»»: текущие суммы и кнопка на каждый счётчик."""
    sep = "───────────────────────────\n"
    text = f"♻️ <b>ОБНУЛИТЬ СЧЁТЧИК «ПОТРАЧЕНО»</b>\n{sep}"
    if flash:
        text += f"{flash}\n{sep}"
    text += ("Эти счётчики вечные — их обнуляют, когда меняется ключ или рабочее "
             "пространство. На реальные деньги в кабинете провайдера обнуление "
             "НЕ влияет, остаток на счету оно тоже не трогает.\n\n")
    for cfg in _COST_FIELDS.values():
        spent, _ = _read_number(cfg["key"], "money")
        text += f"  • {_icon(cfg['provider'])} {cfg['name']}: <b>{_money_str(spent)}</b>\n"

    buttons = [InlineKeyboardButton(f"{_icon(cfg['provider'])} {cfg['name']}",
                                    callback_data=f"bal:zeroask:{code}")
               for code, cfg in _COST_FIELDS.items()]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⬅️ Назад к счетам", callback_data="bal:panel")])
    return text, InlineKeyboardMarkup(rows)


async def send_balance_panel(bot, chat_id: int, user_id: int, flash: str = ""):
    """Присылает экран «💰 Счета и квоты» новым сообщением (через общую гигиену
    панелей — предыдущая панель удаляется). Используется после ввода числа:
    ответ пришёл сообщением, править «то же самое» сообщение уже нечем."""
    text, markup = _build_balance_panel(flash)
    await _send_panel_message(bot, chat_id, text, markup)


async def _show_screen(context, query, text: str, markup: InlineKeyboardMarkup):
    """Показывает экран НА МЕСТЕ — правкой того же сообщения (панель остаётся
    одна, гигиена не трогается). Не получилось (сообщение старое, текст
    совпал) — присылаем панель заново."""
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as e:
        logger.debug("💰 Не удалось обновить экран счетов на месте: %s", e)
        await _send_panel_message(context.bot, query.message.chat_id, text, markup)


async def _handle_balance_callback(query, context, data: str, chat_id: int, user_id: int):
    """
    Кнопки экрана «💰 Счета и квоты» (префикс bal:). Ветки:
      bal:panel                    — сам экран
      bal:set:<код>                — начать ввод числа (ждём сообщение)
      bal:cancel                   — отменить ожидание числа
      bal:zero                     — экран обнуления «потрачено»
      bal:zeroask:<провайдер>      — подтверждение обнуления
      bal:zerogo:<провайдер>       — выполнить обнуление
    В _CALLBACK_RULES этих кнопок нет намеренно: запрет по умолчанию оставляет
    их владельцу, как и всю панель API.
    """
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    # Любая ветка, кроме начала ввода, снимает ожидание числа.
    if action != "set":
        context.user_data.pop("balance_edit", None)

    if action in ("panel", "cancel"):
        await query.answer("Отменено." if action == "cancel" else None)
        text, markup = _build_balance_panel()
        await _show_screen(context, query, text, markup)
        return

    if action == "set":
        field_id = ":".join(parts[2:])
        info = _balance_field(field_id)
        if not info:
            await query.answer("⚠️ Такого счётчика больше нет — открой экран заново.",
                               show_alert=True)
            return
        context.user_data["balance_edit"] = field_id
        await query.answer()
        # Значок в заголовке уже стоит внутри title (провайдерский, из
        # PROVIDER_ICONS) — второго не добавляем.
        now = _value_str(info["key"], info["kind"], info["absent"])
        if info["kind"] == "tokens":
            now += " токенов"
        text = (
            f"<b>{html.escape(info['title'])}</b>\n"
            "───────────────────────────\n"
            f"Сейчас: {now}\n\n"
            "Пришли новое число одним сообщением.\n"
            f"Например: <code>{info['example']}</code>\n"
            f"Прочерк <code>-</code> — {info['clear']}."
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="bal:cancel")]])
        await _show_screen(context, query, text, markup)
        return

    if action == "zero":
        await query.answer()
        text, markup = _build_zero_panel()
        await _show_screen(context, query, text, markup)
        return

    if action == "zeroask":
        code = parts[2] if len(parts) > 2 else ""
        cfg = _COST_FIELDS.get(code)
        if not cfg:
            await query.answer("⚠️ Неизвестный счётчик.", show_alert=True)
            return
        await query.answer()
        spent, _ = _read_number(cfg["key"], "money")
        text = (
            f"❗️ <b>ОБНУЛИТЬ «ПОТРАЧЕНО» У {cfg['name'].upper()}?</b>\n"
            "───────────────────────────\n"
            f"Сейчас: <b>{_money_str(spent)}</b> → станет <b>{_money_str(0)}</b>\n"
            "Остаток на счету не изменится."
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("❗️ Да, обнулить", callback_data=f"bal:zerogo:{code}"),
            InlineKeyboardButton("Отмена", callback_data="bal:zero"),
        ]])
        await _show_screen(context, query, text, markup)
        return

    if action == "zerogo":
        code = parts[2] if len(parts) > 2 else ""
        cfg = _COST_FIELDS.get(code)
        if not cfg:
            await query.answer("⚠️ Неизвестный счётчик.", show_alert=True)
            return
        was, _ = _read_number(cfg["key"], "money")
        set_setting(cfg["key"], "0")
        logger.info("🔧 Владелец %s обнулил счётчик расхода %s (было %s)",
                    user_id, cfg["key"], _money_str(was))
        _audit(user_id, "cost_zero", 0, f"{cfg['name']}: было {_money_str(was)} → $0")
        await query.answer("✅ Счётчик обнулён")
        text, markup = _build_zero_panel(
            f"✅ <b>{cfg['name']}</b>: было {_money_str(was)} → стало {_money_str(0)}")
        await _show_screen(context, query, text, markup)
        return

    await query.answer("⚠️ Неизвестная кнопка экрана счетов.", show_alert=True)


async def handle_balance_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """
    Ввод числа для экрана «💰 Счета и квоты». Зовётся из handlers/messages.py,
    когда в личке ВЛАДЕЛЬЦА висит ожидание `user_data["balance_edit"]`.

    Возвращает True — сообщение съедено этим режимом (в ИИ не пойдёт).
    Не число → режим НЕ гаснет: бот объясняет ошибку самоудаляемым сообщением
    и продолжает ждать (выйти можно кнопкой «Отмена» или любой командой).
    """
    field_id = context.user_data.get("balance_edit")
    if not field_id:
        return False
    info = _balance_field(field_id)
    chat_id = update.effective_chat.id
    # Сообщение с числом в чате не нужно — панель показывает итог сама.
    await delete_user_message_safe(update.message)
    if not info:
        context.user_data.pop("balance_edit", None)
        await send_balance_panel(context.bot, chat_id, update.effective_user.id,
                                "⚠️ Такого счётчика больше нет — значение не изменено.")
        return True

    raw = (text or "").strip()
    was_str = _value_str(info["key"], info["kind"], info["absent"])

    # Прочерк — убрать значение СОВСЕМ (не ноль: см. delete_setting).
    if raw in ("-", "–", "—"):
        delete_setting(info["key"])
        context.user_data.pop("balance_edit", None)
        logger.info("🔧 Владелец %s убрал значение %s (было %s)",
                    update.effective_user.id, info["key"], raw)
        _audit(update.effective_user.id, "balance", 0, f"{info['short']}: убрано")
        await send_balance_panel(context.bot, chat_id, update.effective_user.id,
                                f"✅ <b>{html.escape(info['short'])}</b>: было {was_str} → "
                                f"стало <i>{info['absent']}</i>")
        return True

    value, err = _parse_number(raw, info["kind"])
    if value is None:
        warn = await context.bot.send_message(
            chat_id=chat_id,
            text=(f"⚠️ {err}.\n"
                  f"Пришли число — например <code>{info['example']}</code>, "
                  f"или нажми «❌ Отмена» на экране выше."),
            parse_mode=ParseMode.HTML,
        )
        if warn:
            # Самоудаляемое, а НЕ через гигиену панелей: иначе предупреждение
            # снесло бы сам экран ввода, с которого человек и должен продолжить.
            schedule_delete(context.bot, chat_id, warn.message_id, 15)
        return True

    set_setting(info["key"], str(int(value)) if info["kind"] == "tokens" else f"{value:.6f}")
    context.user_data.pop("balance_edit", None)
    new_str = (f"<b>{_tokens_str(value)}</b>" if info["kind"] == "tokens"
               else f"<b>{_money_str(value)}</b>")
    logger.info("🔧 Владелец %s изменил %s: %s → %s",
                update.effective_user.id, info["key"], raw, value)
    _audit(update.effective_user.id, "balance", 0,
           f"{info['short']}: стало {_tokens_str(value) if info['kind'] == 'tokens' else _money_str(value)}")
    await send_balance_panel(context.bot, chat_id, update.effective_user.id,
                            f"✅ <b>{html.escape(info['short'])}</b>: было {was_str} → стало {new_str}")
    return True


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
        # Самообновление (2026-07-27): бот сам раз в 10 минут смотрит, нет ли
        # на GitHub новой версии, и забирает её в тишине. Тумблер нужен, чтобы
        # можно было спокойно отправлять правки пачками, не выкатывая каждую.
        # Надпись — только через _onoff (стандарт тумблеров, см. раздел 4 карты).
        # Оба — управление самим ботом, поэтому стоят одним рядом в два столбца
        # (решение Максима 2026-07-27). Надпись перезапуска укорочена с
        # «ПЕРЕЗАПУСТИТЬ БОТА»: в половину ширины длинная не влезает.
        # Обе кнопки владельческие (в _CALLBACK_RULES их нет) — у модератора
        # ряд пропадает целиком, заглушек в нём нет.
        [
            InlineKeyboardButton(
                f"⬇️ САМООБНОВЛЕНИЕ: {_onoff(get_setting('auto_update_enabled', AUTO_UPDATE_ENABLED_DEFAULT) == '1')}",
                callback_data="adm_autoupdate"),
            InlineKeyboardButton("🔄 ПЕРЕЗАПУСК", callback_data="system_restart"),
        ],
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

    # Версия в заголовке — способ увидеть, какая сборка РЕАЛЬНО работает
    # (на сервере она меняется только после того, как он забрал правки).
    # Синей ссылкой на код этой версии — обычный текст в Телеграме покрасить
    # нельзя, синим рисуются только ссылки.
    if owner:
        text = (f"🎛 <b>АДМИН-ПАНЕЛЬ</b> · {BOT_VERSION_HTML}\n"
                "───────────────────────────\n"
                "<i>Выберите раздел управления:</i>")
    elif keyboard:
        text = (f"🛡 <b>ПАНЕЛЬ МОДЕРАТОРА</b> · {BOT_VERSION_HTML}\n"
                "───────────────────────────\n"
                "<i>Доступны разделы по твоим правам:</i>")
    else:
        # Модератор, у которого сняли все галочки: не молчим, чтобы человек
        # понимал, что дело в правах, а не в поломке бота.
        text = (f"🛡 <b>ПАНЕЛЬ МОДЕРАТОРА</b> · {BOT_VERSION_HTML}\n"
                "───────────────────────────\n"
                "<i>Прав пока не выдано — обратись к владельцу бота.</i>")

    sent_msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        # Без этого под панелью развернётся карточка GitHub со ссылки версии.
        link_preview_options=LinkPreviewOptions(is_disabled=True),
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
