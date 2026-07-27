import logging
import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import IMAGE_DAILY_LIMIT, ADMIN_IDS
from database.history import register_image_call, unregister_image_call, get_remaining_image_calls
from services.gemini import generate_image
from utils import register_and_clean_bot_message, delete_user_message_safe, keep_chat_action

logger = logging.getLogger(__name__)

async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Суточный лимит: персональный из карточки пользователя (/users), если
    # задан, иначе общий IMAGE_DAILY_LIMIT; None — безлимит (админы бота).
    # ЕДИНЫЙ источник — services/user_settings.image_limit_for: тот же помощник
    # зовёт /rank, иначе показанный остаток разъедется с фактическим.
    from services.user_settings import image_limit_for
    daily_limit = image_limit_for(user_id)
    unlimited = daily_limit is None
    image_remaining = 0 if unlimited else get_remaining_image_calls(user_id, daily_limit)
    # Человекочитаемая метка остатка лимита для логов: у админа — безлимит.
    limit_label = "∞" if unlimited else f"{image_remaining} из {daily_limit}"

    if context.args:
        prompt = " ".join(context.args)
    else:
        # logger.info("🎨 /imagine без описания (user %s) — показана подсказка", user_id)  # скрыто по просьбе
        text = f"🎨 Используй команду так: `/imagine [описание картинки]`\nНапример: `/imagine танк Т-90 в стиле киберпанк`"
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    if not unlimited and image_remaining <= 0:
        logger.info("🎨 Отказ в картинке (пользователь %s): лимит на сегодня исчерпан (0 из %d)", user_id, daily_limit)
        if daily_limit == 0:
            # Персональный запрет из карточки пользователя — не «на сегодня»,
            # завтра ничего не обновится, и врать об этом не надо.
            err_text = "❌ <b>Генерация картинок недоступна.</b>\n\nДля тебя она отключена администрацией."
        else:
            err_text = "❌ <b>Лимит исчерпан!</b>\n\nТы уже потратил все доступные генерации картинок на сегодня. Лимиты обновятся завтра!"
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=err_text, parse_mode=ParseMode.HTML)
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    logger.info("🎨 Запрос картинки (пользователь %s): %s | лимит: %s", user_id, prompt[:100], limit_label)

    # Списываем попытку СРАЗУ, до генерации (и до первого await после проверки
    # лимита): обработчик не блокирует очередь (block=False в handlers/__init__.py),
    # и списание после генерации позволило бы залпом /imagine обойти лимит.
    # Если генерация не удастся — попытка вернётся (unregister_image_call ниже).
    register_image_call(user_id)

    status_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ _Рисую картинку, ожидайте..._", parse_mode=ParseMode.MARKDOWN)

    # Статус «отправляет фото…» висит всю генерацию — в дополнение
    # к сообщению-статусу «Рисую картинку…» выше.
    async with keep_chat_action(context.bot, chat_id, "upload_photo"):
        loop = asyncio.get_running_loop()
        image_bytes = await loop.run_in_executor(None, generate_image, prompt)
    
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
    except Exception:
        pass
        
    if image_bytes:
        # Попытка уже списана авансом перед генерацией — здесь только остаток.
        remaining_after = "∞" if unlimited else f"{get_remaining_image_calls(user_id, daily_limit)} из {daily_limit}"
        logger.info("🎨 Картинка готова (пользователь %s) | осталось: %s", user_id, remaining_after)

        try:
            sent_msg = await context.bot.send_photo(chat_id=chat_id, photo=image_bytes)
        except Exception as e:
            logger.error("⚠️ Не удалось отправить сгенерированную картинку: %s", e)
            err_msg = await context.bot.send_message(chat_id=chat_id, text="❌ <b>Ошибка!</b> Сгенерированная картинка слишком тяжелая или произошла ошибка Telegram API.", parse_mode=ParseMode.HTML)
            if err_msg:
                await register_and_clean_bot_message(context.bot, chat_id, err_msg.message_id)

    else:
        # Генерация не удалась — возвращаем списанную авансом попытку.
        unregister_image_call(user_id)
        err_msg = await context.bot.send_message(chat_id=chat_id, text="❌ <b>Ошибка!</b> Не удалось сгенерировать изображение.", parse_mode=ParseMode.HTML)
        if err_msg:
            await register_and_clean_bot_message(context.bot, chat_id, err_msg.message_id)


