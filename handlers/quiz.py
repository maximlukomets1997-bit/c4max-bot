import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import IMAGE_DAILY_LIMIT, ADMIN_IDS
from database.history import get_user_stats, add_quiz_attempt, get_remaining_image_calls
from data.quiz_questions import get_random_question
import asyncio
from telegram.constants import ParseMode
from utils import register_and_clean_bot_message, delete_user_message_safe

ACTIVE_QUIZZES = {}

logger = logging.getLogger(__name__)


async def _auto_delete_quiz(context, chat_id: int, message_id: int, poll_id: str):
    """Через 60 сек удаляет викторину (отвеченную или нет) и пишет об этом в лог."""
    await asyncio.sleep(60)
    info = ACTIVE_QUIZZES.get(poll_id)
    answered = bool(info and info.get("triggered_next"))
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info("🎮 Викторина авто-удалена через 60 сек (чат %s, %s)",
                    chat_id, "был ответ" if answered else "без ответа")
    except Exception:
        pass  # уже удалена / нет прав — молча


async def send_quiz_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет случайный тактический опрос в режиме викторины (Quiz Poll)
    и регистрирует его в ACTIVE_QUIZZES.
    """
    # Защитная очистка памяти: если отслеживаемых опросов слишком много, убираем старые
    if len(ACTIVE_QUIZZES) > 500:
        oldest_keys = list(ACTIVE_QUIZZES.keys())[:100]
        for k in oldest_keys:
            ACTIVE_QUIZZES.pop(k, None)
            
    q = get_random_question()
    
    try:
        # Отправляем родной опрос Telegram в режиме Quiz
        poll_msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=q["question"],
            options=q["options"],
            type="quiz",
            correct_option_id=q["correct_idx"],
            explanation=q["explanation"],
            explanation_parse_mode=ParseMode.HTML,
            is_anonymous=False
        )
        # Регистрируем активный опрос, сохраняя правильный ответ, chat_id и флаг автоматического перехода
        ACTIVE_QUIZZES[poll_msg.poll.id] = {
            "correct_idx": q["correct_idx"],
            "chat_id": chat_id,
            "triggered_next": False,
            "question": q["question"],
            "options": q["options"],
            "explanation": q["explanation"]
        }
        logger.info("🎮 Новая викторина отправлена (чат %s)", chat_id)

        # Авто-удаление этой викторины через 60 сек — и отвеченной, и неотвеченной.
        asyncio.create_task(_auto_delete_quiz(context, chat_id, poll_msg.message_id, poll_msg.poll.id))
    except Exception as e:
        logger.error("⚠️ Не удалось отправить викторину в чат %s: %s", chat_id, e)
        # Пробуем сообщить об ошибке
        try:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text="❌ <b>Ошибка связи!</b> Не удалось доставить тактический опрос в сектор. Попробуйте еще раз.",
                parse_mode=ParseMode.HTML
            )
            if sent_msg:
                await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        except Exception:
            pass


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    chat_id = update.effective_chat.id
    logger.info("🎮 Запущена викторина (чат %s)", chat_id)
    await send_quiz_question(chat_id, context)


async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /rank — тонкая обёртка над send_rank_panel (см. ниже)."""
    await delete_user_message_safe(update.message)
    await send_rank_panel(context.bot, update.effective_chat.id, update.effective_user)


async def send_rank_panel(bot, chat_id: int, user):
    """
    Личное дело бойца — звание, статистика викторин, остаток картинок и служба
    в гарнизоне. Вынесено из cmd_rank 2026-08-04 тем же приёмом, что
    send_adm_panel / send_mod_panel: панель открывается И командой /rank,
    И кнопкой «🎖 Моё звание» с экрана /start. Второй сборки того же текста
    заводить нельзя — разъедутся.
    """
    user_id = user.id
    username = user.username or user.first_name or f"ID_{user_id}"
    # logger.info("🪖 Открыто личное дело /rank (%s)", username)  # скрыто по просьбе

    stats = get_user_stats(user_id)
    
    correct = stats["correct_answers"]
    attempts = stats["total_attempts"]
    rate = stats["success_rate"]
    rank = stats["rank"]
    icon = stats["rank_icon"]
    desc = stats["rank_desc"]
    next_needed = stats["next_rank_needed"]
    rank_min = stats["rank_min"]

    # Почётное звание из карточки пользователя (/users) перекрывает заработанное
    # в викторине. Значок и характеристику берём из общего списка config.QUIZ_RANKS
    # по имени. Прогресс-бар ниже НЕ трогаем: он про викторину, а она идёт своим
    # чередом (просто повышения больше не объявляются — см. handle_poll_answer).
    from services.user_settings import honorary_rank
    from config import QUIZ_RANKS
    honorary = honorary_rank(user_id)
    rank_suffix = ""
    if honorary:
        found = next((r for r in QUIZ_RANKS if r["name"] == honorary), None)
        rank = honorary
        rank_suffix = " 🏅"
        if found:
            icon, desc = found["icon"], found["desc"]
    
    # Расчет лимитов генерации картинок. Лимит берём тем же помощником, что и
    # /imagine (services/user_settings.image_limit_for): персональный из карточки
    # пользователя, иначе общий; None — безлимит у админов бота. Считать здесь
    # по-своему нельзя — показанный остаток разъедется с фактическим.
    from services.user_settings import image_limit_for
    daily_limit = image_limit_for(user_id)
    if daily_limit is None:
        img_limit_text = "∞ из ∞ (Безлимит)"
    else:
        img_limit_text = f"{get_remaining_image_calls(user_id, daily_limit)} из {daily_limit}"

    # Служба в гарнизоне: личное дело (стаж, активность, статус «проверенный»).
    # Правило доверия — одно, в services/antispam.py::trust_info.
    from services.antispam import trust_info
    ti = trust_info(user_id)
    if ti["trusted"]:
        trust_line = "✅ <b>Проверенный боец</b>"
    elif ti["forgive_left"]:
        trust_line = f"⚠️ Взыскание активно ещё {ti['forgive_left']} дн."
    else:
        need = []
        if ti["days"] < ti["need_days"]:
            need.append(f"стаж {ti['days']}/{ti['need_days']} дн.")
        if ti["msgs"] < ti["need_msgs"]:
            need.append(f"сообщений {ti['msgs']}/{ti['need_msgs']}")
        trust_line = "⏳ На испытательном сроке" + (f" ({', '.join(need)})" if need else "")
    
    # Генерация красивого прогресс-бара
    if next_needed == -1:
        # Максимальный ранк
        progress_bar = "🟢" * 10 + " 100%"
        progress_text = "Достигнуто высшее звание Генерального Штаба! 👑"
    else:
        total_needed = next_needed - rank_min
        progress = correct - rank_min
        if progress < 0:
            progress = 0
        if total_needed <= 0:
            total_needed = 1
        
        fraction = min(max(progress / total_needed, 0.0), 1.0)
        filled = int(fraction * 10)
        empty = 10 - filled
        percent = int(fraction * 100)
        progress_bar = "🟢" * filled + "⚪" * empty + f" {percent}%"
        progress_text = f"Прогресс до следующего звания: {correct}/{next_needed} верных ответов."

    # HTML версия для Telegram
    # Имя/ник пользователя — чужой текст: экранируем, иначе символ «<» в имени
    # ломает HTML-разметку и /rank перестаёт отвечать этому пользователю.
    text_html = (
        f"🪖 <b>ЛИЧНОЕ ДЕЛО БОЙЦА</b> 🪖\n"
        f"───────────────────────────\n"
        f"👤 <b>Позывной:</b> @{html.escape(username)}\n"
        f"🎖️ <b>Звание:</b> {icon} <b>{html.escape(rank)}</b>{rank_suffix}\n"
        f"📝 <b>Характеристика:</b> <i>«{desc}»</i>\n"
        f"───────────────────────────\n"
        f"📊 <b>Боевая статистика:</b>\n"
        f"├ Пройдено тестов: <code>{attempts}</code>\n"
        f"├ Точных попаданий: <code>{correct}</code>\n"
        f"└ Эффективность (АКК): <code>{rate}%</code>\n"
        f"───────────────────────────\n"
        f"🎨 <b>Генерация картинок:</b>\n"
        f"└ Остаток на сегодня: <code>{img_limit_text}</code>\n"
        f"───────────────────────────\n"
        f"🪪 <b>Служба в гарнизоне:</b>\n"
        f"├ Стаж: <code>{ti['days']} дн.</code>\n"
        f"├ Сообщений в группах: <code>{ti['msgs']}</code>\n"
        f"└ {trust_line}\n"
        f"───────────────────────────\n"
        f"📈 {progress_bar}\n"
        f"👉 {progress_text}"
    )

    rank_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Викторина", callback_data="quiz_start"),
            InlineKeyboardButton("🗑️ Очистить историю", callback_data="clear_history_btn"),
        ],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:back")],
    ])

    sent_msg = await bot.send_message(
        chat_id=chat_id,
        text=text_html,
        parse_mode=ParseMode.HTML,
        reply_markup=rank_keyboard,
    )
    if sent_msg:
        await register_and_clean_bot_message(bot, chat_id, sent_msg.message_id)


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll_answer = update.poll_answer
    poll_id = poll_answer.poll_id
    
    if poll_id not in ACTIVE_QUIZZES:
        return
        
    quiz_info = ACTIVE_QUIZZES[poll_id]
    correct_idx = quiz_info["correct_idx"]
    chat_id = quiz_info["chat_id"]
    
    user = poll_answer.user
    user_id = user.id
    
    username = user.username or user.first_name or f"ID_{user_id}"
    
    selected_option = poll_answer.option_ids[0] if poll_answer.option_ids else None
    if selected_option is None:
        return
        
    is_correct = (selected_option == correct_idx)
    
    # Получаем старую статистику перед записью
    old_stats = get_user_stats(user_id)
    
    # Записываем результат попытки в БД
    add_quiz_attempt(user_id, username, is_correct)
    
    # Получаем обновленную статистику
    new_stats = get_user_stats(user_id)
    
    logger.info("🎮 %s ответил на викторину: %s", username, "верно ✅" if is_correct else "неверно ❌")

    # Если звание повысилось, присылаем личное сообщение с поздравлением.
    # У кого стоит ПОЧЁТНОЕ звание (карточка /users) — молчим: бот объявил бы
    # «тебе присвоено звание Сержант», пока в /rank у человека висит «Полковник».
    from services.user_settings import honorary_rank
    if old_stats["rank"] != new_stats["rank"] and not honorary_rank(user_id):
        # Имя/ник — чужой текст: экранируем (см. комментарий в cmd_rank).
        promotion_text = (
            f"⚡ <b>ВНИМАНИЕ НА ПЛАЦУ! ВНЕОЧЕРЕДНОЕ ПОВЫШЕНИЕ!</b> ⚡\n\n"
            f"Солдат @{html.escape(username)}, приказом Генерального Штаба за отличные тактические знания и боевые заслуги тебе присвоено новое воинское звание!\n\n"
            f"🎖️ Новое звание: {new_stats['rank_icon']} <b>{new_stats['rank']}</b>\n"
            f"📝 <i>«{new_stats['rank_desc']}»</i>\n\n"
            f"Так держать, боец! Увидимся в бою! 🫡"
        )
        try:
            # Отправляем сообщение напрямую в ЛС пользователю
            await context.bot.send_message(
                chat_id=user_id,
                text=promotion_text,
                parse_mode=ParseMode.HTML
            )
            logger.info("🎮 %s повышен в звании: %s", username, new_stats["rank"])
        except Exception as e:
            logger.warning("⚠️ Не удалось отправить ЛС о повышении пользователю %s: %s", user_id, e)

    # Автоматически отправляем следующий тактический опрос через небольшую паузу (2.5 секунды).
    # Используем флаг triggered_next, чтобы гарантировать, что каждый опрос порождает следующий ровно ОДИН раз
    # (даже если в общем чате на опрос отвечают несколько участников одновременно).
    # Пауза — ОТЛОЖЕННОЙ задачей, а не sleep прямо в обработчике: обработчик
    # с ожиданием внутри держал бы всю очередь апдейтов бота 2.5 секунды.
    # application.create_task — чтобы ошибки задачи попадали в error handler.
    if not quiz_info.get("triggered_next", False):
        quiz_info["triggered_next"] = True

        async def _next_question_later():
            await asyncio.sleep(2.5)  # пользователи успевают увидеть результат и прочесть разбор
            await send_quiz_question(chat_id, context)

        context.application.create_task(_next_question_later())


