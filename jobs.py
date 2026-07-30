# ───────────────────────────────────────────────
#  jobs.py — фоновые задачи бота
#
#  Содержит:
#    news_polling_loop()  — проверка новостей каждые 10 минут,
#                           форматирование через модель и рассылка подписчикам
#    cleanup_loop()       — суточная очистка архива групп и журнала модерации
#                           + месячный сброс статистики API (_monthly_stats_reset)
#    rag_catchup_loop()   — ежечасный добор базы знаний: дотягивает статьи,
#                           не проиндексированные из-за лимита Google
#    daily_report_loop()  — суточный (и по понедельникам недельный) отчёт
#                           о расходах владельцу в личку
#    watchdog_loop()      — «отметки живости» внешнему сторожу: по их пропаже
#                           владелец узнаёт, что бот перестал отвечать
#    air_alert_loop()     — воздушная тревога в Днепре: опрос alerts.in.ua
#                           и сообщение ВЛАДЕЛЬЦУ в личку на каждом переломе
# ───────────────────────────────────────────────

import asyncio
import html
import logging
import time

from telegram import InputMediaPhoto

from database.history import (
    is_news_already_sent,
    mark_news_as_sent,
    get_subscribed_chats,
    save_group_message,
)
from services.scraper import fetch_latest_news, fetch_article
from services.gemini import format_news_as_colonel
from services.knowledge_store import save_pending_news
from utils import register_and_clean_bot_message
from utils_format import send_formatted, convert_md, fits_caption

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────
#  Новости: отправка в один чат
# ───────────────────────────────────────────────

async def send_news_to_chat(bot, chat_id: int, text: str, image_url: str, url: str, stat_images: list = None):
    """
    Отправляет новость в чат. Картинки (главная + карточка ТТХ) уходят ОДНИМ
    сообщением-альбомом (media_group). Если текст помещается в подпись (≤1024),
    он идёт подписью к альбому — тогда всё одним сообщением. Если текст длиннее —
    альбом отправляется без подписи, а текст уходит отдельным сообщением после него
    (ограничение Telegram: подпись к альбому максимум 1024 символа).
    """
    md = f"{text}\n\n[🔗 Читать на сайте]({url})"
    plain = f"{text}\n\nЧитать на сайте: {url}"

    try:
        cap_text, cap_entities = convert_md(md)
    except Exception as e:
        logger.warning("⚠️ Не удалось отформатировать новость для чата %s: %s — отправляю без разметки", chat_id, e)
        cap_text, cap_entities = plain, []

    # Собираем картинки: главная + карточка(и) ТТХ
    images = []
    if image_url:
        images.append(image_url)
    if stat_images:
        images.extend(stat_images)
    images = images[:10]  # лимит Telegram на альбом

    caption_fits = fits_caption(cap_text)

    try:
        if len(images) >= 2:
            # Альбом: подпись вешаем на первую картинку (если текст помещается)
            media = []
            for idx, img in enumerate(images):
                if idx == 0 and caption_fits:
                    media.append(InputMediaPhoto(media=img, caption=cap_text, caption_entities=cap_entities))
                else:
                    media.append(InputMediaPhoto(media=img))
            await bot.send_media_group(chat_id=chat_id, media=media)
            if not caption_fits:
                # длинный текст не влезает в подпись альбома — отдельным сообщением
                await send_formatted(bot, chat_id, md, disable_preview=True)

        elif len(images) == 1:
            if caption_fits:
                await bot.send_photo(chat_id=chat_id, photo=images[0],
                                     caption=cap_text, caption_entities=cap_entities)
            else:
                await bot.send_photo(chat_id=chat_id, photo=images[0])
                await send_formatted(bot, chat_id, md, disable_preview=True)

        else:
            await send_formatted(bot, chat_id, md, disable_preview=True)

    except Exception as ex:
        logger.error("⚠️ Не удалось отправить новость в чат %s: %s — отправляю по отдельности", chat_id, ex)
        for img in images:
            try:
                await bot.send_photo(chat_id=chat_id, photo=img)
            except Exception as e:
                logger.warning("⚠️ Не удалось отправить картинку новости в чат %s: %s", chat_id, e)
        await send_formatted(bot, chat_id, md, disable_preview=True)

    # Новость ушла в ГРУППУ (chat_id < 0) — записываем сводку в архив групп как
    # реплику бота: собственные сообщения апдейтами не приходят, а стенограмма
    # режима «Сам в разговор» должна видеть новость, которую обсуждают участники.
    # В личные чаты архив не ведётся. Ошибка записи рассылку не ломает.
    if chat_id < 0:
        try:
            save_group_message(chat_id, bot.id, bot.username or "", bot.first_name or "", text, False)
        except Exception as e:
            logger.debug("📰 Не удалось записать новость в архив групп %s: %s", chat_id, e)


# ───────────────────────────────────────────────
#  Новости: фоновый цикл опроса
# ───────────────────────────────────────────────

async def news_polling_loop(application):
    """
    Фоновый цикл проверки новостей. Запускается один раз при старте бота.
    Периодичность проверки — каждые 600 секунд (10 минут).
    """
    # Рутинные проверки (раз в 10 минут) в лог НЕ пишутся — только события:
    # найдена новая новость, рассылка, ошибка (контракт стиля в logging_setup.py).
    logger.info("📰 Запущен фоновый цикл проверки новостей (каждые 10 минут, в лог пишутся только события)")
    await asyncio.sleep(10)  # Даём боту время запуститься

    while True:
        try:
            loop = asyncio.get_running_loop()
            # Скачивание списка новостей — сетевой вызов: уводим в рабочий поток,
            # иначе при медленном ответе сайта весь бот замирает до 15 секунд
            # (fetch_article ниже уже сделан так же).
            news_items = await loop.run_in_executor(None, fetch_latest_news)

            for item in reversed(news_items):
                url = item["url"]
                if is_news_already_sent(url):
                    continue

                logger.info("📰 Новая новость: «%s» — готовлю сводку", item["title"])

                # Загружаем полный текст статьи + карточки ТТХ
                article = await loop.run_in_executor(None, fetch_article, url)
                # Если активная модель сбоит, цепочка фолбэка внутри
                # services/gemini.py сама уведомит админов (не чаще раза в час).
                formatted_news = await loop.run_in_executor(
                    None, format_news_as_colonel,
                    item["title"], item["description"], item["tag"], article["text"]
                )

                # Рассылаем всем подписчикам
                chat_ids = get_subscribed_chats()
                delivered = 0
                if chat_ids:
                    logger.info("📰 Рассылаю сводку %d подписчикам", len(chat_ids))
                    for chat_id in chat_ids:
                        # ⚠️ Отправка КАЖДОМУ чату защищена отдельно (2026-07-27).
                        # Раньше защиты не было, и один недоступный чат (бота
                        # выгнали, группу удалили, отобрали право писать) ронял
                        # ВЕСЬ цикл: остальные подписчики новость не получали,
                        # отметки «разослано» не было, статья не попадала в
                        # папку ожидания базы знаний — и всё это повторялось
                        # каждые 10 минут, заново тратя запрос к модели.
                        try:
                            await send_news_to_chat(
                                bot=application.bot,
                                chat_id=chat_id,
                                text=formatted_news,
                                # Главная — крупная картинка из статьи; если её нет,
                                # откат на обложку-превью из списка новостей.
                                image_url=article.get("main_image") or item["image_url"],
                                url=url,
                                stat_images=article["stat_images"][:1],  # одна карточка ТТХ
                            )
                            delivered += 1
                        except Exception as send_err:
                            logger.warning("📰 Не удалось отправить новость в чат %s: %s",
                                           chat_id, send_err)
                        await asyncio.sleep(0.1)

                # Отметка ставится ДАЖЕ если не доставили никому: иначе бот
                # будет вечно перезапускать разбор одной и той же новости.
                mark_news_as_sent(url)
                if chat_ids and delivered == 0:
                    logger.error("📰 Новость не дошла НИ ОДНОМУ подписчику (%d чатов) — "
                                 "проверь права бота: %s", len(chat_ids), url)
                elif delivered < len(chat_ids):
                    logger.warning("📰 Новость доставлена %d из %d подписчиков: %s",
                                   delivered, len(chat_ids), url)
                else:
                    logger.info("📰 Новость разослана и зафиксирована в БД: %s", url)

                # Сохраняем ИСХОДНЫЙ текст статьи (не сводку модели) в папку
                # ожидания базы знаний — админ решит в панели, добавлять ли
                # её в RAG. Ошибка сохранения не должна ломать цикл рассылки.
                try:
                    save_pending_news(
                        title=item["title"],
                        tag=item["tag"],
                        url=url,
                        text=article["text"],
                        description=item["description"],
                    )
                except Exception as e:
                    logger.error("⚠️ Не удалось сохранить новость в папку базы знаний: %s", e)

        except Exception as e:
            logger.error("⚠️ Не удалось выполнить цикл проверки новостей: %s", e)

        await asyncio.sleep(600)


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
    ⚠️ Расходы DeepSeek (deepseek_cost_usd) и Xiaomi (xiaomi_cost_usd) НЕ
    обнуляются — ведутся «за всё время» и сверяются с кабинетами провайдеров
    (решение Максима: DeepSeek 2026-07-21, Xiaomi 2026-07-25). У обоих в
    settings есть остаток счёта, который тает сам, — обнуление расхода
    разрушило бы сверку.

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

    ds = _cost("deepseek_cost_usd")
    qw = _cost("qwen_cost_usd")
    img = _cost("image_cost_usd")
    xm = _cost("xiaomi_cost_usd")

    calls_lines = "\n".join(
        f"  • <code>{name}</code>: <b>{cnt}</b>"
        for name, cnt in stats["api_calls_by_model"]
    ) or "  • вызовов не было"

    text = (
        f"📊 <b>Итоги месяца: {_month_label(last_month)}</b>\n"
        f"───────────────────────────\n"
        f"💰 <b>Расходы:</b>\n"
        f"  • DeepSeek: <b>${ds:.6f}</b> <i>(за всё время, не обнуляется)</i>\n"
        f"  • Xiaomi: <b>${xm:.6f}</b> <i>(за всё время, не обнуляется)</i>\n"
        f"  • Qwen: <b>${qw:.6f}</b>\n"
        f"  • Картинки: <b>${img:.6f}</b>\n"
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
    set_setting("qwen_cost_usd", "0")
    set_setting("image_cost_usd", "0")
    set_setting("stats_reset_month", current_month)
    logger.info("🚀 Месячный сброс статистики за %s: удалено записей вызовов %d, обнулены обмены у %d пользователей, расходы DeepSeek $%.6f / Qwen $%.6f / картинки $%.6f",
                _month_label(last_month), deleted, users_reset, ds, qw, img)


# ───────────────────────────────────────────────
#  Суточный отчёт о расходах (00:00 по Киеву, в личку владельцу)
# ───────────────────────────────────────────────

async def daily_report_loop(application):
    """
    Раз в сутки в 00:00 по Киеву присылает ВЛАДЕЛЬЦУ в личку отчёт о расходах
    за прошедшие сутки: вызовы по каждой модели, расход $ по провайдерам и
    сожжённые токены Qwen — те же счётчики, что в панели «📡 Настройки API».

    Как считается: панельные счётчики накопительные, поэтому в полночь бот
    делает «фотографию» их значений и показывает разницу с прошлой полуночью
    (services/daily_report.py). Вызовы берутся точно, по времени из api_calls.

    Первый запуск после внедрения: снимков ещё нет — просто ставится точка
    отсчёта, отчёт придёт в ближайшую полночь.

    Бот был выключен в полночь: условие отправки — «по Киеву наступили новые
    сутки с момента снимка», поэтому пропущенный отчёт уходит сразу при
    запуске, а период в заголовке подписан честно («с 24.07 00:00 по 26.07
    12:40»). Перезапуск в течение суток отчёт НЕ порождает.

    Каждый ПОНЕДЕЛЬНИК в 00:00 следом за суточным уходит ВТОРОЕ сообщение —
    недельный отчёт за пн–вс. Он считается не разницей снимков за 7 дней, а
    суммой уже посчитанных суток (копилка в settings, см. services/daily_report.py):
    первого числа месяца бот обнуляет счётчики, и прямая разница в такую неделю
    соврала бы. Проспал понедельник — отчёт уйдёт при первом запуске, с честной
    подписью «дней: N».

    Сообщение НЕ регистрируется в гигиене панелей (как «Итоги месяца») —
    история расходов должна оставаться в чате, панели её не затирают.
    Спим до полуночи кусками не больше часа: так цикл переживает и спящий
    компьютер, и переход на зимнее/летнее время.
    """
    from telegram.constants import ParseMode
    from config import ADMIN_IDS
    from services import daily_report

    await asyncio.sleep(15)  # даём боту подняться (как у остальных циклов)
    logger.info("🚀 Запущен фоновый цикл суточного отчёта о расходах (00:00 по Киеву, владельцу в личку)")

    try:
        daily_report.start_snapshot_if_needed()
    except Exception as e:
        logger.error("⚠️ Не удалось поставить первый снимок счётчиков: %s", e)

    while True:
        try:
            if daily_report.period_closed():
                text = daily_report.midnight_report()
                if text:
                    for admin_id in ADMIN_IDS:
                        try:
                            await application.bot.send_message(
                                chat_id=admin_id, text=text, parse_mode=ParseMode.HTML
                            )
                        except Exception as e:
                            logger.warning("⚠️ Не удалось отправить суточный отчёт владельцу %s: %s", admin_id, e)
                    logger.info("📊 Суточный отчёт о расходах отправлен")

            # Недельный отчёт — ВТОРЫМ сообщением, сразу после суточного:
            # проверка идёт после него намеренно, чтобы последние закрытые
            # сутки успели попасть в копилку и в отчёт за неделю.
            if daily_report.week_closed():
                weekly = daily_report.weekly_report()
                if weekly:
                    for admin_id in ADMIN_IDS:
                        try:
                            await application.bot.send_message(
                                chat_id=admin_id, text=weekly, parse_mode=ParseMode.HTML
                            )
                        except Exception as e:
                            logger.warning("⚠️ Не удалось отправить недельный отчёт владельцу %s: %s", admin_id, e)
                    logger.info("📅 Недельный отчёт о расходах отправлен")
        except Exception as e:
            logger.error("⚠️ Не удалось подготовить отчёт о расходах: %s", e)

        try:
            delay = min(daily_report.seconds_to_next_midnight(), 3600)
        except Exception:
            delay = 3600
        await asyncio.sleep(delay)


# ───────────────────────────────────────────────
#  Ежечасный добор базы знаний (RAG)
# ───────────────────────────────────────────────

async def rag_catchup_loop(application):
    """
    Раз в час проверяет, полон ли векторный индекс базы знаний, и дотягивает
    недостающие статьи обычной синхронизацией (только новые/изменённые файлы,
    с паузами при лимите Google — см. services/rag.py). Нужен после сбоев
    квоты: раньше неполная база ждала ручного перезапуска или нажатия кнопки.

    Проверка при полной базе бесплатна: ни запросов к Google, ни строк в логе.
    Уведомления админам в личку — по границам инцидента (решение Максима
    2026-07-19): одно «⚠️ база неполная» при первой неудаче добора и одно
    «✅ снова полная» при выздоровлении; между ними — только строки в логе.
    Пока идёт ручная пересборка (защёлка kb_rebuild_running в bot_data, её же
    ставит кнопка панели /rag) — пропускает свой час; на время собственной
    работы ставит ту же защёлку, чтобы панель вежливо просила подождать.
    """
    from config import RAG_ENABLED, ADMIN_IDS
    from services import rag

    if not RAG_ENABLED:
        return  # RAG выключен глобально — цикл не нужен (включение = рестарт бота)

    async def _notify_admins(text: str) -> None:
        # Ошибка отправки одному админу не мешает остальным (как в итогах месяца)
        for admin_id in ADMIN_IDS:
            try:
                await application.bot.send_message(chat_id=admin_id, text=text)
            except Exception as e:
                logger.warning("⚠️ Не удалось отправить уведомление о доборе базы админу %s: %s", admin_id, e)

    notified_broken = False  # об ЭТОМ инциденте в личку уже жаловались?
    while True:
        # Стартовую синхронизацию делает main.py при запуске — первый час пропускаем
        await asyncio.sleep(3600)
        try:
            if application.bot_data.get("kb_rebuild_running"):
                continue  # идёт ручная пересборка — не мешаем, проверим через час
            loop = asyncio.get_running_loop()
            lag = await loop.run_in_executor(None, rag.index_lag)
            if not lag:
                notified_broken = False  # база полная; следующий сбой — новый инцидент
                continue

            logger.info("🚀 Добор базы знаний: индекс неполный (статей без векторов: %d) — дотягиваю", lag)
            application.bot_data["kb_rebuild_running"] = True
            try:
                result = await loop.run_in_executor(None, rag.sync_knowledge_base)
            finally:
                application.bot_data["kb_rebuild_running"] = False
            if result is None:
                continue
            indexed, total = result
            if indexed >= total:
                logger.info("🚀 Добор базы знаний: база снова полная (%d из %d)", indexed, total)
                await _notify_admins(f"✅ База знаний снова полная: {indexed} из {total} статей (автоматический добор).")
                notified_broken = False
            else:
                logger.warning("⚠️ Добор базы знаний: удалось не всё (%d из %d) — следующая попытка через час", indexed, total)
                if not notified_broken:
                    notified_broken = True
                    await _notify_admins(
                        f"⚠️ База знаний неполная: проиндексировано {indexed} из {total} статей — "
                        f"лимит Google пока не пускает.\n"
                        f"Буду пытаться добрать каждый час и напишу, когда база снова станет полной."
                    )
        except Exception as e:
            logger.error("⚠️ Не удалось выполнить цикл добора базы знаний: %s", e)


# ───────────────────────────────────────────────
#  Самообновление: бот сам забирает новый код
# ───────────────────────────────────────────────

async def auto_update_loop(application):
    """
    Раз в AUTO_UPDATE_INTERVAL_SEC (10 минут) спрашивает GitHub, нет ли новой
    версии, и если есть — забирает её и перезапускается. Решение Максима
    2026-07-27.

    ⚠️ БОТ ПРИ ЭТОМ НЕ ЗАМИРАЕТ: сама проверка и скачивание выполняются в
    отдельном потоке (services.deploy.update → run_in_executor), поэтому люди
    в чатах продолжают получать ответы. Единственная пауза — сам перезапуск.

    ⚠️ ЖДЁМ ТИШИНЫ. Перезапуск обрывает разговор: тот, кто ждал ответа, его не
    получит. Поэтому проверка вообще не начинается, пока в чатах не станет тихо
    AUTO_UPDATE_QUIET_SEC секунд (решение Максима: минута). Шумно — молча ждём
    следующего круга. Тишину проверяем ДО обновления, а не после: иначе на
    диске лежал бы новый код, а в памяти работал старый.

    Дома цикл завершается сразу (can_update() = False): там лежат ещё не
    отправленные правки Максима, и скачивание дралось бы с его работой.
    """
    from config import (ADMIN_IDS, AUTO_UPDATE_ENABLED_DEFAULT, AUTO_UPDATE_INTERVAL_SEC,
                        AUTO_UPDATE_QUIET_SEC, AUTO_UPDATE_TICK_SEC)
    from database.history import get_setting
    from services import deploy

    await asyncio.sleep(20)  # даём боту подняться (как у остальных циклов)

    if not deploy.can_update():
        logger.info("⬇️ Самообновление выключено: здесь код с GitHub не забирается")
        return

    logger.info("⬇️ Запущен фоновый цикл самообновления (проверка каждые %d мин, "
                "перезапуск только при тишине %d сек)",
                AUTO_UPDATE_INTERVAL_SEC // 60, AUTO_UPDATE_QUIET_SEC)

    async def _notify(text: str) -> None:
        # Ошибка отправки одному владельцу не мешает остальным (как в итогах месяца)
        for admin_id in ADMIN_IDS:
            try:
                await application.bot.send_message(chat_id=admin_id, text=text)
            except Exception as e:
                logger.warning("⚠️ Не удалось сообщить об обновлении %s: %s", admin_id, e)

    last_check = time.monotonic()
    while True:
        await asyncio.sleep(AUTO_UPDATE_TICK_SEC)
        try:
            if get_setting("auto_update_enabled", AUTO_UPDATE_ENABLED_DEFAULT) != "1":
                continue
            if time.monotonic() - last_check < AUTO_UPDATE_INTERVAL_SEC:
                continue
            if deploy.quiet_for() < AUTO_UPDATE_QUIET_SEC:
                continue  # в чатах разговор — подождём, перезапуск его оборвёт

            last_check = time.monotonic()
            # quiet_nochange: «нового кода нет» в лог не пишем — проверка идёт
            # каждые 10 минут, и эта строка забивала бы лог целиком. Все прочие
            # исходы (обновился, откатился, не достучался) пишутся как обычно.
            res = await deploy.update(quiet_nochange=True)
            status = res.get("STATUS")

            if status == "UPDATED":
                await _notify(f"⬇️ Обновился сам: {res.get('MSG', '')}\nПерезапускаюсь…")
                logger.info("⬇️ Самообновление: перезапуск на %s", res.get("VERSION"))
                # Тот же путь, что у кнопки: помечаем остановку перезапуском
                # (post_stop промолчит) и просим библиотеку остановиться —
                # поднимет systemd.
                application.bot_data["shutdown_reason"] = "restart"
                application.stop_running()
                return
            if status == "ROLLBACK":
                await _notify(f"⚠️ Самообновление: {res.get('MSG', '')}\n"
                              f"Бот продолжает работать на прежней версии.")
            # NOCHANGE и NETFAIL — молча, только в логе (решение Максима:
            # 144 сообщения в сутки «нового нет» никому не нужны).
        except Exception as e:
            logger.error("⚠️ Цикл самообновления споткнулся: %s", e)


# ───────────────────────────────────────────────
#  Суточная очистка архива групповых сообщений
# ───────────────────────────────────────────────

async def cleanup_loop(application):
    """
    Раз в сутки чистит устаревшие данные и проверяет месячный сброс:
      • архив group_messages — старше 10 дней;
      • журнал модерации + улики (moderation_log/mute_evidence) — старше 7 дней;
      • журнал базы знаний — старше KB_LOG_DAYS дней;
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


# ───────────────────────────────────────────────
#  Сторож: «отметки живости» внешнему наблюдателю (2026-07-27)
# ───────────────────────────────────────────────

def _ping_watchdog(url: str) -> None:
    """
    Одна «отметка» внешнему сторожу (healthchecks.io). Синхронная — зовётся
    через run_in_executor, как все сетевые вызовы бота. Идёт через общую
    переиспользуемую сессию (services/http.py), поэтому почти бесплатна.
    Ошибку НЕ глушит: её обязан увидеть вызывающий цикл, иначе он не поймёт,
    что отметки перестали проходить, и не напишет об этом в лог.
    """
    from services.http import session
    response = session().get(url, timeout=10)
    response.raise_for_status()


async def watchdog_loop(application):
    """
    Раз в WATCHDOG_PING_SEC отмечается у внешнего сторожа (healthchecks.io):
    «я работаю». Отметки прекратились — сторож сам напишет владельцу в Telegram.
    Это ЕДИНСТВЕННЫЙ способ узнать о выключенном компьютере или пропавшем
    интернете: сам бот в этот момент сообщить ничего не может.

    ⚠️ ОТМЕТКА ЗНАЧИТ НЕ «ПРОЦЕСС ЗАПУЩЕН», А «БОТ ДОСТАЁТ ДО TELEGRAM»:
    перед каждой отметкой бот спрашивает у Telegram get_me, и если ответа нет —
    отметку НЕ шлёт. Иначе сторож молчал бы в случае «бот жив, но Telegram
    недоступен» — а для людей в чате это выглядит точно так же, как смерть бота.
    Не «упрощать» до безусловной отметки: смысл проверки именно в этом.
    Оговорка честности ради: get_me подтверждает связь с Telegram, но не то,
    что цела очередь апдейтов. Полностью «немого» бота при живой сети такая
    проверка теоретически может не поймать.

    Заодно пишет местную метку живости (WATCHDOG_ALIVE_FILE) — её читает
    watchdog_local.py, чтобы отличить зависшего бота от работающего.

    В лог пишет ТОЛЬКО ГРАНИЦЫ происшествия (как rag_catchup_loop): одна
    строка, когда отметки перестали проходить, и одна, когда снова пошли.
    Иначе при отметке раз в 5 минут лог утонул бы в 288 строках за сутки.

    ⚠️ ЦИКЛ РАБОТАЕТ ВСЕГДА, даже если WATCHDOG_URL не задан — не «оптимизировать»
    выходом в начале. Без URL пропускается ТОЛЬКО отметка наружу, а метка живости
    пишется по-прежнему: иначе местный сторож у человека без внешнего решил бы,
    что бот мёртв, и слал бы вечную ложную тревогу (наступил на этом при
    написании 2026-07-27).

    Задача «тихая»: любая ошибка гасится, сторож не должен ронять бота.
    """
    from config import WATCHDOG_URL, WATCHDOG_PING_SEC, WATCHDOG_ALIVE_FILE

    if WATCHDOG_URL:
        logger.info("🐕 Сторож включён: отметка внешнему наблюдателю каждые %d сек", WATCHDOG_PING_SEC)
    else:
        logger.info("🐕 Внешний сторож не настроен (WATCHDOG_URL пуст в .env) — "
                    "наружу не отмечаемся, метку живости для местного сторожа пишем")

    healthy = True   # прошлый круг удался? (в лог пишем только границы)

    while True:
        try:
            # 1) Слышит ли нас Telegram. Не слышит — НИЧЕГО не отмечаем:
            #    пусть сторожа сработают, бот сейчас бесполезен для людей.
            await application.bot.get_me()

            # 2) Отметка внешнему сторожу (если настроен).
            if WATCHDOG_URL:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _ping_watchdog, WATCHDOG_URL)

            # 3) Местная метка живости — её читает watchdog_local.py.
            #    Пишем ПОСЛЕ удачной проверки Telegram: файл означает не
            #    «процесс запущен», а «бот в строю».
            with open(WATCHDOG_ALIVE_FILE, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))

            if not healthy:
                logger.info("🐕 Сторож: связь восстановлена, отметки снова проходят")
                healthy = True

        except Exception as e:
            if healthy:
                logger.warning("🐕 Сторож: отметка НЕ прошла (%s) — если не восстановится, "
                               "владельцу придёт тревога", e)
                healthy = False

        await asyncio.sleep(WATCHDOG_PING_SEC)


# ───────────────────────────────────────────────
#  🚨 Воздушная тревога в Днепре (2026-07-30)
# ───────────────────────────────────────────────

def _airalert_now_kyiv() -> str:
    """Текущее время по Киеву «14:32» — им подписаны оба сообщения."""
    from datetime import datetime
    from services.daily_report import _kyiv_tz
    return datetime.now(_kyiv_tz()).strftime("%H:%M")


async def air_alert_loop(application):
    """
    Раз в `airalert.poll_interval()` спрашивает у сервиса тревог, объявлена ли
    тревога в Днепре или по Днепропетровской области, и на КАЖДОМ ПЕРЕЛОМЕ
    пишет ВЛАДЕЛЬЦУ в личку: «🚨 тревога» и «✅ отбой» с длительностью.
    Рассылка только в ADMIN_IDS — это личное уведомление, подписки нет
    (решение Максима 2026-07-30).

    ⚠️ СОСТОЯНИЕ ЖИВЁТ В БАЗЕ, а не в памяти (settings: airalert_active +
    airalert_since). Иначе каждый перезапуск бота во время тревоги слал бы
    «🚨 тревога» заново, а после перезапуска на отбое отбой пропал бы вовсе.
    Перезапуски у нас частые — есть и кнопка, и самообновление раз в 10 минут.

    ⚠️ «СОСТОЯНИЕ НЕИЗВЕСТНО» — ОТДЕЛЬНЫЙ, ТРЕТИЙ СЛУЧАЙ (ключа в settings нет).
    В него бот попадает при первом включении тумблера, и из него первый же
    удачный опрос выходит МОЛЧА, если тревоги нет, и сообщением «уже идёт»,
    если тревога есть. Так включённый тумблер сразу говорит правду, но не
    поднимает шум на пустом месте.

    ⚠️ «НЕ СМОГ УЗНАТЬ» НИКОГДА НЕ СЧИТАЕТСЯ ОТБОЕМ (services/airalert.check
    возвращает на это None). Молчащий сервис опаснее ложной тревоги: тишина
    в чате читается как «всё спокойно». Затянулось дольше AIRALERT_SILENCE_SEC —
    владельцу уходит предупреждение, что бот ослеп, и потом одно о выздоровлении;
    между ними только строки в логе (границы происшествия, как у сторожа).

    Тумблер выключен — опроса нет вовсе (не тратим лимит запросов), а состояние
    забывается, чтобы следующее включение снова доложило обстановку.
    Задача «тихая»: любая ошибка гасится, тревога не должна ронять бота.
    """
    import time as _time
    from telegram.constants import ParseMode
    from config import ADMIN_IDS, AIRALERT_ENABLED_DEFAULT, AIRALERT_SILENCE_SEC
    from database.history import delete_setting, get_setting, set_setting
    from services import airalert

    await asyncio.sleep(15)  # даём боту подняться (как у остальных циклов)

    poll_sec = airalert.poll_interval()
    logger.info("🚨 Запущено слежение за воздушной тревогой · источник: %s · опрос раз в %d сек",
                airalert.source_name(), poll_sec)
    if not airalert.precise():
        # Это должно быть видно в логе с первой строки: на запасном источнике
        # тревога по одной громаде до владельца не дойдёт, и «бот молчал»
        # объясняется именно этим, а не поломкой.
        logger.warning("🚨 Работаем на ЗАПАСНОМ источнике без ключа: видны только тревоги "
                       "по области целиком. Появится ALERTS_TOKEN в .env — переключимся сами")

    async def _tell_owner(text: str) -> None:
        # Сообщение НЕ регистрируется в гигиене панелей — тревоги должны
        # остаться в чате историей, а не затираться следующей панелью.
        for admin_id in ADMIN_IDS:
            try:
                await application.bot.send_message(
                    chat_id=admin_id, text=text, parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.warning("🚨 Не удалось отправить сообщение о тревоге владельцу %s: %s", admin_id, e)

    blind_since = 0.0      # когда сервис замолчал (0 = отвечает)
    blind_told = False     # владельцу о слепоте уже сказали?

    while True:
        try:
            enabled = get_setting("airalert_enabled", AIRALERT_ENABLED_DEFAULT) == "1"

            if not enabled:
                # Забываем состояние: следующее включение должно доложить
                # обстановку заново, а не молчать, если тревогу объявили,
                # пока слежение было выключено.
                if get_setting("airalert_active", "") != "":
                    delete_setting("airalert_active")
                    delete_setting("airalert_since")
                blind_since, blind_told = 0.0, False
                await asyncio.sleep(poll_sec)
                continue

            try:
                result = airalert.check()
            except airalert.RateLimited as e:
                # Просят сбавить темп — слушаемся, иначе заблокируют токен.
                logger.warning("🚨 %s — пауза 5 минут", e)
                await asyncio.sleep(300)
                continue

            # ── Сервис не ответил ────────────────────────────────────────
            if result is None:
                now = _time.monotonic()
                if not blind_since:
                    blind_since = now
                elif not blind_told and now - blind_since >= AIRALERT_SILENCE_SEC:
                    # Причину показываем сразу: «сервис лёг» пройдёт само, а
                    # «не принял токен» не пройдёт никогда и лечится правкой
                    # .env — без этой строки две разные беды выглядят одинаково.
                    why = airalert.last_error()
                    await _tell_owner(
                        "⚠️ <b>Тревоги: нет связи с сервисом</b>\n"
                        "───────────────────────────\n"
                        f"alerts.in.ua не отвечает уже {airalert.human_duration(now - blind_since)}.\n"
                        + (f"<i>Причина: {html.escape(why[:200])}</i>\n" if why else "")
                        + "<i>Пока молчу — тишина здесь НЕ означает, что тревоги нет.</i>"
                    )
                    blind_told = True
                    logger.error("🚨 Сервис тревог молчит дольше %d сек — владелец предупреждён",
                                 AIRALERT_SILENCE_SEC)
                await asyncio.sleep(poll_sec)
                continue

            if blind_since:
                if blind_told:
                    await _tell_owner(
                        "✅ <b>Тревоги: связь с сервисом восстановлена</b>\n"
                        "───────────────────────────\n"
                        "<i>Слежу дальше.</i>"
                    )
                logger.info("🚨 Сервис тревог снова отвечает")
                blind_since, blind_told = 0.0, False

            # ── Сравниваем с тем, что знали ──────────────────────────────
            active, places = result
            known = get_setting("airalert_active", "")     # "" = состояние неизвестно
            was_active = (known == "1")
            first_look = (known == "")

            if active and not was_active:
                started = int(_time.time())
                set_setting("airalert_active", "1")
                set_setting("airalert_since", str(started))
                where = ", ".join(places[:4]) if places else "Днепропетровская область"
                head = "🚨 <b>ВОЗДУШНАЯ ТРЕВОГА</b>"
                if first_look:
                    # Тумблер включили посреди тревоги (или бот не работал,
                    # когда её объявили) — честно говорим, что она уже идёт.
                    head += " <i>(уже идёт)</i>"
                await _tell_owner(
                    f"{head}\n"
                    "───────────────────────────\n"
                    # Название места приходит от чужого сервиса — экранируем,
                    # иначе «<» в нём сломает отправку (правило раздела 3 карты).
                    f"🕒 {_airalert_now_kyiv()} · {html.escape(where)}"
                )
                logger.info("🚨 Объявлена воздушная тревога: %s", where)

            elif not active and was_active:
                try:
                    started = int(get_setting("airalert_since", "0") or 0)
                except ValueError:
                    started = 0
                lasted = airalert.human_duration(int(_time.time()) - started) if started else "неизвестно сколько"
                set_setting("airalert_active", "0")
                delete_setting("airalert_since")
                await _tell_owner(
                    "✅ <b>ОТБОЙ ВОЗДУШНОЙ ТРЕВОГИ</b>\n"
                    "───────────────────────────\n"
                    f"🕒 {_airalert_now_kyiv()} · длилась {lasted}"
                )
                logger.info("🚨 Отбой воздушной тревоги (длилась %s)", lasted)

            elif first_look:
                # Первый взгляд, тревоги нет — просто запоминаем, молча.
                set_setting("airalert_active", "0")

        except Exception as e:
            logger.error("🚨 Сбой в слежении за тревогой: %s", e)

        await asyncio.sleep(poll_sec)
