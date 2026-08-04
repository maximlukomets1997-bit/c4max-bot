# ───────────────────────────────────────────────
#  handlers/admin/panel_balance.py — экран «💰 Счета и квоты».
#
#  Выделен из panel_main.py 2026-08-04 разрезом БЕЗ изменения логики: экран
#  ни разу не пересекается ни с /adm, ни с панелью моделей — у него свой
#  префикс кнопок bal:, свой ввод чисел и свои реестры. В panel_main он
#  занимал 338 строк из 820.
# ───────────────────────────────────────────────
import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import AVAILABLE_MODELS, PROVIDER_ICONS, PROVIDERS
from database.history import set_setting, get_setting, delete_setting, get_qwen_tokens
from services import roles
from utils import delete_user_message_safe, schedule_delete

logger = logging.getLogger(__name__)
from .common import _adm_back_row, _audit, _send_panel_message


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
# из которых вычитает add_provider_cost (одна на всех, ключи берёт из реестра).
# Денежного остатка у Qwen намеренно нет (решение Максима 2026-07-27): там
# бесплатная квота в токенах, а не счёт в долларах.
# ⚠️ СОБИРАЮТСЯ ИЗ РЕЕСТРА `config.PROVIDERS` (2026-08-03) — ключи, имена и
# надписи кнопок там, здесь только ПОРЯДОК НА ЭКРАНЕ: кнопки счетов разложены
# руками по два в ряд, и блоки текста идут тем же порядком, что они. Провайдер,
# забытый в этих кортежах, на экран не попадёт — поэтому полноту сверяет
# `preflight.py` (проверка «провайдеры»), а не внимательность.
_BALANCE_ORDER = ("deepseek", "xiaomi", "image")
_COST_ORDER = ("deepseek", "qwen", "xiaomi", "image")

_BALANCE_FIELDS = {
    pid: {"key": PROVIDERS[pid]["balance_key"], "provider": pid,
          "name": PROVIDERS[pid]["title"], "btn": PROVIDERS[pid]["balance_btn"]}
    for pid in _BALANCE_ORDER
}

# Счётчики «потрачено» — их можно обнулить (при смене ключа или рабочего
# пространства). Обнуление руками нужно потому, что вечные счётчики
# (DeepSeek, Xiaomi) месячный сброс не трогает вовсе, а у месячных
# (Qwen, картинки) бывает нужно начать счёт заново посреди месяца.
_COST_FIELDS = {
    pid: {"key": PROVIDERS[pid]["cost_key"], "provider": pid,
          "name": PROVIDERS[pid]["title"]}
    for pid in _COST_ORDER
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

    # Денежные блоки — в том же порядке, что кнопки ниже.
    for field_id in _BALANCE_ORDER:
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
