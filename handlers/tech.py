# ─────────────────────────────────────────────
#  handlers/tech.py — 📊 команда /ttx: справочник техники из базы знаний
#  (2026-08-04)
#
#  Команда ПУБЛИЧНАЯ и работает и в личке, и в группах. Ответ собирается
#  ИЗ ФАЙЛА статьи (services/tech_card.py) — без нейросети, без токенов и
#  без ожидания: то, что раньше приходилось спрашивать у модели, лежит
#  готовым в knowledge/approved.
#
#  Три ступени поиска, от бесплатной к платной:
#    1) название, имя файла и игровые прозвища — чтение файлов, ноль сети;
#    2) семантический поиск базы знаний (`rag.test_search`) — ОДИН дешёвый
#       эмбеддинг запроса, и только если первая ступень не дала ответа;
#    3) кнопки «возможно, ты имел в виду».
#
#  ⚠️ Ответ НЕ регистрируется в гигиене панелей (`register_and_clean_bot_message`)
#  намеренно: в группе она удалила бы предыдущее сообщение бота — карточку
#  другого человека посреди обсуждения или новость. Кнопки разделов правят
#  ТО ЖЕ САМОЕ сообщение, поэтому одна команда = одно сообщение в чате.
# ─────────────────────────────────────────────

import asyncio
import html
import logging
import os
import time

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      InlineQueryResultArticle, InputTextMessageContent,
                      LinkPreviewOptions)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from services import tech_card
from utils import delete_user_message_safe, schedule_delete

logger = logging.getLogger(__name__)

_ICON = "📊"

# Через сколько исчезает файл статьи, отданный кнопкой «Вся статья файлом».
# Как у файлов промптов: скачивают и открывают сразу, а в чате они копятся.
_FILE_TTL = 600

# Через сколько исчезает подсказка «как пользоваться» и ответ «не нашёл»:
# это служебные сообщения, в группе им висеть незачем.
_HINT_TTL = 60

# Не чаще одного ответа в 10 секунд на человека. Команда публичная, а карточка
# — большое сообщение: без этого залп /ttx забил бы группу. Счётчик в памяти
# процесса (как у антиспама) — переживать перезапуск ему незачем.
_COOLDOWN_SEC = 10
_last_call: dict[int, float] = {}

# Превью ссылок выключено во ВСЕХ сообщениях справочника (решение Максима
# 2026-08-04). Раздел «🎬 Видеообзор» содержит ссылки на YouTube и TikTok, и
# Telegram разворачивал первую из них в обложку ролика во весь экран: карточка
# переставала быть карточкой, а кнопки разделов уезжали за нижний край.
# ⚠️ Ставить ОДИНАКОВО на всех путях — карточка, раздел и инлайн-ответ: иначе
# превью будет то появляться, то исчезать при переходе между разделами.
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _cooled_down(user_id: int) -> bool:
    """True — можно отвечать; иначе слишком часто."""
    now = time.monotonic()
    if now - _last_call.get(user_id, 0.0) < _COOLDOWN_SEC:
        return False
    _last_call[user_id] = now
    return True


# ─── сборка клавиатуры ──────────────────────────────────────────────

def _card_keyboard(article: dict, data: dict) -> InlineKeyboardMarkup:
    """
    Кнопки разделов статьи по два в ряд + «вся статья файлом».

    Раздел с ТТХ своей кнопки не получает: он и так показан на карточке.
    Кнопки собираются ПО ФАКТИЧЕСКОМУ составу статьи — у корабля не будет
    «Куда пробивать», если этого раздела в статье нет.
    """
    tok = tech_card.token(article["fname"])
    buttons = [
        InlineKeyboardButton(tech_card.section_label(title), callback_data=f"ttx:{tok}:{i}")
        for i, (title, _) in enumerate(data["sections"]) if not tech_card.is_specs(title)
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("📄 Вся статья файлом", callback_data=f"ttx:{tok}:file")])
    return InlineKeyboardMarkup(rows)


def _section_keyboard(article: dict) -> InlineKeyboardMarkup:
    tok = tech_card.token(article["fname"])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ К карточке", callback_data=f"ttx:{tok}:card"),
        InlineKeyboardButton("📄 Файлом", callback_data=f"ttx:{tok}:file"),
    ]])


def _candidates_keyboard(candidates: list) -> InlineKeyboardMarkup:
    """Кнопки «возможно, ты имел в виду» — по одной на статью."""
    rows = []
    for art in candidates:
        icon = tech_card.kind_icon(art["kind"])
        rows.append([InlineKeyboardButton(
            f"{icon} {art['title'][:40]}",
            callback_data=f"ttx:{tech_card.token(art['fname'])}:card",
        )])
    return InlineKeyboardMarkup(rows)


# ─── семантическая ступень ──────────────────────────────────────────

async def _semantic_lookup(query: str):
    """
    Вторая ступень: тот же гибридный поиск, что подмешивает статьи модели.
    Берём лучший результат, ПРОШЕДШИЙ отбор по настройкам панели /rag, —
    порог и «запас над фоном» откалиброваны владельцем, второй калибровки
    здесь заводить нельзя.

    Ходит в сеть (эмбеддинг запроса), поэтому только через отдельный поток.
    """
    try:
        from services import rag
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, rag.test_search, query)
    except Exception as e:
        logger.debug("%s Семантический поиск справочника не удался: %s", _ICON, e)
        return None
    for row in result.get("results", []):
        if row.get("passes"):
            art = tech_card.by_title(row["title"])
            if art:
                logger.info("%s Справочник: «%s» найдено смыслом → %s",
                            _ICON, query, art["title"])
                return art
    return None


# ─── команда /ttx ───────────────────────────────────────────────────

async def cmd_ttx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /ttx <название> — карточка техники из базы знаний.
    Работает у всех и везде; ответ идёт из файла статьи, без нейросети.
    """
    await delete_user_message_safe(update.message)
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    query = " ".join(context.args).strip() if context.args else ""

    if not query:
        await _send_hint(context.bot, chat.id)
        return

    if not _cooled_down(user.id):
        return  # молча: сообщение с командой уже удалено, отвечать нечем

    article, candidates = tech_card.find_local(query)
    if not article and not candidates:
        article = await _semantic_lookup(query)

    if not article:
        sent = await context.bot.send_message(
            chat_id=chat.id,
            text=tech_card.render_candidates(query, candidates),
            parse_mode=ParseMode.HTML,
            reply_markup=_candidates_keyboard(candidates) if candidates else None,
        )
        logger.info("%s Справочник: «%s» — не найдено (подсказок: %d)",
                    _ICON, query, len(candidates))
        if sent and not candidates:
            schedule_delete(context.bot, chat.id, sent.message_id, _HINT_TTL)
        return

    await _send_card(context.bot, chat.id, article)
    logger.info("%s Справочник: «%s» → %s", _ICON, query, article["title"])


def ttx_hint_text(bot) -> str:
    """
    Текст подсказки «как пользоваться справочником».

    Вынесен из _send_hint 2026-08-04: тот же текст показывает кнопка
    «📊 ТТХ техники» главного экрана /start, а второй копии этих строк быть
    не должно — команда и кнопка обязаны рассказывать одно и то же.

    ⚠️ Ник бота берём из `bot.username` (он заполнен с первой минуты работы),
    а НЕ через `await bot.get_me()`, как было раньше: это сетевой запрос, а
    текст теперь собирается ещё и на нажатие кнопки.
    """
    total = len(tech_card.index())
    return (
        f"{_ICON} <b>СПРАВОЧНИК ТЕХНИКИ</b>\n"
        "───────────────────────────\n"
        "Напиши <code>/ttx</code> и название — отдам ТТХ прямо из базы знаний: "
        "мгновенно и без нейросети.\n\n"
        "<b>Примеры:</b>\n"
        "<code>/ttx ариете</code>\n"
        "<code>/ttx Leopard 2A5</code>\n"
        "<code>/ttx т80уе1</code>\n"
        "───────────────────────────\n"
        f"<i>В базе статей: {total}. Понимаю названия, игровые прозвища, "
        f"латиницу и кириллицу.\n"
        f"Меня можно звать и в любом другом чате: наберите "
        f"<code>@{html.escape(bot.username or '')} ариете</code>.</i>"
    )


async def _send_hint(bot, chat_id: int):
    """Подсказка «как пользоваться» — когда /ttx позвали без названия."""
    sent = await bot.send_message(chat_id=chat_id, text=ttx_hint_text(bot),
                                  parse_mode=ParseMode.HTML,
                                  link_preview_options=_NO_PREVIEW)
    if sent:
        schedule_delete(bot, chat_id, sent.message_id, _HINT_TTL)


async def _send_card(bot, chat_id: int, article: dict):
    """Отправляет карточку техники новым сообщением."""
    data = tech_card.load(article)
    await bot.send_message(
        chat_id=chat_id,
        text=tech_card.render_card(article, data),
        parse_mode=ParseMode.HTML,
        reply_markup=_card_keyboard(article, data),
        link_preview_options=_NO_PREVIEW,
    )


# ─── кнопки карточки ────────────────────────────────────────────────

async def handle_ttx_callback(query, context, data: str) -> None:
    """
    Ветки ttx:<ключ>:<что> — разделы статьи, возврат к карточке и файл.

    ⚠️ Зовётся из роутера ДО гейта прав: это кнопки для ВСЕХ, как quiz_start.
    Ключ статьи считается из имени файла (см. tech_card.token), поэтому
    кнопки под старой карточкой работают и после перезапуска бота.
    """
    parts = data.split(":")
    tok = parts[1] if len(parts) > 1 else ""
    what = parts[2] if len(parts) > 2 else "card"

    article = tech_card.by_token(tok)
    if not article:
        await query.answer("Статьи больше нет в базе знаний.", show_alert=True)
        return

    if what == "file":
        await _send_article_file(query, article)
        return

    try:
        data_art = tech_card.load(article)
    except Exception as e:
        logger.warning("⚠️ %s Не удалось прочитать статью %s: %s", _ICON, article["fname"], e)
        await query.answer("Не удалось прочитать статью.", show_alert=True)
        return

    if what == "card":
        text, markup = tech_card.render_card(article, data_art), _card_keyboard(article, data_art)
    else:
        try:
            idx = int(what)
            text = tech_card.render_section(article, data_art, idx)
        except (ValueError, IndexError):
            # Статью правили, пока карточка висела в чате, — разделы съехали.
            await query.answer("Раздел не найден — открой карточку заново.", show_alert=True)
            return
        markup = _section_keyboard(article)

    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                      link_preview_options=_NO_PREVIEW)
    except Exception as e:
        # Самая частая причина — нажали кнопку раздела, который уже открыт:
        # Telegram отвечает «Message is not modified». Ругаться незачем.
        logger.debug("%s Не удалось перерисовать карточку: %s", _ICON, e)


async def _send_article_file(query, article: dict) -> None:
    """
    Отдаёт статью .md файлом. Идёт ОТДЕЛЬНЫМ сообщением, а карточка остаётся
    на месте — как сделано с файлами логов: иначе экран, из которого нажали,
    затёрся бы самим файлом.
    """
    await query.answer("⏳ Отправляю файл…")
    try:
        with open(article["path"], "rb") as f:
            blob = f.read()
    except Exception as e:
        logger.warning("⚠️ %s Не удалось прочитать файл статьи %s: %s", _ICON, article["fname"], e)
        return
    bot = query.get_bot()
    chat_id = query.message.chat_id
    sent = await bot.send_document(
        chat_id=chat_id,
        document=blob,
        filename=article["fname"],
        caption=(f"{_ICON} <b>{html.escape(article['title'])}</b> — статья целиком.\n"
                 f"<i>Файл исчезнет через 10 минут.</i>"),
        parse_mode=ParseMode.HTML,
    )
    if sent:
        schedule_delete(bot, chat_id, sent.message_id, _FILE_TTL)


# ─── инлайн-режим: справочник в ЛЮБОМ чате ──────────────────────────

async def inline_ttx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Инлайн-запрос «@бот ариете» — тот же справочник, но в любом чате, даже
    там, где бота нет.

    ⚠️ Работает, только если инлайн-режим включён у @BotFather (/setinline).
    Пока он выключен, Telegram просто не присылает такие запросы — молча,
    как было с событиями о вступлении в группу.

    Кнопок у инлайн-ответов нет намеренно: у сообщения, отправленного через
    инлайн, нет обычного chat_id, и ветки разделов пришлось бы писать
    вторым, почти таким же обработчиком. Полная статья доступна командой.
    """
    inline = update.inline_query
    if inline is None:
        return
    text = (inline.query or "").strip()
    results = []
    for art in tech_card.suggest(text, limit=5):
        try:
            data = tech_card.load(art)
        except Exception:
            continue
        icon = tech_card.kind_icon(art["kind"])
        description = " ".join(data["head"].split())[:120] or "Статья базы знаний C4_Max"
        results.append(InlineQueryResultArticle(
            id=tech_card.token(art["fname"]),
            title=f"{icon} {art['title']}",
            description=description,
            input_message_content=InputTextMessageContent(
                message_text=tech_card.render_card(art, data),
                parse_mode=ParseMode.HTML,
                link_preview_options=_NO_PREVIEW,
            ),
        ))
    try:
        # cache_time: один и тот же запрос Telegram какое-то время не
        # переспрашивает. База знаний меняется редко, а инлайн срабатывает
        # на каждое нажатие клавиши.
        await inline.answer(results, cache_time=60, is_personal=False)
    except Exception as e:
        logger.debug("%s Не удалось ответить на инлайн-запрос: %s", _ICON, e)
