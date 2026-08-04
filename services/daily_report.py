# ───────────────────────────────────────────────
#  services/daily_report.py — отчёты о расходах (панель «📡 Настройки API»):
#  СУТОЧНЫЙ (каждую полночь) и НЕДЕЛЬНЫЙ (понедельник 00:00 по Киеву)
#
#  Отчёт показывает ровно те счётчики, что видны в панели управления моделями:
#  вызовы по каждой модели, расход $ по провайдерам, остаток бесплатной квоты
#  Qwen и остатки на счетах. Но панель показывает НАКОПЛЕННОЕ, а нужен расход
#  ЗА СУТКИ — поэтому раз в сутки в полночь по Киеву бот делает «фотографию»
#  счётчиков (таблица stats_snapshots) и показывает РАЗНИЦУ с прошлой полуночью.
#  НЕДЕЛЬНЫЙ отчёт — СУММА уже посчитанных суток (копилка в settings), а не
#  разница снимков за 7 дней: почему именно так — в разделе «Недельный отчёт».
#  Рисует оба отчёта ОДНА функция `render` — чтобы они не разъехались по виду.
#
#  Кто зовёт:
#    • jobs.py::daily_report_loop  — в 00:00 по Киеву: midnight_report() → в личку владельцу,
#      и там же, если неделя закрылась (week_closed), weekly_report() вторым сообщением;
#    • handlers/admin/panel_main.py — кнопки «📊 Отчёт за вчера» (last_report_text)
#      и «📅 Отчёт за неделю» (last_weekly_text) в панели «📡 Настройки API».
#
#  ⚠️ Вызовы за период считаются ПО ВРЕМЕНИ из таблицы api_calls, а расходы —
#  разницей снимков. Раз в месяц бот сам обнуляет api_calls и копилки Qwen и
#  картинок (jobs.py::_monthly_stats_reset) — чтобы отчёт за 1-е число не
#  занизил цифры, сброс перед обнулением откладывает уничтожаемую часть
#  в «перенос» (note_monthly_reset ниже), а отчёт её прибавляет.
# ───────────────────────────────────────────────

import html
import json
import logging
from datetime import datetime, timezone, timedelta

from config import AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, PROVIDER_ICONS, PROVIDER_ICON_FALLBACK, PROVIDERS
import database.history as hist

logger = logging.getLogger(__name__)

# Ключ settings, где лежит текст ПОСЛЕДНЕГО отправленного суточного отчёта.
# Кнопка «📊 Отчёт за вчера» показывает именно его — тот же текст, что пришёл
# ночью. Пересобирать отчёт заново нельзя: после месячного обнуления вызовов
# исходных данных за те сутки в базе уже нет, и пересборка соврала бы.
_LAST_TEXT_KEY = "daily_report_last_text"

# Ключ settings с «переносом» — тем, что месячный сброс уничтожил внутри
# ТЕКУЩЕГО (ещё не отчитанного) периода. JSON: {"calls": {модель: N},
# "qwen_cost": число, "image_cost": число}. Обнуляется при каждом отчёте.
_CARRY_KEY = "daily_report_carry"

# Недельный отчёт (понедельник 00:00 по Киеву). Копилка — JSON с суммой уже
# посчитанных суток: {"start_utc", "end_utc", "days", "calls", "burned",
# "qwen_reset", "image_cost", "qwen_cost", "deepseek_cost", "*_manual"}.
# Подробности и причина, почему сумма, а не разница снимков, — в разделе
# «Недельный отчёт» внизу файла.
_WEEK_KEY = "weekly_report_acc"
_WEEK_LAST_TEXT_KEY = "weekly_report_last_text"

# Месяцы в родительном падеже — для подписи «24 июля 2026»
# (_MONTHS_RU в jobs.py стоит в именительном: «июль» — там он для итогов месяца).
_MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря")


# ───────────────────────────────────────────────
#  Время: Киев и UTC
# ─────────────────────────────────────────────

def _kyiv_tz():
    """Часовой пояс Киева. Без tzdata — запасное летнее смещение UTC+3
    (та же страховка, что в jobs.py и database/history.py)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Kyiv")
    except Exception:
        return timezone(timedelta(hours=3))


def _utc_str(moment: datetime) -> str:
    """UTC «ГГГГ-ММ-ДД ЧЧ:ММ:СС» — тот же формат, в котором SQLite пишет
    called_at (иначе строковое сравнение времени перестанет работать)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc(text: str) -> datetime | None:
    """Обратное превращение строки снимка во время (UTC)."""
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _to_kyiv(moment: datetime) -> datetime:
    return moment.astimezone(_kyiv_tz())


def kyiv_now() -> datetime:
    return datetime.now(_kyiv_tz())


def kyiv_label(moment: datetime) -> str:
    """Подпись снимка по Киеву — «2026-07-25 00:00»."""
    return _to_kyiv(moment).strftime("%Y-%m-%d %H:%M")


def seconds_to_next_midnight() -> float:
    """Сколько секунд осталось до ближайшей полуночи по Киеву (+5 сек запаса,
    чтобы проснуться уже В НОВЫХ сутках, а не за миг до них)."""
    now = kyiv_now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (tomorrow - now).total_seconds() + 5)


def _period_label(start: datetime, end: datetime) -> str:
    """Подпись периода. Ровные сутки от полуночи до полуночи — «24 июля 2026
    (00:00–24:00 по Киеву)»; всё остальное (бот был выключен, первый неполный
    период) — честным диапазоном «с 24.07 15:20 по 25.07 00:00»."""
    s, e = _to_kyiv(start), _to_kyiv(end)
    length = (e - s).total_seconds()
    if s.hour == 0 and s.minute == 0 and e.hour == 0 and e.minute == 0 and 23 * 3600 <= length <= 25 * 3600:
        return f"{s.day} {_MONTHS_GEN[s.month - 1]} {s.year} (00:00–24:00 по Киеву)"
    return f"с {s.strftime('%d.%m %H:%M')} по {e.strftime('%d.%m %H:%M')} (по Киеву)"


def _period_note(start: datetime, end: datetime) -> str:
    """Предупреждение, если период — не ровные сутки. Врать «за сутки», когда
    бот полночь проспал, нельзя: цифры тогда не сравнить с соседними днями."""
    length = (_to_kyiv(end) - _to_kyiv(start)).total_seconds()
    if length > 25 * 3600:
        return "⚠️ <i>Бот был выключен в полночь — в отчёт попало больше одних суток.</i>\n"
    if length < 23 * 3600:
        return "⚠️ <i>Период неполный — бот работал не все сутки.</i>\n"
    return ""


# ───────────────────────────────────────────────
#  Счётчики: снимок и «перенос» месячного сброса
# ─────────────────────────────────────────────

def _money(key: str) -> float:
    """Число из settings (там всё строками) с запасным нулём."""
    try:
        return float(hist.get_setting(key, "0") or 0)
    except (TypeError, ValueError):
        return 0.0


def collect_counters() -> dict:
    """Текущие значения всех счётчиков панели «📡 Настройки API»: копилки
    расходов по каждому провайдеру, остатки на счетах и остаток квоты по каждой
    модели Qwen.
    Вызовы сюда НЕ входят — они считаются по времени из таблицы api_calls."""
    out = {}
    # Копилки и остатки — по реестру провайдеров (config.PROVIDERS): имена
    # ключей снимка складываются как «<провайдер>_cost» и «<провайдер>_balance».
    # Нового провайдера сюда вписывать не нужно — он приедет с реестром.
    for pid, meta in PROVIDERS.items():
        if meta["cost_key"]:
            out[f"{pid}_cost"] = _money(meta["cost_key"])
        if meta["balance_key"]:
            out[f"{pid}_balance"] = _money(meta["balance_key"])
    out["qwen_tokens"] = hist.get_qwen_tokens()
    return out


def _carry_read() -> dict:
    """Перенос от месячного сброса (см. шапку файла). Ошибка разбора = переноса
    нет: лучше слегка занизить одну строку, чем не отправить отчёт вовсе."""
    try:
        raw = hist.get_setting(_CARRY_KEY, "") or ""
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _carry_clear() -> None:
    hist.set_setting(_CARRY_KEY, "")


def note_monthly_reset() -> None:
    """Зовётся из jobs.py::_monthly_stats_reset ПЕРЕД обнулением счётчиков.

    Месячный сброс стирает таблицу вызовов и копилки Qwen и картинок. Часть
    этих цифр относится к ТЕКУЩИМ суткам, за которые отчёт ещё не отправлен, —
    без переноса отчёт за 1-е число показал бы только то, что накапало ПОСЛЕ
    обнуления. Поэтому уничтожаемую часть откладываем в settings, а отчёт её
    прибавит и перенос обнулит.

    Тихая: сбой переноса не должен мешать самому месячному сбросу.
    """
    try:
        last = hist.get_last_stats_snapshot()
        if not last:
            return  # снимков ещё нет — периода, которому что-то принадлежит, тоже
        carry = _carry_read()
        # Вызовы, накопленные с последнего снимка (их удалит clear_api_calls)
        calls = carry.get("calls") or {}
        for model_name, cnt in hist.count_api_calls_between(last["taken_at_utc"]):
            calls[model_name] = calls.get(model_name, 0) + cnt
        carry["calls"] = calls
        # Копилки Qwen и картинок обнуляются целиком — переносим их значение
        # ЦЕЛИКОМ: расход периода = (значение на момент сброса − снимок) + то,
        # что накапает после сброса. См. формулу в шапке файла.
        # Переносим ТОЛЬКО те копилки, которые месячный сброс обнуляет
        # (признак monthly_reset в реестре). Вечные счётчики — DeepSeek,
        # Xiaomi, OpenRouter — он не трогает, и переносить их нечего.
        for pid, meta in PROVIDERS.items():
            if meta["cost_key"] and meta["monthly_reset"]:
                key = f"{pid}_cost"
                carry[key] = float(carry.get(key, 0.0)) + _money(meta["cost_key"])
        hist.set_setting(_CARRY_KEY, json.dumps(carry, ensure_ascii=False))
        logger.info("📊 Суточный отчёт: перед месячным сбросом отложено %d моделей вызовов, Qwen $%.6f, картинки $%.6f",
                    len(calls), carry.get("qwen_cost", 0.0), carry.get("image_cost", 0.0))
    except Exception as e:
        logger.warning("⚠️ Не удалось отложить счётчики перед месячным сбросом: %s", e)


# ───────────────────────────────────────────────
#  Сборка текста отчёта
# ─────────────────────────────────────────────

def _fmt_num(value) -> str:
    """12345 → «12 345» (неразрывного пробела не нужно — Telegram не ломает)."""
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _spent(current: float, base: float, carried: float = 0.0) -> tuple[float, bool]:
    """Расход за период = (текущее + отложенное) − снимок. Второе значение —
    признак «счётчик переставили руками»: разница ушла в минус, и цифре верить
    нельзя (так бывает, если значение в settings правили вручную)."""
    value = (current + carried) - base
    if value < 0:
        return 0.0, True
    return value, False


def _calls_by_group(calls: dict) -> dict:
    """Раскладывает вызовы по блокам панели: gemini / image / qwen / deepseek /
    xiaomi / openrouter и «прочие» (модель удалена из конфига, а вызовы за
    период были). Порядок внутри блока — по числу вызовов, как в панели.
    Блоки берутся из реестра config.PROVIDERS (2026-08-03) — нового провайдера
    вписывать сюда не нужно, он приедет вместе с реестром. Раньше блоки были
    перечислены руками, и забытый провайдер молча уезжал в «прочие»."""
    groups = {pid: [] for pid in PROVIDERS}
    groups["other"] = []
    seen = set()

    for model_name, meta in AVAILABLE_MODELS.items():
        provider = meta.get("provider")
        if provider not in groups:
            continue
        groups[provider].append((model_name, calls.get(model_name, 0)))
        seen.add(model_name)
    for model_name in AVAILABLE_IMAGE_MODELS:
        groups["image"].append((model_name, calls.get(model_name, 0)))
        seen.add(model_name)
    for model_name, cnt in calls.items():
        if model_name not in seen and cnt:
            groups["other"].append((model_name, cnt))

    for provider in groups:
        groups[provider].sort(key=lambda pair: (-pair[1], pair[0]))
    return groups


def _qwen_line(model_name: str, cnt: int, burned, cur_left, partial: bool = False) -> str:
    """Строка модели Qwen: вызовы + сожжённые за период токены + остаток квоты.

    burned  — сожжено за период (None = сравнивать не с чем; отрицательное =
              остаток ВЫРОС, значит квоту завели заново после смены ключа, и
              разница бессмысленна);
    partial — недельный отчёт, внутри которого квоту переставляли: сумма по
              дням тогда неполная, и врать точной цифрой нельзя.
    """
    parts = [f"  • <code>{html.escape(model_name)}</code>: <b>{cnt}</b>"]
    if burned is not None:
        if burned > 0:
            mark = "сожжено ≥" if partial else "сожжено"
            parts.append(f"{mark} <b>{_fmt_num(burned)}</b>")
        elif burned < 0:
            parts.append("<i>квота переставлена вручную</i>")
    if partial and (burned is None or burned >= 0):
        parts.append("<i>квоту переставляли</i>")
    if cur_left is None:
        parts.append("<i>квота не задана</i>")
    else:
        parts.append(f"осталось <b>{_fmt_num(cur_left)}</b>")
    return " · ".join(parts) + "\n"


def _plain_lines(rows: list) -> str:
    """Строки блока без токенов: «  • модель: N». Имя модели экранируется:
    в блок «прочие» оно приходит из базы, и символ «<» сломал бы отправку."""
    if not rows:
        return "  • нет данных\n"
    return "".join(f"  • <code>{html.escape(name)}</code>: <b>{cnt}</b>\n" for name, cnt in rows)


def period_totals(start_utc: str, end_utc: str,
                  base: dict, current: dict, carry: dict) -> dict:
    """Итог периода В ЦИФРАХ: вызовы по моделям, сожжённые токены Qwen и расход
    по каждому провайдеру. Отдельно от рисования, потому что цифры приходят из ДВУХ разных
    мест: у суточного отчёта — из разницы снимков (эта функция), у недельного —
    сложением уже посчитанных суток из копилки. Рисует их одна и та же
    функция `render` — иначе два отчёта со временем разъехались бы по виду.
    """
    calls = {name: cnt for name, cnt in hist.count_api_calls_between(start_utc, end_utc)}
    for name, cnt in (carry.get("calls") or {}).items():
        calls[name] = calls.get(name, 0) + cnt

    # Сожжённые токены Qwen: остаток на начало минус остаток на конец.
    # Отрицательное значение = квоту завели заново (смена ключа) — оставляем
    # как есть, строка модели покажет это словами.
    burned = {}
    base_tokens = base.get("qwen_tokens") or {}
    cur_tokens = current.get("qwen_tokens") or {}
    for model_name, base_left in base_tokens.items():
        cur_left = cur_tokens.get(model_name)
        if base_left is not None and cur_left is not None:
            burned[model_name] = base_left - cur_left

    out = {
        "calls": calls,
        "burned": burned,
        "qwen_reset": [name for name, value in burned.items() if value < 0],
    }
    # Расход по каждому провайдеру с копилкой — по реестру. Отложенное
    # (carry) прибавляется только там, где копилку обнуляет месячный сброс:
    # у вечных счётчиков переносить нечего, и carry для них всегда пуст.
    for pid, meta in PROVIDERS.items():
        if not meta["cost_key"]:
            continue
        carried = float(carry.get(f"{pid}_cost", 0.0)) if meta["monthly_reset"] else 0.0
        spent, manual = _spent(current.get(f"{pid}_cost", 0.0),
                               base.get(f"{pid}_cost", 0.0), carried)
        out[f"{pid}_cost"] = spent
        out[f"{pid}_manual"] = manual
    return out


def _proactive_block(start_utc: str, end_utc: str, total_calls: int) -> str:
    """
    Блок «Сам в разговор» для суточного отчёта (2026-07-31): сколько раз бот
    проверялся, сколько раз вступил и какую долю вызовов это съело.

    ⚠️ ДОЛЯ РАСХОДА СЧИТАЕТСЯ ПО ЧИСЛУ ВЫЗОВОВ, а не по деньгам, и об этом
    сказано прямо в тексте. Точная стоимость живёт в services/gemini.py, и
    лезть туда ради этой строки не стали (решение 2026-07-31): у активной
    бесплатной Gemini она всё равно нулевая. Цифра скорее ЗАНИЖЕНА:
    проактивный запрос длиннее среднего — в нём вся стенограмма, статьи базы
    знаний и инструкция участия. Переключишься на платную активную модель —
    тогда и делать точный проброс, будет ради чего.

    Медиа-триггеры считаются отдельно: у них на проверку уходит ДВА запроса
    (сначала вложение разбирает отдельная модель, потом сам запрос).
    """
    try:
        stats = hist.proactive_stats(start_utc, end_utc)
    except Exception as e:
        logger.debug("📊 Не удалось собрать итоги участия для отчёта: %s", e)
        return ""
    checks = stats["checks"]
    if not checks:
        return ""

    replies = stats["reply"] + stats["reply_mute"]
    share = f"{round(replies * 100 / checks)}%" if checks else "—"
    media = sum(n for k, n in stats["by_trigger"].items() if k != "text")
    # Вызовов на проверки ушло: по одному на каждую + по одному на разбор
    # каждого вложения.
    spent_calls = checks + media
    of_all = f" · это ≈{round(spent_calls * 100 / total_calls)}% вызовов" if total_calls else ""

    line = (f"───────────────────────────\n"
            f"🗣 <b>Сам в разговор</b>\n"
            f"проверок <b>{checks}</b> · вступил <b>{replies}</b> ({share}) · "
            f"промолчал <b>{stats['silent']}</b>\n")
    if media:
        line += f"<i>из них по вложениям: {media} (каждая стоит двух вызовов)</i>\n"
    line += f"<i>потрачено вызовов: {spent_calls}{of_all}</i>\n"
    return line


def render(header: str, period_line: str, note: str,
           totals: dict, current: dict, extra: str = "",
           proactive_period: tuple | None = None) -> str:
    """Рисует текст отчёта по готовым цифрам периода. ОДНА рисовалка на оба
    отчёта — суточный и недельный.

    header      — заголовок («📊 РАСХОД ЗА СУТКИ»);
    period_line — подпись периода строкой;
    note        — предупреждение под подписью (или пустая строка);
    totals      — цифры периода (см. period_totals);
    current     — счётчики на конец периода: отсюда берутся ОСТАТКИ на счетах
                  и остаток квоты Qwen (они всегда «на сейчас», не за период);
    extra       — дополнительные строки в самом низу (средние за день у недели).
    Порядок блоков и подписи повторяют панель «📡 Настройки API», чтобы отчёт
    читался с ней один в один.
    """
    calls = totals.get("calls") or {}
    burned = totals.get("burned") or {}
    reset_models = set(totals.get("qwen_reset") or ())
    groups = _calls_by_group(calls)
    cur_tokens = current.get("qwen_tokens") or {}

    def _sum(provider: str) -> int:
        return sum(cnt for _, cnt in groups[provider])

    manual_note = " <i>(счётчик правили вручную)</i>"

    total_calls = sum(calls.values())
    total_money = sum(totals.get(f"{pid}_cost", 0.0) for pid in PROVIDERS)

    # Блоки собираются ИЗ РЕЕСТРА config.PROVIDERS (2026-08-03) — тем же
    # порядком и по тем же правилам, что панель «📡 Настройки API», чтобы
    # отчёт читался с ней один в один. Раньше шесть блоков были выписаны
    # здесь руками, и забытый провайдер молча уезжал в «прочие».
    # Особенности, заданные полями реестра:
    #   • quota_tokens — у Qwen вместо простого списка моделей строки с
    #     остатком бесплатной квоты (_qwen_line);
    #   • report_approx — «≈» перед суммой: расход Qwen расчётный по прайсу;
    #   • balance_key — есть счёт, значит под расходом идёт строка остатка.
    text = f"{header}\n{period_line}\n{note}───────────────────────────\n"
    for pid, meta in PROVIDERS.items():
        text += f"{meta['icon']} <b>{meta['calls_label']}: {_sum(pid)}</b>\n"
        if meta["quota_tokens"]:
            text += "".join(_qwen_line(name, cnt, burned.get(name), cur_tokens.get(name),
                                       name in reset_models)
                            for name, cnt in groups[pid])
        else:
            text += _plain_lines(groups[pid])
        if meta["cost_key"]:
            spent = totals.get(f"{pid}_cost", 0.0)
            approx = "≈" if meta["report_approx"] else ""
            text += (f"💰 <b>{meta['money_label']}:</b> {approx}${spent:.6f}"
                     f"{manual_note if totals.get(f'{pid}_manual') else ''}\n")
            if meta["balance_key"]:
                text += f"   Остаток на счету: <b>${current.get(f'{pid}_balance', 0.0):.6f}</b>\n"
        text += "───────────────────────────\n"
    # Хвост «прочих» и итоги дописываются ниже; лишний разделитель в конце
    # цикла снимаем — раньше его тут не было.
    text = text[:-len("───────────────────────────\n")]

    if groups["other"]:
        text += (f"───────────────────────────\n"
                 f"{PROVIDER_ICON_FALLBACK} <b>Прочие вызовы</b> <i>(модели нет в конфиге)</i>\n"
                 f"{_plain_lines(groups['other'])}")

    # Блок «Сам в разговор» — только у суточного отчёта: недельному пришлось бы
    # передавать границы периода, а он считается суммой суток, а не по времени.
    # Понадобится в недельном — это одна строка здесь и одна в weekly_report.
    if proactive_period:
        text += _proactive_block(proactive_period[0], proactive_period[1], total_calls)

    text += (
        f"───────────────────────────\n"
        f"📡 <b>Всего вызовов: {total_calls}</b>\n"
        f"💵 <b>Всего денег: ≈${total_money:.6f}</b>"
    )
    if extra:
        text += f"\n{extra}"
    return text


def build_report(header: str, start_utc: str, end_utc: str,
                 base: dict, current: dict, carry: dict) -> str:
    """Суточный отчёт за период [start_utc, end_utc): считает цифры разницей
    снимков и рисует их общей рисовалкой."""
    start_dt = _parse_utc(start_utc) or datetime.now(timezone.utc)
    end_dt = _parse_utc(end_utc) or datetime.now(timezone.utc)
    totals = period_totals(start_utc, end_utc, base, current, carry)
    return render(header, _period_label(start_dt, end_dt),
                  _period_note(start_dt, end_dt), totals, current,
                  proactive_period=(start_utc, end_utc))


# ───────────────────────────────────────────────
#  Точки входа: полночный отчёт и кнопка панели
# ─────────────────────────────────────────────

def _save_snapshot(moment: datetime, counters: dict) -> None:
    hist.save_stats_snapshot(_utc_str(moment), kyiv_label(moment), counters)


def start_snapshot_if_needed() -> bool:
    """Первый запуск после внедрения отчёта: снимков нет — ставим первый,
    с него пойдёт отсчёт. True — снимок только что поставлен."""
    if hist.get_last_stats_snapshot():
        return False
    now = datetime.now(timezone.utc)
    _save_snapshot(now, collect_counters())
    _carry_clear()
    logger.info("📊 Суточный отчёт: поставлен первый снимок счётчиков — первый отчёт придёт в ближайшую полночь")
    return True


def period_closed() -> bool:
    """Пора ли отчитываться: наступили ли по Киеву новые сутки с момента
    последнего снимка. Это единственное условие отправки — оно же ловит случай
    «бот был выключен в полночь» (при запуске отчёт уйдёт сразу) и не даёт
    слать отчёт на каждый перезапуск в течение одних суток."""
    last = hist.get_last_stats_snapshot()
    if not last:
        return False
    start = _parse_utc(last["taken_at_utc"])
    if not start:
        return False
    return _to_kyiv(start).date() < kyiv_now().date()


def midnight_report(header: str = "📊 <b>РАСХОД ЗА СУТКИ</b>") -> str | None:
    """Закрывает текущий период: собирает отчёт с прошлого снимка по «сейчас»,
    делает новый снимок и обнуляет перенос. Возвращает текст для отправки
    (его же сохраняет для кнопки «Отчёт за вчера») или None, если снимков ещё
    не было — тогда просто ставится первый.

    ⚠️ Снимок ставится ПОСЛЕ сборки текста и той же меткой времени, что и конец
    периода: вызовы, пришедшие во время сборки, попадут в следующий период,
    а не потеряются и не посчитаются дважды.
    """
    last = hist.get_last_stats_snapshot()
    if not last:
        start_snapshot_if_needed()
        return None

    now = datetime.now(timezone.utc)
    now_utc = _utc_str(now)
    current = collect_counters()
    carry = _carry_read()

    start_dt = _parse_utc(last["taken_at_utc"]) or now
    totals = period_totals(last["taken_at_utc"], now_utc, last["data"], current, carry)
    text = render(header, _period_label(start_dt, now), _period_note(start_dt, now),
                  totals, current)

    _save_snapshot(now, current)
    _carry_clear()
    hist.set_setting(_LAST_TEXT_KEY, text)

    # Закрытые сутки идут в недельную копилку — из неё в понедельник соберётся
    # недельный отчёт. Тихо: сбой копилки не должен мешать суточному отчёту.
    try:
        week_add_day(last["taken_at_utc"], now_utc, totals)
    except Exception as e:
        logger.warning("⚠️ Не удалось добавить сутки в недельную копилку: %s", e)
    return text


def _open_day_totals() -> tuple[int, float, datetime | None]:
    """Незакрытый кусок суток: сколько вызовов и денег набежало с последнего
    снимка (обычно с полуночи) по «сейчас». Третьим значением — время снимка.
    Общий счёт для обоих живых хвостов: суточного и недельного."""
    last = hist.get_last_stats_snapshot()
    if not last:
        return 0, 0.0, None
    start_dt = _parse_utc(last["taken_at_utc"])
    if not start_dt:
        return 0, 0.0, None
    now = datetime.now(timezone.utc)
    base = last["data"]
    current = collect_counters()
    carry = _carry_read()
    totals = period_totals(last["taken_at_utc"], _utc_str(now), base, current, carry)
    money = (totals.get("image_cost", 0.0) + totals.get("qwen_cost", 0.0)
             + totals.get("deepseek_cost", 0.0) + totals.get("xiaomi_cost", 0.0)
             + totals.get("openrouter_cost", 0.0))
    return sum((totals.get("calls") or {}).values()), money, start_dt


def today_so_far() -> str:
    """Строка-хвост для кнопки: сколько набежало с последнего снимка (обычно
    с полуночи) до текущей минуты. Считается на лету, снимок НЕ трогает."""
    total, money, start_dt = _open_day_totals()
    if not start_dt:
        return ""
    now = datetime.now(timezone.utc)
    start_kyiv = _to_kyiv(start_dt)
    # Обычный случай — снимок стоит ровно в полночь: пишем «с полуночи».
    # Если бот полночь проспал, показываем настоящее время снимка, а не врём.
    if start_kyiv.hour == 0 and start_kyiv.minute == 0:
        since = "с полуночи"
    else:
        since = f"с {start_kyiv.strftime('%d.%m %H:%M')}"
    e = _to_kyiv(now).strftime("%H:%M")
    return (f"\n───────────────────────────\n"
            f"📈 <i>Сегодня {since} по {e}: вызовов <b>{total}</b> · ≈${money:.6f}</i>")


def last_report_text() -> str | None:
    """Текст последнего отправленного суточного отчёта — для кнопки
    «📊 Отчёт за вчера». None, если полночь ещё ни разу не наступала после
    внедрения. Пересборкой не заменять: после месячного обнуления вызовов
    исходных данных за те сутки в базе уже нет."""
    text = hist.get_setting(_LAST_TEXT_KEY, "") or ""
    return text or None


# ───────────────────────────────────────────────
#  Недельный отчёт (понедельник 00:00 по Киеву)
# ─────────────────────────────────────────────
#
#  Неделя считается НЕ разницей снимков «неделю назад → сейчас», а СУММОЙ уже
#  посчитанных суток: каждый суточный отчёт кладёт свои цифры в копилку, а в
#  понедельник копилка становится отчётом и обнуляется. Причина — месячный
#  сброс: первого числа бот обнуляет вызовы и копилки Qwen и картинок, и прямая
#  разница за неделю, на которую попало 1-е число, показала бы бессмыслицу.
#  Сумма семи суточных цифр от сброса не страдает: каждая из них посчитана
#  с учётом «переноса» (см. note_monthly_reset выше).
# ─────────────────────────────────────────────

def _week_read() -> dict:
    """Недельная копилка. Ошибка разбора = копилки нет: лучше потерять одну
    неделю, чем застрять на нечитаемой записи навсегда."""
    try:
        raw = hist.get_setting(_WEEK_KEY, "") or ""
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _week_write(data: dict) -> None:
    hist.set_setting(_WEEK_KEY, json.dumps(data, ensure_ascii=False))


def _week_clear() -> None:
    hist.set_setting(_WEEK_KEY, "")


def week_add_day(start_utc: str, end_utc: str, totals: dict) -> None:
    """Складывает закрытые сутки в недельную копилку (зовётся из midnight_report
    сразу после сборки суточного отчёта). Начало недели = начало ПЕРВЫХ суток,
    попавших в копилку."""
    acc = _week_read()
    if not acc:
        acc = {"start_utc": start_utc, "days": 0, "calls": {}, "burned": {}, "qwen_reset": []}

    acc["end_utc"] = end_utc
    acc["days"] = int(acc.get("days") or 0) + 1

    calls = acc.get("calls") or {}
    for name, cnt in (totals.get("calls") or {}).items():
        calls[name] = calls.get(name, 0) + cnt
    acc["calls"] = calls

    # Сожжённые токены Qwen складываем только там, где разница осмысленна.
    # Сутки, в которые квоту заводили заново (остаток ВЫРОС), сумму испортили
    # бы — такие модели помечаем, и недельная строка честно скажет «≥».
    burned = acc.get("burned") or {}
    reset = set(acc.get("qwen_reset") or ())
    for name, value in (totals.get("burned") or {}).items():
        if value > 0:
            burned[name] = burned.get(name, 0) + value
        elif value < 0:
            reset.add(name)
    acc["burned"] = burned
    acc["qwen_reset"] = sorted(reset)

    for key in ("image_cost", "qwen_cost", "deepseek_cost", "xiaomi_cost", "openrouter_cost"):
        acc[key] = float(acc.get(key, 0.0)) + float(totals.get(key, 0.0))
    for key in ("image_manual", "qwen_manual", "deepseek_manual", "xiaomi_manual",
                "openrouter_manual"):
        acc[key] = bool(acc.get(key)) or bool(totals.get(key))

    _week_write(acc)


def week_closed() -> bool:
    """Пора ли слать недельный отчёт. Условия два, и второе — страховка:
    • по Киеву наступил понедельник (обычный случай, отчёт за пн–вс);
    • с начала копилки прошло 7 суток и больше — так отчёт уйдёт даже если
      бот проспал понедельник целиком, а не будет копиться до бесконечности.
    Пустая копилка (суточных отчётов ещё не было) — не пора."""
    acc = _week_read()
    if not int(acc.get("days") or 0):
        return False
    if kyiv_now().weekday() == 0:  # 0 = понедельник
        return True
    start = _parse_utc(acc.get("start_utc") or "")
    if start and (kyiv_now() - _to_kyiv(start)).days >= 7:
        return True
    return False


def _week_label(start: datetime | None, end: datetime | None, days: int) -> str:
    """Подпись недели: «20–26 июля 2026 (по Киеву) · дней: 7». Последний день —
    это конец периода минус секунда: конец стоит в полночь СЛЕДУЮЩИХ суток."""
    if not start or not end:
        return f"последние {days} сут. (по Киеву)"
    s = _to_kyiv(start)
    last = _to_kyiv(end) - timedelta(seconds=1)
    if s.year == last.year and s.month == last.month:
        span = f"{s.day}–{last.day} {_MONTHS_GEN[s.month - 1]} {s.year}"
    else:
        span = (f"{s.day} {_MONTHS_GEN[s.month - 1]} – "
                f"{last.day} {_MONTHS_GEN[last.month - 1]} {last.year}")
    return f"{span} (по Киеву) · дней: {days}"


def _week_note(days: int) -> str:
    """Предупреждение, если в отчёт попала не ровная неделя. Врать «за неделю»,
    когда суток было меньше, нельзя: цифры тогда не сравнить с другой неделей."""
    if days < 7:
        return f"⚠️ <i>Неделя неполная — в отчёт попало суток: {days}.</i>\n"
    if days > 7:
        return f"⚠️ <i>В отчёт попало больше недели — суток: {days}.</i>\n"
    return ""


def _week_average(totals: dict, days: int) -> str:
    """Строка «в среднем в день» в самом низу недельного отчёта. Делим на
    число РЕАЛЬНО попавших суток, а не на семь: бот мог день не работать."""
    if days <= 0:
        return ""
    calls = sum((totals.get("calls") or {}).values())
    money = (totals.get("image_cost", 0.0) + totals.get("qwen_cost", 0.0)
             + totals.get("deepseek_cost", 0.0) + totals.get("xiaomi_cost", 0.0)
             + totals.get("openrouter_cost", 0.0))
    return f"📈 <b>В среднем в день:</b> вызовов {round(calls / days)} · ≈${money / days:.6f}"


def weekly_report(header: str = "📅 <b>РАСХОД ЗА НЕДЕЛЮ</b>") -> str | None:
    """Собирает недельный отчёт из копилки, сохраняет его текст для кнопки и
    копилку обнуляет. None — копилка пуста (закрытых суток ещё не было).

    Остатки на счетах и остаток квоты Qwen берутся ТЕКУЩИЕ: это не расход за
    период, а «сколько есть сейчас» — как в панели «📡 Настройки API».
    """
    acc = _week_read()
    days = int(acc.get("days") or 0)
    if not days:
        return None

    current = collect_counters()
    totals = {
        "calls": acc.get("calls") or {},
        "burned": acc.get("burned") or {},
        "qwen_reset": acc.get("qwen_reset") or [],
        "image_cost": float(acc.get("image_cost", 0.0)),
        "qwen_cost": float(acc.get("qwen_cost", 0.0)),
        "deepseek_cost": float(acc.get("deepseek_cost", 0.0)),
        "xiaomi_cost": float(acc.get("xiaomi_cost", 0.0)),
        "openrouter_cost": float(acc.get("openrouter_cost", 0.0)),
        "image_manual": bool(acc.get("image_manual")),
        "qwen_manual": bool(acc.get("qwen_manual")),
        "deepseek_manual": bool(acc.get("deepseek_manual")),
        "xiaomi_manual": bool(acc.get("xiaomi_manual")),
        "openrouter_manual": bool(acc.get("openrouter_manual")),
    }

    text = render(header,
                  _week_label(_parse_utc(acc.get("start_utc") or ""),
                              _parse_utc(acc.get("end_utc") or ""), days),
                  _week_note(days), totals, current, _week_average(totals, days))

    hist.set_setting(_WEEK_LAST_TEXT_KEY, text)
    _week_clear()
    return text


def week_so_far() -> str:
    """Строка-хвост для кнопки «📅 Отчёт за неделю»: сколько набежало с начала
    ТЕКУЩЕЙ недели по эту минуту — закрытые сутки из копилки плюс незакрытый
    кусок сегодняшних. Ничего не сохраняет и копилку не трогает."""
    acc = _week_read()
    open_calls, open_money, snapshot_dt = _open_day_totals()

    total = sum((acc.get("calls") or {}).values()) + open_calls
    money = (float(acc.get("image_cost", 0.0)) + float(acc.get("qwen_cost", 0.0))
             + float(acc.get("deepseek_cost", 0.0)) + float(acc.get("xiaomi_cost", 0.0))
             + float(acc.get("openrouter_cost", 0.0))
             + open_money)

    start_dt = _parse_utc(acc.get("start_utc") or "") or snapshot_dt
    if not start_dt:
        return ""
    since = _to_kyiv(start_dt).strftime("%d.%m %H:%M")
    now = _to_kyiv(datetime.now(timezone.utc)).strftime("%d.%m %H:%M")
    return (f"\n───────────────────────────\n"
            f"📈 <i>С начала недели ({since}) по {now}: вызовов <b>{total}</b> · ≈${money:.6f}</i>")


def last_weekly_text() -> str | None:
    """Текст последнего отправленного недельного отчёта — для кнопки
    «📅 Отчёт за неделю». Как и суточный, НЕ пересобирается: исходных данных за
    прошедшие недели в базе может уже не быть."""
    text = hist.get_setting(_WEEK_LAST_TEXT_KEY, "") or ""
    return text or None
