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
#  Пользоваться можно и вовсе не набирая названий: КАТАЛОГ (2026-08-04) —
#  класс техники → список кнопок → карточка. Всё в одном сообщении.
#
#  ⚠️ Гигиена панелей применяется ТОЛЬКО В ЛИЧКЕ (`_send_ttx_message`):
#  там карточка ведёт себя как остальные экраны бота и убирается следующей
#  командой (просьба Максима — раньше она висела в переписке вечно).
#  В ГРУППЕ регистрации нет намеренно: гигиена удаляет предыдущее сообщение
#  бота, то есть снесла бы карточку другого человека посреди обсуждения или
#  свежую новость. Кнопки правят ТО ЖЕ сообщение, поэтому одна команда =
#  одно сообщение в чате.
# ─────────────────────────────────────────────

import asyncio
import html
import logging
import time

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      InlineQueryResultArticle, InputTextMessageContent,
                      LinkPreviewOptions)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from services import tech_card
from services.knowledge_store import ARTICLE_KINDS
from utils import (delete_user_message_safe, register_and_clean_bot_message,
                   schedule_delete)

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
    # Возврат в КАТАЛОГ — сразу в список своего класса, а не к выбору класса:
    # человек пришёл оттуда и, скорее всего, хочет посмотреть соседнюю машину.
    rows.append([InlineKeyboardButton(
        "⬅️ К списку", callback_data=f"ttx:k:{article['kind']}:0")])
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
        # Без названия открываем КАТАЛОГ: человек выбирает класс и жмёт по
        # названию. Прежняя текстовая подсказка «напиши /ttx ариете» ушла —
        # её суть переехала в подвал каталога.
        await _send_catalog(context.bot, chat.id)
        return

    if not _cooled_down(user.id):
        return  # молча: сообщение с командой уже удалено, отвечать нечем

    article, candidates = tech_card.find_local(query)
    if not article and not candidates:
        article = await _semantic_lookup(query)

    if not article:
        sent = await _send_ttx_message(
            context.bot, chat.id, tech_card.render_candidates(query, candidates),
            _candidates_keyboard(candidates) if candidates else None,
        )
        logger.info("%s Справочник: «%s» — не найдено (подсказок: %d)",
                    _ICON, query, len(candidates))
        if sent and not candidates:
            schedule_delete(context.bot, chat.id, sent.message_id, _HINT_TTL)
        return

    await _send_card(context.bot, chat.id, article)
    logger.info("%s Справочник: «%s» → %s", _ICON, query, article["title"])


# ─── 📚 КАТАЛОГ: техника кнопками, без набора названия ──────────────
#
#  Просьба Максима 2026-08-04: обычный человек не должен помнить, как
#  пишется «Ariete PSO», и уж тем более набирать «/ttx ариете». Он жмёт
#  «📊 ТТХ техники» и дальше только тыкает: класс → название → карточка.
#
#  Развилка по классам ОБЯЗАТЕЛЬНА: статей 80, и списком они не влезают
#  ни в лимит Telegram (~100 кнопок), ни в экран телефона.

# Сколько названий на одной странице списка. Два столбца по десять рядов:
# больше на экран телефона не помещается, а листание дешёвое.
_CATALOG_PAGE = 20


def catalog_text(bot) -> str:
    """Текст экрана выбора класса техники."""
    total = len(tech_card.index())
    return (
        f"{_ICON} <b>СПРАВОЧНИК ТЕХНИКИ</b>\n"
        f"{tech_card.SEPARATOR}\n"
        f"В базе <b>{total}</b> статей. Выбери класс — дальше просто жми "
        f"по названию.\n"
        f"{tech_card.SEPARATOR}\n"
        f"<i>Знаешь название — можно и напрямую: <code>/ttx ариете</code>. "
        f"Понимаю игровые прозвища, латиницу и кириллицу.\n"
        f"А ещё меня можно звать в ЛЮБОМ чате: наберите "
        f"<code>@{html.escape(bot.username or '')} ариете</code>.</i>"
    )


def catalog_keyboard() -> InlineKeyboardMarkup:
    """
    Кнопки классов техники — по два в ряд, с числом статей, и ВЫХОД в главное
    меню последним рядом.

    Выход обязателен: каталог — верхний экран справочника, и без него человек,
    зашедший сюда с /start, упирался в тупик (просьба Максима 2026-08-04).
    Цепочка возвратов теперь замкнута целиком: карточка → список своего класса
    → классы → главное меню. Ведёт он на `menu:back` — тот же callback, что у
    остальных возвратов в меню; своего заводить нельзя, иначе экранов «главное
    меню» станет два и они разъедутся.
    """
    buttons = [
        InlineKeyboardButton(f"{icon} {name.capitalize()} ({count})",
                             callback_data=f"ttx:k:{kind}:0")
        for kind, icon, name, count in tech_card.kinds_summary()
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)


def _kind_screen(kind: str, page: int):
    """
    Экран списка одного класса: (текст, клавиатура).

    Номер страницы подрезается по ФАКТИЧЕСКОМУ числу статей — статью могли
    удалить, пока список висел в чате (та же защита, что в панели /rag).
    """
    items = tech_card.by_kind(kind)
    meta = ARTICLE_KINDS.get(kind, ARTICLE_KINDS["other"])
    pages = max(1, (len(items) + _CATALOG_PAGE - 1) // _CATALOG_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * _CATALOG_PAGE:(page + 1) * _CATALOG_PAGE]

    first = page * _CATALOG_PAGE + 1
    last = page * _CATALOG_PAGE + len(chunk)
    text = (
        f"{meta['icon']} <b>{meta['name'].upper()}</b>\n"
        f"{tech_card.SEPARATOR}\n"
        f"Статьи {first}–{last} из {len(items)}"
        + (f" · страница {page + 1} из {pages}" if pages > 1 else "")
        + f"\n{tech_card.SEPARATOR}\n"
        f"<i>Жми по названию — покажу ТТХ.</i>"
    )

    buttons = [
        InlineKeyboardButton(tech_card.short_title(a["title"]),
                             callback_data=f"ttx:{tech_card.token(a['fname'])}:card")
        for a in chunk
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]

    # Ряд листания — только когда листать есть что. На краю списка стрелка
    # становится «▫️» с пустышкой, чтобы ряд не прыгал по ширине (как в /rag).
    if pages > 1:
        prev_btn = (InlineKeyboardButton("⬅️", callback_data=f"ttx:k:{kind}:{page - 1}")
                    if page > 0 else InlineKeyboardButton("▫️", callback_data="ttx:noop"))
        next_btn = (InlineKeyboardButton("➡️", callback_data=f"ttx:k:{kind}:{page + 1}")
                    if page < pages - 1 else InlineKeyboardButton("▫️", callback_data="ttx:noop"))
        rows.append([
            prev_btn,
            InlineKeyboardButton(f"📄 {page + 1} из {pages}", callback_data="ttx:noop"),
            next_btn,
        ])

    rows.append([InlineKeyboardButton("⬅️ К классам", callback_data="ttx:cat")])
    return text, InlineKeyboardMarkup(rows)


async def _send_catalog(bot, chat_id: int):
    """Отправляет каталог новым сообщением (команда /ttx без названия)."""
    await _send_ttx_message(bot, chat_id, catalog_text(bot), catalog_keyboard())


async def _send_card(bot, chat_id: int, article: dict):
    """Отправляет карточку техники новым сообщением."""
    data = tech_card.load(article)
    await _send_ttx_message(bot, chat_id, tech_card.render_card(article, data),
                            _card_keyboard(article, data))


async def _send_ttx_message(bot, chat_id: int, text: str, markup):
    """
    Общая отправка экранов справочника.

    ⚠️ В ЛИЧКЕ сообщение регистрируется в гигиене панелей (просьба Максима
    2026-08-04): карточка ТТХ висела в переписке вечно и не убиралась
    следующей командой, в отличие от всех остальных экранов бота.
    ⚠️ В ГРУППЕ — НЕ регистрируется, и это не забывчивость: гигиена удаляет
    ПРЕДЫДУЩЕЕ сообщение бота в чате, то есть снесла бы карточку другого
    человека посреди обсуждения или свежую новость. Признак лички — знак
    chat_id (у групп он отрицательный), как в рассылке новостей.
    """
    sent = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        reply_markup=markup, link_preview_options=_NO_PREVIEW,
    )
    if sent and chat_id > 0:
        await register_and_clean_bot_message(bot, chat_id, sent.message_id)
    return sent


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

    # ── Каталог: экраны выбора, а не статья ──────────────────────────
    # Разбираются ДО поиска статьи по ключу: «cat», «k» и «noop» ключами
    # быть не могут (ключ — 10 знаков md5), но искать по ним статью незачем.
    if tok == "noop":
        await query.answer()
        return

    if tok == "cat":
        await query.answer()
        await _edit_ttx_screen(query, catalog_text(query.get_bot()), catalog_keyboard())
        return

    if tok == "k":
        kind = parts[2] if len(parts) > 2 else ""
        try:
            page = int(parts[3]) if len(parts) > 3 else 0
        except ValueError:
            page = 0
        await query.answer()
        text, markup = _kind_screen(kind, page)
        await _edit_ttx_screen(query, text, markup)
        return

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
    await _edit_ttx_screen(query, text, markup)


async def _edit_ttx_screen(query, text: str, markup) -> None:
    """
    Перерисовывает ТО ЖЕ сообщение новым экраном справочника.

    Все экраны — каталог, список класса, карточка и раздел — живут в ОДНОМ
    сообщении: одна команда = одно сообщение в чате, и человек не листает
    вверх мимо десяти своих же нажатий.
    """
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                      link_preview_options=_NO_PREVIEW)
    except Exception as e:
        # Самая частая причина — нажали кнопку экрана, который уже открыт:
        # Telegram отвечает «Message is not modified». Ругаться незачем.
        logger.debug("%s Не удалось перерисовать экран справочника: %s", _ICON, e)


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
