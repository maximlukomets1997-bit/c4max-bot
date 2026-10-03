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
#    restore_pending_deletes()        — подхват отложенных удалений после перезапуска
#    cancel_pending_deletes()         — отмена их таймеров при остановке
#    mention()                        — текстовое обращение к пользователю
# ───────────────────────────────────────────────

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from telegram import Update
from telegram.error import BadRequest, Forbidden
from database.history import (
    register_bot_message,
    get_old_bot_messages,
    remove_bot_message,
    add_pending_delete,
    remove_pending_delete,
    list_pending_deletes,
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


# ───────────────────────────────────────────────
#  Самоудаление сообщений бота
# ───────────────────────────────────────────────
#
#  ⚠️ ТАЙМЕР ПЕРЕЖИВАЕТ ПЕРЕЗАПУСК (03.10.2026, вопрос Максима «почему не
#  удалилось это уведомление»). Раньше отложенное удаление жило только в
#  памяти процесса: «✅ RAG пересобрана заново!» должно было исчезнуть через
#  минуту, но через 29 секунд бота перезапустили кнопкой, и таймер умер
#  вместе со старым процессом. Так было у ВСЕХ самоудалений сразу — итоги,
#  файлы логов, подсказки, ссылка входа, приветствие новичка в группе, — а
#  перезапусков с самообновлением бывает по нескольку в день.
#
#  Теперь у каждого таймера есть заметка в базе (таблица pending_deletes):
#    • schedule_delete ставит её вместе с задачей;
#    • _delete_after снимает её ПОСЛЕ попытки удаления, а не при отмене
#      задачи: остановка бота отменяет задачи, и заметка обязана это пережить;
#    • restore_pending_deletes при запуске (main.post_init) подхватывает
#      оставшиеся: просроченное удаляет сразу, остальное — в свой срок;
#    • cancel_pending_deletes при остановке (main.post_shutdown) отменяет
#      задачи, а не бросает их: брошенные asyncio отмечал в журнале строкой
#      «Task was destroyed but it is pending!».
#  Сбой записи заметки удаления не отменяет: таймер в памяти работает как
#  прежде, теряется только страховка на случай перезапуска.

# Telegram не даёт ботам удалять сообщения старше 48 часов: такие заметки
# после долгого простоя выбрасываются без попытки.
_PENDING_DELETE_MAX_AGE = 48 * 3600

# Таймеры, ждущие своего часа. Ссылки держим сами: цикл событий хранит задачи
# слабо, а остановке бота нужен их список, чтобы отменить.
_DELETE_TASKS: set = set()


async def _delete_after(bot, chat_id: int, message_id: int, delay: float):
    # Отмена (остановка бота) прерывает сон, и заметка остаётся в базе —
    # удаление доделает следующий запуск. Поэтому отмену здесь не ловим.
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (BadRequest, Forbidden):
        # Сообщение уже удалено, старше 48 часов, нет прав, чат пропал —
        # повторять бессмысленно, заметку снимаем.
        pass
    except Exception as e:
        # Сеть, таймаут, флуд-контроль: заметка остаётся, удаление повторит
        # следующий запуск бота.
        logger.debug("🧹 Самоудаление сообщения %s в чате %s не прошло (%s) — "
                     "повторит следующий запуск", message_id, chat_id, e)
        return
    _forget_pending_delete(chat_id, message_id)


def _arm_delete(bot, chat_id: int, message_id: int, delay: float) -> bool:
    """Заводит таймер в памяти. False — нет цикла событий (вне хендлеров, в тестах)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    task = loop.create_task(_delete_after(bot, chat_id, message_id, delay))
    _DELETE_TASKS.add(task)
    task.add_done_callback(_DELETE_TASKS.discard)
    return True


def _forget_pending_delete(chat_id: int, message_id: int) -> None:
    try:
        remove_pending_delete(chat_id, message_id)
    except Exception as e:
        # Не снялась — следующий запуск попробует удалить ещё раз, получит
        # «сообщения нет» и снимет её сам.
        logger.warning("⚠️ Не удалось снять заметку о самоудалении %s в чате %s: %s",
                       message_id, chat_id, e)


def schedule_delete(bot, chat_id: int, message_id: int, delay: float = 30):
    """
    Запланировать удаление сообщения через `delay` секунд (fire-and-forget).
    Таймер живёт в памяти процесса, а на случай перезапуска рядом ложится
    заметка в базе — см. шапку раздела.
    """
    if not _arm_delete(bot, chat_id, message_id, delay):
        # Нет запущенного цикла событий (вне хендлеров, в тестах) — пропускаем.
        return
    # Заметка пишется сразу за таймером, без await между ними: таймер не
    # может сработать и снять её раньше, чем она записана.
    try:
        add_pending_delete(chat_id, message_id, time.time() + delay)
    except Exception as e:
        logger.warning("⚠️ Не удалось записать заметку о самоудалении %s в чате %s: %s — "
                       "перезапуск в этот срок оставит сообщение висеть",
                       message_id, chat_id, e)


def restore_pending_deletes(bot) -> None:
    """
    Подхватывает заметки, оставшиеся от прошлого процесса (зовёт main.post_init):
    просроченное удаляет сразу, остальное — в свой срок. Заметки старше
    48 часов выбрасывает без попытки — такие сообщения Telegram удалять не даёт.
    """
    try:
        notes = list_pending_deletes()
    except Exception as e:
        logger.warning("⚠️ Не удалось прочитать заметки о самоудалении: %s", e)
        return
    now = time.time()
    overdue = waiting = stale = 0
    for chat_id, message_id, delete_at in notes:
        if delete_at < now - _PENDING_DELETE_MAX_AGE:
            _forget_pending_delete(chat_id, message_id)
            stale += 1
            continue
        delay = delete_at - now
        if not _arm_delete(bot, chat_id, message_id, max(0.0, delay)):
            return
        if delay > 0:
            waiting += 1
        else:
            overdue += 1
    if notes:
        logger.info("🧹 Самоудаления от прошлого запуска: просрочено %d (удаляю сейчас), "
                    "ждут срока %d, старше двух суток и выброшено %d", overdue, waiting, stale)


async def cancel_pending_deletes() -> None:
    """
    Отменяет таймеры самоудаления при остановке бота (зовёт main.post_shutdown).
    Заметки остаются в базе — сообщения удалит следующий запуск.
    """
    loop = asyncio.get_running_loop()
    tasks = [t for t in _DELETE_TASKS if t.get_loop() is loop]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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
