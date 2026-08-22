import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database.history import (get_user_stats, add_quiz_attempt, get_remaining_image_calls,
                              get_random_quiz_question, note_quiz_question_asked)
import asyncio
from telegram.constants import ParseMode
from utils import register_and_clean_bot_message, delete_user_message_safe

ACTIVE_QUIZZES = {}

logger = logging.getLogger(__name__)


# Сколько живёт викторина ПО КНОПКЕ. У вопроса дня таймеров нет вовсе.
# ⚠️ Числа подставляются в лог ОТСЮДА, а не пишутся в строку словами: зашитое
# в текст число переживает правку и начинает врать молча — проект на этом уже
# стоял. Меняешь срок — меняешь ровно одну из этих двух величин.
QUIZ_THINK_SEC        = 60   # столько ждём ответа, пока вопрос висит нетронутым
QUIZ_AFTER_ANSWER_SEC = 30   # столько вопрос ещё виден ПОСЛЕ ответа


async def _auto_delete_quiz(context, chat_id: int, message_id: int, poll_id: str):
    """
    Убирает викторину по кнопке. Отсчёт зависит от того, ответили или нет
    (решение Максима 2026-08-22):
      • ответили — вопрос виден ещё QUIZ_AFTER_ANSWER_SEC сек (успеть прочесть
        разбор) и удаляется;
      • не ответили за QUIZ_THINK_SEC сек — удаляется как есть.

    ⚠️ Раньше таймер был ОДИН на оба случая: 60 сек от появления вопроса.
    Замеры по журналу за 22.08.2026 (108 вопросов подряд): дольше 30 сек
    Максим думает над каждым четвёртым вопросом, дольше 60 — раз за день.
    Поэтому «просто уменьшить срок» нельзя: исчезнувший вопрос уже не
    ответишь, а следующий приходит ТОЛЬКО в ответ на ответ — марафон встаёт
    молча. Отсюда два отдельных срока, а не один.
    """
    info = ACTIVE_QUIZZES.get(poll_id)
    event = info.get("answered_event") if info else None
    answered = False
    try:
        if event is None:
            # Отметки об ответе нет (вопрос пришёл не из send_quiz_question) —
            # ведём себя как прежде: ждём срок раздумий и убираем.
            await asyncio.sleep(QUIZ_THINK_SEC)
        else:
            await asyncio.wait_for(event.wait(), timeout=QUIZ_THINK_SEC)
            answered = True
            await asyncio.sleep(QUIZ_AFTER_ANSWER_SEC)
    except asyncio.TimeoutError:
        pass  # за срок раздумий не ответили — убираем вопрос
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(
            "🎮 Викторина убрана (чат %s, %s)", chat_id,
            f"через {QUIZ_AFTER_ANSWER_SEC} сек после ответа" if answered
            else f"без ответа за {QUIZ_THINK_SEC} сек")
    except Exception:
        pass  # уже удалена / нет прав — молча


async def send_quiz_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                             auto: bool = False, question: dict | None = None):
    """
    Отправляет случайный тактический опрос в режиме викторины (Quiz Poll)
    и регистрирует его в ACTIVE_QUIZZES.

    auto=True — это ВОПРОС ДНЯ (2026-08-20, просьба Максима): раз в сутки в
    12:00 по Киеву, см. services/quiz_daily.py. Отличий ровно два:
      • НЕ ставится авто-удаление через минуту — вопрос висит сутки, чтобы люди
        успели ответить; вчерашний убирает следующая отправка;
      • возвращается запись об опросе (chat_id, message_id, poll_id, правильный
        ответ) — её кладут в базу, чтобы ответы считались и после перезапуска.
    Обычный вызов (кнопка «сыграть») возвращает None и ведёт себя как раньше.

    question — готовый вопрос вместо случайного из банка (2026-08-20, решение
    Максима «во все группы один и тот же вопрос»). Тогда отметку «задан»
    ставит ВЫЗЫВАЮЩИЙ и ровно один раз: иначе счётчик вырос бы на число групп,
    и банк расходовался бы вдвое-втрое быстрее, чем идут дни.

    ⚠️ ВОПРОСЫ БЕРУТСЯ ИЗ БАНКА В БАЗЕ (2026-08-05, решение Максима), а не из
    списка в коде: их собирает по статьям базы знаний панель /quizadm, и в игру
    идут только ОДОБРЕННЫЕ там. Пустой банк — штатный случай (сразу после
    выкатки он именно такой), поэтому здесь не ошибка, а понятная людям
    строчка: играть пока нечем.
    """
    # Защитная очистка памяти: если отслеживаемых опросов слишком много, убираем старые
    if len(ACTIVE_QUIZZES) > 500:
        oldest_keys = list(ACTIVE_QUIZZES.keys())[:100]
        for k in oldest_keys:
            ACTIVE_QUIZZES.pop(k, None)

    q = question or get_random_quiz_question()
    if not q:
        logger.info("🎮 Викторина: банк вопросов пуст (чат %s)", chat_id)
        if auto:
            # Вопрос дня при пустом банке просто не приходит: писать в группу
            # «вопросов пока нет» каждый полдень — худшее из возможного.
            return None
        try:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=("🎮 <b>Вопросов пока нет.</b>\n"
                      "Штаб готовит новые задания по базе знаний — загляни чуть позже."),
                parse_mode=ParseMode.HTML,
            )
            if sent_msg:
                await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        except Exception as e:
            logger.warning("⚠️ Не удалось сообщить о пустом банке вопросов: %s", e)
        return

    try:
        # Отправляем родной опрос Telegram в режиме Quiz
        poll_msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=q["question"],
            options=q["options"],
            type="quiz",
            correct_option_id=q["correct_idx"],
            # Пустая строка разбора — не то же самое, что «разбора нет»:
            # Telegram на пустой explanation отвечает ошибкой, и вопрос не
            # уходит вовсе. Модель разбор даёт почти всегда, но «почти».
            explanation=q["explanation"] or None,
            explanation_parse_mode=ParseMode.HTML,
            is_anonymous=False
        )
        # Регистрируем активный опрос, сохраняя правильный ответ, chat_id и флаг автоматического перехода
        ACTIVE_QUIZZES[poll_msg.poll.id] = {
            "correct_idx": q["correct_idx"],
            "chat_id": chat_id,
            "triggered_next": False,
            # ⚠️ Вопрос дня НЕ порождает следующий вопрос (2026-08-20, решение
            # Максима). Правило «ответил — получи следующий» писалось для кнопки
            # «сыграть сейчас», и на суточном вопросе давало цепочку: первый же
            # ответивший запускал поток вопросов в группе. Метка читается
            # в handle_poll_answer.
            "auto": auto,
            # Сигнал «на этот вопрос ответили» для задачи удаления
            # (_auto_delete_quiz). Ставит его handle_poll_answer. У вопроса
            # дня удаления нет вовсе — значит, и ждать сигнал некому.
            "answered_event": None if auto else asyncio.Event(),
            "question": q["question"],
            "options": q["options"],
            "explanation": q["explanation"]
        }
        # Отметку «вопрос задан» ставим ТОЛЬКО после удачной отправки: она
        # опускает вопрос в конец очереди выбора, и считать заданным то, что
        # в чат не ушло, значит незаметно выдавливать вопросы из игры.
        # Вопрос пришёл готовым (вопрос дня во все группы) — отметку ставит
        # вызывающий, один раз на все чаты.
        if question is None:
            note_quiz_question_asked(q["id"])
        logger.info("🎮 Новая викторина отправлена (чат %s)", chat_id)

        # Авто-удаление — только у вопроса ПО КНОПКЕ; сроки и правило
        # «после ответа / без ответа» — в _auto_delete_quiz.
        # ⚠️ У вопроса дня удаления нет намеренно: минута жизни означала бы, что
        # его увидит только тот, кто смотрел в чат ровно в полдень. Вчерашний
        # опрос убирает следующая суточная отправка (jobs/reports.py).
        if not auto:
            asyncio.create_task(_auto_delete_quiz(context, chat_id, poll_msg.message_id, poll_msg.poll.id))
            return None
        return {
            "message_id": poll_msg.message_id,
            "poll_id": poll_msg.poll.id,
            "correct_idx": q["correct_idx"],
            "question": q["question"],
            "options": q["options"],
            "explanation": q["explanation"],
        }
    except Exception as e:
        logger.error("⚠️ Не удалось отправить викторину в чат %s: %s", chat_id, e)
        # ⚠️ У вопроса дня сообщения об ошибке в чат НЕ шлём: люди его не
        # просили, и «❌ Ошибка связи» в группе выглядела бы поломкой на пустом
        # месте. Владелец узнает из лога, а суточная метка не поставится —
        # значит бот попробует снова в ближайший час.
        if auto:
            return None
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
        
    # Ответ пришёл — отсюда задача удаления начинает отсчитывать свой срок
    # (_auto_delete_quiz). Ставится на ЛЮБОЙ ответ, верный или нет, и только
    # один раз: у вопроса дня отметки нет вовсе, там удаления не бывает.
    answered_event = quiz_info.get("answered_event")
    if answered_event is not None and not answered_event.is_set():
        answered_event.set()

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

    # Следующий тактический опрос уходит СРАЗУ после ответа.
    # Используем флаг triggered_next, чтобы гарантировать, что каждый опрос порождает следующий ровно ОДИН раз
    # (даже если в общем чате на опрос отвечают несколько участников одновременно).
    # ОТЛОЖЕННОЙ задачей, а не прямым await: обработчик держал бы всю очередь
    # апдейтов бота, пока идёт отправка. application.create_task — чтобы ошибки
    # задачи попадали в error handler.
    # ⚠️ ПАУЗЫ В 2.5 СЕК ЗДЕСЬ БОЛЬШЕ НЕТ (2026-08-22, решение Максима). Она
    # стояла ровно за тем, чтобы человек успел прочитать разбор во всплывающем
    # окне Telegram до прихода следующего вопроса. Максим убрал её, зная эту
    # цену: на марафоне в сотню вопросов экономится около четырёх минут.
    # Не возвращать «чтобы успевали читать» — вернётся только по его просьбе.
    # ⚠️ У ВОПРОСА ДНЯ ПРОДОЛЖЕНИЯ НЕТ (2026-08-20). Марафон остаётся только
    # у кнопки «сыграть сейчас». Проверка обязана быть здесь, а не в отправке:
    # цепочку запускает ОТВЕТ, а не сама отправка.
    if quiz_info.get("auto"):
        return
    if not quiz_info.get("triggered_next", False):
        quiz_info["triggered_next"] = True
        context.application.create_task(send_quiz_question(chat_id, context))


