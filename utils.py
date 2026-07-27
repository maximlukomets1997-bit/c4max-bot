# ───────────────────────────────────────────────
#  utils.py — утилиты бота
#
#  Содержит:
#    should_respond_in_group()        — логика ответа в группах
#    clean_mention()                  — удаление @упоминания бота из текста
#    keep_chat_action()               — непрерывный статус «печатает…» на время работы
#    register_and_clean_bot_message() — авто-удаление старых сообщений бота
#    delete_user_message_safe()       — тихое удаление сообщения пользователя
#    schedule_delete()                — отложенное удаление сообщения
#    mention()                        — текстовое обращение к пользователю
# ───────────────────────────────────────────────

import asyncio
import logging
from contextlib import asynccontextmanager
from telegram import Update
from database.history import (
    register_bot_message,
    get_old_bot_messages,
    remove_bot_message,
)

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────
#  Группы: логика ответа
# ───────────────────────────────────────────────

def should_respond_in_group(update: Update, bot_username: str) -> bool:
    """
    В группе бот отвечает только если:
      1. Упоминают @имя_бота в тексте или в подписи к медиа (фото и т.п.)
      2. Отвечают (Reply) на сообщение бота
    """
    message = update.message
    if message is None:
        return False

    # Reply на сообщение бота
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.username == bot_username:
            return True

    # Упоминание через entities. У текстовых сообщений разметка лежит в
    # entities, у фото и других медиа с подписью — в caption_entities;
    # проверяем оба поля (текст и подпись взаимоисключающи, поэтому
    # смещения всегда относятся к строке text ниже).
    text = message.text or message.caption or ""
    all_entities = list(message.entities or ()) + list(message.caption_entities or ())
    for entity in all_entities:
        if entity.type == "mention":
            mention = text[entity.offset: entity.offset + entity.length]
            if mention.lower() == f"@{bot_username}".lower():
                return True

    return False


def clean_mention(text: str, bot_username: str) -> str:
    """Убирает @упоминание бота из текста перед отправкой в модель."""
    return text.replace(f"@{bot_username}", "").strip()


# ───────────────────────────────────────────────
#  Статус чата («печатает…» / «отправляет фото…»)
# ───────────────────────────────────────────────

@asynccontextmanager
async def keep_chat_action(bot, chat_id: int, action: str = "typing"):
    """
    Держит статус чата включённым, пока выполняется тело блока `async with`.

    Telegram гасит статус сам через ~5 секунд после каждого сигнала, поэтому
    фоновая задача шлёт его заново каждые 4.5 секунды. На выходе из блока
    задача останавливается, а статус гаснет сам (или его перекрывает
    отправленный ответ).

    action: "typing" — «печатает…» (ответы модели на текст/фото/голос),
            "upload_photo" — «отправляет фото…» (генерация картинок /imagine).

    Ошибки отправки сигнала глушатся: статус — украшение, он не должен
    мешать подготовке ответа.
    """
    stop = asyncio.Event()

    async def _refresh_loop():
        while not stop.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action=action)
            except Exception:
                pass  # сеть мигнула / нет прав — некритично
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.5)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_refresh_loop())
    try:
        yield
    finally:
        stop.set()
        task.cancel()


# ───────────────────────────────────────────────
#  Менеджер сообщений бота в группах
# ───────────────────────────────────────────────

async def delete_user_message_safe(message):
    """
    Безопасно удаляет сообщение пользователя (если бот имеет права администратора).
    Ошибки тихо игнорируются, чтобы не сломать бота.
    """
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        pass


def mention(user) -> str:
    """
    Текстовое обращение к пользователю для подстановки в начало сообщений:
    @username, иначе имя, иначе id. Без HTML — безопасно вставлять в любой текст.
    """
    if user is None:
        return ""
    if getattr(user, "username", None):
        return f"@{user.username}"
    return getattr(user, "first_name", None) or str(getattr(user, "id", ""))


async def _delete_after(bot, chat_id: int, message_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        # Сообщение уже удалено / нет прав / чат пропал — молча игнорируем.
        pass


def schedule_delete(bot, chat_id: int, message_id: int, delay: int = 30):
    """
    Запланировать удаление сообщения через `delay` секунд (fire-and-forget).
    Реализовано на asyncio, без JobQueue: задача живёт в памяти процесса,
    при рестарте бота незавершённые удаления теряются — для коротких
    служебных сообщений это приемлемо.
    """
    try:
        asyncio.create_task(_delete_after(bot, chat_id, message_id, delay))
    except RuntimeError:
        # Нет запущенного event loop (например, вне хендлеров/в тестах) — пропускаем.
        pass


async def register_and_clean_bot_message(bot, chat_id: int, message_id: int, keep_count: int = 1):
    """
    Регистрирует сообщение бота в базе данных 
    и удаляет старые сообщения, оставляя только последние `keep_count`.
    """

    try:
        register_bot_message(chat_id, message_id)

        old_ids = get_old_bot_messages(chat_id, keep_count=keep_count)
        for old_id in old_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_id)
                # logger.info("Удалено старое сообщение бота: chat_id=%s, message_id=%s", chat_id, old_id)  # скрыто по просьбе
            except Exception as e:
                error_msg = str(e)
                if "Message to delete not found" not in error_msg:
                    logger.warning("⚠️ Не удалось удалить сообщение %s в чате %s: %s", old_id, chat_id, error_msg)
            finally:
                remove_bot_message(chat_id, old_id)
    except Exception as e:
        logger.error("⚠️ Не удалось очистить старые сообщения бота в чате %s: %s", chat_id, e)
