# ───────────────────────────────────────────────
#  jobs/cleanup.py — Суточная чистка и месячный сброс статистики API
#
#  Выделен из jobs.py 2026-08-04 разрезом БЕЗ изменения логики
#  (файл был на 837 строк и держал шесть независимых циклов).
#  Чистка и сброс идут одним циклом намеренно: обе работы суточные, и второй
#  цикл ради сброса просыпался бы в ту же минуту.
# ───────────────────────────────────────────────

import logging
import asyncio

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────
#  Месячный сброс статистики API (первое число месяца, по Киеву)
# ───────────────────────────────────────────────

_MONTHS_RU = ("январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


def _current_month_kyiv() -> str:
    """Текущий месяц по Киеву в виде «ГГГГ-ММ» (метка settings stats_reset_month)."""
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/Kyiv"))
    except Exception:
        # Страховка без tzdata: фиксированное летнее смещение Киева (UTC+3)
        now = datetime.now(timezone(timedelta(hours=3)))
    return now.strftime("%Y-%m")


def _month_label(month_key: str) -> str:
    """«2026-07» → «июль 2026» — для сообщения с итогами месяца."""
    try:
        year, month = month_key.split("-")
        return f"{_MONTHS_RU[int(month) - 1]} {year}"
    except Exception:
        return month_key


async def _monthly_stats_reset(application) -> None:
    """
    Месячный сброс счётчиков панели «📡 Настройки API»: вызовы моделей
    (таблица api_calls) и накопленные расходы (qwen/image_cost_usd).
    ⚠️ Расходы DeepSeek (deepseek_cost_usd), Xiaomi (xiaomi_cost_usd) и
    (deepseek_cost_usd, xiaomi_cost_usd) НЕ обнуляются — ведутся «за всё время»
    и сверяются с кабинетами провайдеров (решение Максима: DeepSeek 2026-07-21,
    Xiaomi 2026-07-25).
    У всех троих в settings есть остаток счёта, который тает сам, — обнуление
    расхода разрушило бы сверку.

    Вызывается раз в сутки из cleanup_loop: сверяет текущий месяц (по Киеву)
    с меткой settings 'stats_reset_month'. Месяц сменился → каждому админу
    в личку уходят «📊 Итоги месяца» (сообщение НЕ регистрируется в гигиене
    панелей — остаётся в чате как история расходов), затем счётчики
    обнуляются. Если бот был выключен 1-го числа, сброс происходит при
    первой проверке в новом месяце. Первый запуск после внедрения: метка
    просто ставится, без сброса. Вместе со счётчиками API обнуляются и
    обмены «вопрос-ответ» (user_token_usage) — поэтому в панели /stats
    они подписаны «за этот месяц».
    """
    from telegram.constants import ParseMode
    from config import ADMIN_IDS
    from database.history import (
        get_setting, set_setting, get_bot_stats, clear_api_calls, clear_user_token_usage,
    )

    current_month = _current_month_kyiv()
    last_month = get_setting("stats_reset_month", "")

    # Первый запуск после внедрения: ставим метку и выходим, ничего не обнуляя.
    if not last_month:
        set_setting("stats_reset_month", current_month)
        return
    if last_month == current_month:
        return

    # ── Месяц сменился: собираем итоги ДО обнуления ──
    stats = get_bot_stats()

    def _cost(key: str) -> float:
        try:
            return float(get_setting(key, "0") or 0)
        except (TypeError, ValueError):
            return 0.0

    # Расходы — по реестру провайдеров (config.PROVIDERS). Вечные счётчики
    # (monthly_reset=False) идут первыми и с пометкой «за всё время»: их
    # месячный сброс не трогает, они сверяются с кабинетом провайдера.
    from config import PROVIDERS
    spent = {pid: _cost(meta["cost_key"])
             for pid, meta in PROVIDERS.items() if meta["cost_key"]}
    eternal = [pid for pid, meta in PROVIDERS.items()
               if meta["cost_key"] and not meta["monthly_reset"]]
    monthly = [pid for pid, meta in PROVIDERS.items()
               if meta["cost_key"] and meta["monthly_reset"]]
    cost_lines = "".join(
        f"  • {PROVIDERS[pid]['title']}: <b>${spent[pid]:.6f}</b>"
        f"{' <i>(за всё время, не обнуляется)</i>' if pid in eternal else ''}\n"
        for pid in eternal + monthly
    )

    calls_lines = "\n".join(
        f"  • <code>{name}</code>: <b>{cnt}</b>"
        for name, cnt in stats["api_calls_by_model"]
    ) or "  • вызовов не было"

    text = (
        f"📊 <b>Итоги месяца: {_month_label(last_month)}</b>\n"
        f"───────────────────────────\n"
        f"💰 <b>Расходы:</b>\n"
        f"{cost_lines}"
        f"───────────────────────────\n"
        f"📡 <b>Вызовы API: {stats['api_calls_total']}</b>\n"
        f"{calls_lines}\n"
        f"───────────────────────────\n"
        f"💬 <b>Обменов «вопрос-ответ»: {stats['lifetime_requests']}</b>\n"
        f"───────────────────────────\n"
        f"<i>Счётчики обнулены — новый месяц считается с нуля.</i>"
    )

    # Итоги — каждому админу в личку. Ошибка отправки одному админу не мешает
    # ни другим админам, ни самому сбросу (история хотя бы попадёт в лог ниже).
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.send_message(
                chat_id=admin_id, text=text, parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось отправить итоги месяца админу %s: %s", admin_id, e)

    # ── Обнуление ──
    # ⚠️ СНАЧАЛА откладываем то, что принадлежит текущим (ещё не отчитанным)
    # суткам: сброс сотрёт вызовы и копилки Qwen/картинок, а суточный отчёт
    # за 1-е число считается разницей счётчиков — без этого он занизил бы
    # цифры на всё, что накапало с полуночи до момента сброса.
    try:
        from services import daily_report
        daily_report.note_monthly_reset()
    except Exception as e:
        logger.warning("⚠️ Не удалось отложить счётчики для суточного отчёта: %s", e)

    # Счётчик DeepSeek НЕ обнуляется (решение Максима 2026-07-21): он ведётся
    # «за всё время» и сверяется с накоплением в кабинете DeepSeek — месячный
    # сброс разрушал бы эту сверку. Qwen и картинки остаются месячными.
    deleted = clear_api_calls()
    users_reset = clear_user_token_usage()
    # Обнуляем ТОЛЬКО месячные копилки — какие именно, знает реестр
    # (monthly_reset). Раньше это были две строки руками, и «вечность»
    # счётчика жила в трёх местах сразу: здесь, в тексте итогов и в переносе
    # daily_report.note_monthly_reset. Теперь признак один на всех.
    for pid in monthly:
        set_setting(PROVIDERS[pid]["cost_key"], "0")
    set_setting("stats_reset_month", current_month)
    logger.info("🚀 Месячный сброс статистики за %s: удалено записей вызовов %d, обнулены обмены у %d пользователей, расходы %s",
                _month_label(last_month), deleted, users_reset,
                " / ".join(f"{PROVIDERS[pid]['title']} ${spent[pid]:.6f}"
                           for pid in eternal + monthly))
# ───────────────────────────────────────────────
#  Суточная очистка архива групповых сообщений
# ───────────────────────────────────────────────

async def cleanup_loop(application):
    """
    Раз в сутки чистит устаревшие данные и проверяет месячный сброс:
      • архив group_messages — старше 10 дней;
      • журнал модерации + улики (moderation_log/mute_evidence) — старше 7 дней;
      • журнал базы знаний — старше KB_LOG_DAYS дней;
      • журнал вступлений в группы — старше JOIN_LOG_DAYS дней;
      • снимки счётчиков суточного отчёта — старше 400 дней (кроме последнего);
      • смена месяца (по Киеву) → итоги админам + обнуление вызовов и расходов
        (_monthly_stats_reset, метка settings 'stats_reset_month').
    Первый прогон — сразу при старте, далее каждые 24 часа.
    application нужен месячному сбросу для отправки итогов админам.
    """
    from database.history import delete_old_group_messages, delete_old_moderation_log, delete_old_kb_log
    from services.antispam import MOD_STATS_DAYS
    from config import KB_LOG_DAYS
    logger.info("🚀 Запущен фоновый цикл очистки (архив групп 10 дней, журнал модерации %d дней, журнал базы знаний %d дней, месячный сброс статистики API)",
                MOD_STATS_DAYS, KB_LOG_DAYS)
    while True:
        try:
            delete_old_group_messages(days=10)
        except Exception as e:
            logger.error("⚠️ Не удалось очистить архив групповых сообщений: %s", e)
        try:
            delete_old_moderation_log(days=MOD_STATS_DAYS)
        except Exception as e:
            logger.error("⚠️ Не удалось очистить журнал модерации: %s", e)
        try:
            delete_old_kb_log(days=KB_LOG_DAYS)
        except Exception as e:
            logger.error("⚠️ Не удалось очистить журнал базы знаний: %s", e)
        try:
            from database.history import delete_old_staff_log
            from config import STAFF_LOG_DAYS
            delete_old_staff_log(days=STAFF_LOG_DAYS)
        except Exception as e:
            logger.error("⚠️ Не удалось очистить журнал персонала: %s", e)
        try:
            # Журнал проактивных проверок (2026-07-31): месяц истории —
            # экран подробностей показывает неделю, месяц оставлен на случай
            # «а как было до правки промпта в прошлом месяце».
            from database.history import delete_old_proactive_log
            from config import PROACTIVE_LOG_DAYS
            delete_old_proactive_log(days=PROACTIVE_LOG_DAYS)
        except Exception as e:
            logger.error("⚠️ Не удалось очистить журнал проактивных проверок: %s", e)
        try:
            # Журнал вступлений (2026-08-04): месяц истории — панель /mod
            # показывает неделю, месяц оставлен на случай «а сколько народу
            # приходило, пока проверка была выключена».
            from database.history import delete_old_join_log
            from config import JOIN_LOG_DAYS
            delete_old_join_log(days=JOIN_LOG_DAYS)
        except Exception as e:
            logger.error("⚠️ Не удалось очистить журнал вступлений: %s", e)
        try:
            # Снимки счётчиков для суточного отчёта: один в сутки, храним ~год.
            # Последний снимок функция не трогает — от него идёт текущий период.
            from database.history import delete_old_stats_snapshots
            delete_old_stats_snapshots(days=400)
        except Exception as e:
            logger.error("⚠️ Не удалось очистить снимки счётчиков расходов: %s", e)
        try:
            await _monthly_stats_reset(application)
        except Exception as e:
            logger.error("⚠️ Не удалось выполнить месячный сброс статистики API: %s", e)
        await asyncio.sleep(24 * 3600)
