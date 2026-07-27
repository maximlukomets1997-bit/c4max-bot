# ───────────────────────────────────────────────
#  utils_format.py — форматирование ответов модели для Telegram
#
#  Заменяет самописный конвертер Markdown→HTML (utils.to_safe_html)
#  для ВСЕХ ответов моделей. Использует telegramify-markdown (v1.1.0+):
#    convert()        — Markdown → (text, entities) со смещениями в UTF-16
#    split_entities() — безопасная нарезка длинного текста по границам entity
#                       (не разрывает разметку посередине)
#
#  Цепочка рассуждений модели в тегах <thought>...</thought> выносится
#  в сворачиваемую цитату (expandable_blockquote). Работает для любой
#  модели, у которой включён вывод <thought> (в т.ч. gemma-4-26b-a4b-it).
#
#  Отправка идёт через entities, БЕЗ parse_mode (они взаимоисключающи).
# ───────────────────────────────────────────────

import re
import logging

from telegram import MessageEntity as TgEntity, LinkPreviewOptions
from telegramify_markdown import convert, split_entities, utf16_len
# Если MessageEntity не экспортируется из корня пакета в твоей версии —
# замени строку ниже на:  from telegramify_markdown.type import MessageEntity
from telegramify_markdown import MessageEntity

logger = logging.getLogger(__name__)

# ── Заголовки без инъекции эмодзи ──
# telegramify по умолчанию подставляет эмодзи перед заголовками (📌 и т.п.).
# Мы это отключаем: заголовки оформляются только нативным стилем telegramify
# (жирный/подчёркнутый), без добавления каких-либо символов от бота.
try:
    from telegramify_markdown.config import get_runtime_config
    _cfg = get_runtime_config()
    _cfg.markdown_symbol.heading_level_1 = ""
    _cfg.markdown_symbol.heading_level_2 = ""
    _cfg.markdown_symbol.heading_level_3 = ""
    _cfg.markdown_symbol.heading_level_4 = ""
except Exception as _cfg_err:  # на случай изменения API конфигурации в будущих версиях
    logger.debug("Не удалось настроить символы заголовков telegramify: %s", _cfg_err)

THOUGHT_RE = re.compile(r"<thought>(.*?)</thought>", re.DOTALL | re.IGNORECASE)
THOUGHT_HEADER = "🧠 Мысли (под капотом)\n"
MAX_UTF16 = 4096

# True  — мысли (сворачиваемая цитата) идут В НАЧАЛЕ ответа (надёжнее при нарезке).
# False — мысли идут в конце ответа.
THOUGHTS_AT_TOP = True


def _extract_thoughts(raw_answer: str):
    """Вырезает все блоки <thought>…</thought>, возвращает (тело, объединённые_мысли)."""
    thoughts = []

    def _grab(match):
        thoughts.append(match.group(1).strip())
        return ""

    body = THOUGHT_RE.sub(_grab, raw_answer).strip()
    thought_text = "\n\n".join(t for t in thoughts if t)
    return body, thought_text


def strip_thoughts(raw_answer: str) -> str:
    """Текст ответа без блоков <thought>…</thought> — для логов и превью (в Telegram мысли остаются)."""
    return THOUGHT_RE.sub("", raw_answer or "").strip()


def build_text_and_entities(raw_answer: str):
    """
    Превращает ответ модели в (text, entities[telegramify]).
    Тело форматируется обычным образом; цепочка рассуждений (если есть)
    добавляется в конец как сворачиваемая цитата expandable_blockquote.

    Смещения считаются в UTF-16 (utf16_len), потому что Telegram измеряет
    offset/length именно так (эмодзи 🧠 — это 2 кодовых юнита).
    """
    body, thought_text = _extract_thoughts(raw_answer)

    body_text, body_entities = convert(body)

    if not thought_text:
        return body_text, body_entities

    th_text, th_entities = convert(thought_text)
    header_units = utf16_len(THOUGHT_HEADER)          # длина шапки вместе с \n
    header_bold_len = utf16_len(THOUGHT_HEADER.rstrip("\n"))
    block_body = THOUGHT_HEADER + th_text             # шапка + текст мыслей
    block_len = utf16_len(block_body)

    quote_entities = []
    # Жирная шапка «🧠 Мысли (под капотом)» (offset проставим относительно начала блока)
    quote_entities.append(MessageEntity(type="bold", offset=0, length=header_bold_len))
    # Текст мыслей — сдвиг за шапку
    for e in th_entities:
        e.offset += header_units
        quote_entities.append(e)
    # Сама сворачиваемая цитата (шапка + мысли)
    quote_entities.append(MessageEntity(type="expandable_blockquote", offset=0, length=block_len))

    entities = []

    if THOUGHTS_AT_TOP:
        # [цитата с мыслями] \n [тело ответа] — без пустой строки между ними
        sep = "\n" if body_text else ""
        for e in quote_entities:            # цитата с offset=0 — сдвигать не нужно
            entities.append(e)
        body_shift = block_len + utf16_len(sep)
        for e in body_entities:
            e.offset += body_shift
            entities.append(e)
        text = block_body + sep + body_text
    else:
        # [тело ответа] \n [цитата с мыслями] — без пустой строки между ними
        sep = "\n" if body_text else ""
        entities.extend(body_entities)
        base = utf16_len(body_text + sep)
        for e in quote_entities:
            e.offset += base
            entities.append(e)
        text = body_text + sep + block_body

    return text, entities


def _to_ptb(entities):
    """telegramify MessageEntity → telegram.MessageEntity (для python-telegram-bot)."""
    out = []
    for e in entities:
        out.append(TgEntity(
            type=e.type,
            offset=e.offset,
            length=e.length,
            url=getattr(e, "url", None),
            language=getattr(e, "language", None),
            custom_emoji_id=getattr(e, "custom_emoji_id", None),
        ))
    return out


async def _send_plain(bot, chat_id, text, reply_to, disable_preview):
    """Аварийная отправка голым текстом, нарезка по 4096 символов."""
    first = True
    for i in range(0, len(text), 4096):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text[i:i + 4096],
                link_preview_options=LinkPreviewOptions(is_disabled=disable_preview),
                reply_to_message_id=reply_to if first else None,
            )
        except Exception as e:
            logger.error("⚠️ Не удалось отправить сообщение без разметки в чат %s: %s", chat_id, e)
        first = False


async def send_formatted(bot, chat_id, raw_answer, reply_to=None, disable_preview=True):
    """
    Единая точка отправки ответов модели (текст / Vision / аудио).
    Форматирует через telegramify-markdown, безопасно нарезает длинный текст
    по границам entity и отправляет с entities (без parse_mode).
    При любой ошибке — мягкий фолбэк на голый текст.

    :param reply_to: message_id, на который отвечаем (только в первом чанке).
    """
    try:
        text, entities = build_text_and_entities(raw_answer)
        chunks = list(split_entities(text, entities, MAX_UTF16))
    except Exception as e:
        logger.warning("⚠️ Не удалось отформатировать ответ: %s — отправляю без разметки", e)
        plain = THOUGHT_RE.sub("", raw_answer).strip() or raw_answer
        await _send_plain(bot, chat_id, plain, reply_to, disable_preview)
        return

    first = True
    for chunk_text, chunk_entities in chunks:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk_text,
                entities=_to_ptb(chunk_entities),
                link_preview_options=LinkPreviewOptions(is_disabled=disable_preview),
                reply_to_message_id=reply_to if first else None,
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось отправить с разметкой: %s — отправляю без неё", e)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=chunk_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=disable_preview),
                    reply_to_message_id=reply_to if first else None,
                )
            except Exception as e2:
                logger.error("⚠️ Не удалось отправить сообщение в чат %s: %s", chat_id, e2)
        first = False


# ───────────────────────────────────────────────
#  Помощники для прочих сообщений (новости, команды) — БЕЗ логики <thought>
# ───────────────────────────────────────────────

def convert_md(md: str):
    """
    Markdown → (plain_text, list[telegram.MessageEntity]).
    Простая конвертация без обработки <thought>. Используется для подписей к фото
    и коротких сообщений, где нужны готовые entity (caption_entities/entities).
    """
    text, ents = convert(md)
    return text, _to_ptb(ents)


def fits_caption(text: str, limit: int = 1024) -> bool:
    """True, если текст помещается в подпись к фото (лимит Telegram ≈1024 UTF-16 юнита)."""
    return utf16_len(text) <= limit


async def reply_md(bot, chat_id, md, disable_preview=True, **kwargs):
    """
    Отправляет ОДНО короткое сообщение из Markdown через telegramify и возвращает
    объект отправленного сообщения (нужно, например, для register_and_clean_bot_message).
    При ошибке форматирования — мягкий фолбэк на голый текст.
    """
    try:
        text, entities = convert_md(md)
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            entities=entities,
            link_preview_options=LinkPreviewOptions(is_disabled=disable_preview),
            **kwargs,
        )
    except Exception as e:
        logger.warning("⚠️ Не удалось отформатировать сообщение: %s — отправляю без разметки", e)
        plain = md.replace("**", "").replace("__", "").replace("`", "")
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=plain,
                link_preview_options=LinkPreviewOptions(is_disabled=disable_preview),
                **kwargs,
            )
        except Exception as e2:
            logger.error("⚠️ Не удалось отправить сообщение в чат %s: %s", chat_id, e2)
            return None
