# ───────────────────────────────────────────────
#  jobs/news.py — Новости: опрос сайта, форматирование моделью и рассылка подписчикам
#
#  Выделен из jobs.py 2026-08-04 разрезом БЕЗ изменения логики
#  (файл был на 837 строк и держал шесть независимых циклов).
#  Цикл живёт сам по себе: другие фоновые задачи его не зовут и он не зовёт их.
# ───────────────────────────────────────────────

import logging
from database.history import get_subscribed_chats
from database.history import is_news_already_sent
from database.history import mark_news_as_sent
from database.history import save_group_message
from services.gemini import format_news_as_colonel
from services.knowledge_store import save_pending_news
from services.scraper import fetch_article
from services.scraper import fetch_latest_news
from telegram import InputMediaPhoto
from utils_format import convert_md
from utils_format import fits_caption
from utils_format import send_formatted
import asyncio

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
    #
    # ⚠️ ПОМЕТКА И ССЫЛКА (2026-08-16, решение Максима). Раньше в архив уходила
    # голая сводка, и в стенограмме она выглядела обычной репликой бота: он не
    # понимал, что это его собственная АВТОМАТИЧЕСКАЯ рассылка с сайта, и не мог
    # дать ссылку, когда о новости спрашивали. Пометка в квадратных скобках —
    # тот же приём, которым помечается машинный разбор медиа (services/proactive.py).
    # На то, что видят люди, это не влияет: в чат ушёл текст выше, это только
    # запись для памяти бота.
    archive_text = f"[новость с сайта, ты разослал её в чат] {text}\n\nСсылка: {url}"
    if chat_id < 0:
        try:
            save_group_message(chat_id, bot.id, bot.username or "", bot.first_name or "",
                               archive_text, False)
        except Exception as e:
            logger.debug("📰 Не удалось записать новость в архив групп %s: %s", chat_id, e)
    else:
        # ⚠️ ЛИЧКА: пишем сводку в личную память человека как сообщение бота
        # (2026-08-20, решение Максима). Раньше сюда не писалось ничего, и
        # знание о своей рассылке держала «вечная» память последней новости —
        # она уходила модели в каждый разговор, пока не придёт следующая
        # новость. Её удалили: теперь новость живёт в окне контекста, как любое
        # другое сообщение, и уезжает оттуда сама.
        # В личном чате chat_id и есть user_id.
        try:
            from database.history import add_bot_message
            add_bot_message(chat_id, chat_id, archive_text)
        except Exception as e:
            logger.debug("📰 Не удалось записать новость в личную память %s: %s", chat_id, e)


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

                # ⚠️ «ВЕЧНОЙ» ПАМЯТИ О ПОСЛЕДНЕЙ НОВОСТИ ЗДЕСЬ БОЛЬШЕ НЕТ
                # (заведена 2026-08-16, удалена 2026-08-20 по решению Максима:
                # «новость должна пропадать в контексте, как другие сообщения»).
                # Теперь сводка ложится в память каждого получателя при отправке
                # — send_news_to_chat выше — и уезжает из окна сама.
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
