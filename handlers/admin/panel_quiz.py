"""
handlers/admin/panel_quiz.py — 🎮 панель викторины (2026-08-05, решение Максима)

Вопросы викторины больше НЕ лежат в коде: их собирает по статьям базы знаний
кнопка «🧠 Собрать вопросы» (services/quiz_bank.py), хранит таблица quiz_bank,
а в игру идут ТОЛЬКО одобренные здесь.

⚠️ ОДОБРЕНИЕ РУЧНОЕ И ЭТО ГЛАВНОЕ В ПАНЕЛИ (решение Максима): собранный вопрос
ложится ЧЕРНОВИКОМ и людям не показывается, пока владелец не нажмёт «✅ В игру».
Модель регулярно ошибается в цифрах ТТХ, а вопрос с неверным ответом бьёт по
доверию ко всему боту — тем более что бот же его и «объясняет».

Панель открывается двумя путями (как /rag и /mod): командой /quizadm и кнопкой
«🎮 Викторина» в /adm. Второй сборки текста заводить нельзя — разъедутся.
"""

import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database.history import (
    delete_quiz_drafts,
    delete_quiz_question,
    get_quiz_bank_counts,
    get_quiz_question,
    list_quiz_questions,
    set_quiz_question_approved,
)
from utils import delete_user_message_safe, schedule_delete

from .common import _adm_back_row, _audit, _is_group_chat, _require, _send_panel_message

logger = logging.getLogger(__name__)

_ICON = "🎮"

# Разделитель панелей — 27 символов, единый стандарт всех панелей бота.
_LINE = "───────────────────────────"


def _gen_running(context) -> bool:
    """
    Идёт ли сейчас сборка вопросов. Защёлка в bot_data (не в модуле): тот же
    приём, что у пересборки базы знаний — второй запуск в параллель заплатил бы
    за те же статьи дважды.
    """
    return bool(context.application.bot_data.get("quiz_gen_running"))


async def _popup(query, context, chat_id: int, text: str):
    """
    Всплывающее окно кнопки. ⚠️ Лимит Telegram — 200 символов: длиннее окно
    просто НЕ показывается, и кнопка выглядит мёртвой. Длинный текст уходит
    отдельным самоудаляющимся сообщением (тот же запасной путь, что в /rag).
    """
    if len(text) <= 190:
        await query.answer(text, show_alert=True)
        return
    await query.answer()
    msg = await context.bot.send_message(chat_id=chat_id, text=text)
    if msg:
        schedule_delete(context.bot, chat_id, msg.message_id, 60)


def _question_card(q: dict, position: str = "") -> str:
    """
    Текст карточки одного вопроса: сам вопрос, варианты с отметкой верного,
    разбор и статья-источник. Всё, что пришло от модели, — чужой текст:
    экранируем, иначе «<» в вопросе ломает разметку и карточка не открывается.
    """
    lines = [f"{_ICON} <b>ВОПРОС #{q['id']}</b>{position}", _LINE,
             f"<b>{html.escape(q['question'])}</b>", ""]
    for idx, option in enumerate(q["options"]):
        mark = "✅" if idx == q["correct_idx"] else "▫️"
        lines.append(f"{mark} {html.escape(option)}")
    if q["explanation"]:
        lines += ["", f"💬 <i>{html.escape(q['explanation'])}</i>"]
    lines += [_LINE,
              f"📚 Статья: <code>{html.escape(q['article'])}</code>",
              f"🎯 Задавали раз: <code>{q['asked_count']}</code>"]
    return "\n".join(lines)


def _card_keyboard(q: dict, ids: list, mode: str) -> InlineKeyboardMarkup:
    """
    Кнопки под карточкой вопроса: листание по списку, одобрение/возврат в
    черновики и удаление.

    ⚠️ Листание идёт по СПИСКУ НОМЕРОВ, а не по «следующему id»: одобренный
    вопрос уходит из списка черновиков, и «id+1» после пары одобрений начал бы
    прыгать через записи. Здесь же соседей считаем от текущего положения.
    """
    rows = []
    try:
        pos = ids.index(q["id"])
    except ValueError:
        pos = 0
    prev_id = ids[pos - 1] if pos > 0 else None
    next_id = ids[pos + 1] if pos < len(ids) - 1 else None

    rows.append([
        InlineKeyboardButton("⬅️" if prev_id else "▫️",
                             callback_data=f"quiz:card:{mode}:{prev_id}" if prev_id else "quiz:noop"),
        InlineKeyboardButton(f"{pos + 1} из {len(ids)}", callback_data="quiz:noop"),
        InlineKeyboardButton("➡️" if next_id else "▫️",
                             callback_data=f"quiz:card:{mode}:{next_id}" if next_id else "quiz:noop"),
    ])
    if mode == "draft":
        rows.append([
            InlineKeyboardButton("✅ В игру", callback_data=f"quiz:ok:{q['id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"quiz:del:draft:{q['id']}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("↩️ В черновики", callback_data=f"quiz:back:{q['id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"quiz:del:live:{q['id']}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ К викторине", callback_data="quiz:panel")])
    return InlineKeyboardMarkup(rows)


def _build_panel(context):
    """Текст и кнопки главного экрана панели викторины."""
    from services import quiz_bank

    counts = get_quiz_bank_counts()
    kb = quiz_bank.stats()

    if _gen_running(context):
        status = "⏳ Идёт сборка вопросов — итог придёт сообщением."
    elif kb["articles_left"]:
        status = f"📥 Статей без вопросов: <code>{kb['articles_left']}</code> — есть что собрать."
    else:
        status = "✅ По всем статьям базы знаний вопросы уже собраны."

    text = (
        f"{_ICON} <b>ВИКТОРИНА</b>\n"
        f"{_LINE}\n"
        f"Вопросы собираются по статьям базы знаний. В игру идут только "
        f"одобренные — люди не увидят того, что ты не посмотрел.\n"
        f"{_LINE}\n"
        f"✅ В игре: <code>{counts['approved']}</code>\n"
        f"📝 Черновиков: <code>{counts['drafts']}</code>\n"
        f"📚 Статей в базе знаний: <code>{kb['articles_total']}</code>\n"
        f"{_LINE}\n"
        f"{status}"
    )

    rows = [
        [InlineKeyboardButton("🧠 Собрать вопросы", callback_data="quiz:gen")],
        [
            InlineKeyboardButton(f"📝 Черновики ({counts['drafts']})", callback_data="quiz:list:draft"),
            InlineKeyboardButton(f"✅ В игре ({counts['approved']})", callback_data="quiz:list:live"),
        ],
    ]
    if counts["drafts"]:
        rows.append([InlineKeyboardButton("🗑 Очистить черновики", callback_data="quiz:wipe")])
    rows.append(_adm_back_row())
    return text, InlineKeyboardMarkup(rows)


async def send_quiz_panel(bot, chat_id: int, context):
    """Показывает панель викторины (команда /quizadm и кнопка «🎮 Викторина» в /adm)."""
    text, markup = _build_panel(context)
    await _send_panel_message(bot, chat_id, text, markup)


async def cmd_quiz_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /quizadm — панель викторины (владелец).

    ⚠️ НЕ путать с /quiz: та публичная и запускает игру в чате. Здесь только
    сборка и одобрение вопросов, и, как все панельные команды, работает лишь
    в личке — в группе молча выходим (сообщение уже удалено).
    """
    await delete_user_message_safe(update.message)
    if not await _require(update, context, "owner"):
        return
    if _is_group_chat(update):
        return
    await send_quiz_panel(context.bot, update.effective_chat.id, context)


async def _show_card(bot, chat_id: int, context, mode: str, qid: int | None):
    """
    Карточка вопроса из списка черновиков (mode='draft') или игровых ('live').
    qid=None — открываем первый в списке.
    """
    questions = list_quiz_questions(approved=(mode == "live"))
    if not questions:
        text = (f"{_ICON} <b>{'ВОПРОСЫ В ИГРЕ' if mode == 'live' else 'ЧЕРНОВИКИ'}</b>\n"
                f"{_LINE}\n"
                + ("Пока пусто. Нажми «🧠 Собрать вопросы» — бот сделает их по "
                   "статьям базы знаний." if mode == "draft"
                   else "В игре пока нет ни одного вопроса. Одобри черновики — "
                        "и викторина оживёт."))
        await _send_panel_message(bot, chat_id, text,
                                  InlineKeyboardMarkup([[InlineKeyboardButton(
                                      "⬅️ К викторине", callback_data="quiz:panel")]]))
        return

    ids = [q["id"] for q in questions]
    target = next((q for q in questions if q["id"] == qid), questions[0])
    await _send_panel_message(bot, chat_id, _question_card(target),
                              _card_keyboard(target, ids, mode))


async def _handle_quiz_callback(query, context, data: str, chat_id: int, user_id: int):
    """
    Ветки quiz:<действие>. Все владельческие: в `_CALLBACK_RULES` приставки
    `quiz:` нет, а кнопка без записи в таблице доступна только владельцу
    (запрет по умолчанию — см. services/roles.py).

      quiz:panel              — главный экран панели
      quiz:gen                — собрать вопросы по новым статьям (фоном)
      quiz:list:<draft|live>  — открыть первый вопрос списка
      quiz:card:<режим>:<id>  — конкретный вопрос (листание)
      quiz:ok:<id>            — одобрить: вопрос уходит в игру
      quiz:back:<id>          — вернуть игровой вопрос в черновики
      quiz:del:<режим>:<id>   — удалить вопрос совсем
      quiz:wipe / quiz:wipe_yes — очистить ВСЕ черновики (с подтверждением)
      quiz:noop               — заглушка счётчика листания
    """
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "noop":
        await query.answer()
        return

    if action == "panel":
        await query.answer()
        await send_quiz_panel(context.bot, chat_id, context)
        return

    if action == "gen":
        await _start_generation(query, context, chat_id, user_id)
        return

    if action == "list":
        mode = parts[2] if len(parts) > 2 else "draft"
        await query.answer()
        await _show_card(context.bot, chat_id, context, mode, None)
        return

    if action == "card":
        mode = parts[2] if len(parts) > 2 else "draft"
        qid = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
        await query.answer()
        await _show_card(context.bot, chat_id, context, mode, qid)
        return

    if action in ("ok", "back"):
        qid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        question = get_quiz_question(qid)
        if not question:
            await _popup(query, context, chat_id, "⚠️ Вопрос уже удалён.")
            await send_quiz_panel(context.bot, chat_id, context)
            return
        to_game = (action == "ok")
        set_quiz_question_approved(qid, to_game)
        _audit(user_id, "quiz_approve" if to_game else "quiz_draft", 0,
               f"вопрос #{qid} ({question['article']})")
        logger.info("🎮 Админ %s %s вопрос #%s", user_id,
                    "одобрил" if to_game else "вернул в черновики", qid)
        await query.answer("✅ Вопрос в игре" if to_game else "↩️ Вопрос в черновиках")
        # Остаёмся в ТОМ ЖЕ списке, откуда пришли: одобрив черновик, человек
        # почти всегда хочет посмотреть следующий, а не возвращаться в панель.
        await _show_card(context.bot, chat_id, context, "draft" if to_game else "live", None)
        return

    if action == "del":
        mode = parts[2] if len(parts) > 2 else "draft"
        qid = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        question = get_quiz_question(qid)
        delete_quiz_question(qid)
        if question:
            _audit(user_id, "quiz_delete", 0, f"вопрос #{qid} ({question['article']})")
            logger.info("🎮 Админ %s удалил вопрос #%s", user_id, qid)
        await query.answer("🗑 Вопрос удалён")
        await _show_card(context.bot, chat_id, context, mode, None)
        return

    if action == "wipe":
        # Подтверждение прямо в панели — как у очистки журнала в /rag.
        await query.answer()
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup([[
                InlineKeyboardButton("❗️ Да, очистить черновики", callback_data="quiz:wipe_yes"),
                InlineKeyboardButton("Отмена", callback_data="quiz:panel"),
            ]]))
        except Exception as e:
            logger.warning("⚠️ Не удалось показать подтверждение очистки черновиков: %s", e)
        return

    if action == "wipe_yes":
        removed = delete_quiz_drafts()
        _audit(user_id, "quiz_wipe", 0, f"черновиков удалено: {removed}")
        logger.info("🎮 Админ %s очистил черновики викторины (%d шт.)", user_id, removed)
        await query.answer(f"🗑 Удалено черновиков: {removed}")
        await send_quiz_panel(context.bot, chat_id, context)
        return

    await query.answer("⚠️ Неизвестная кнопка викторины.")


async def _start_generation(query, context, chat_id: int, user_id: int):
    """
    Кнопка «🧠 Собрать вопросы»: проход по статьям, у которых вопросов ещё нет.

    ⚠️ ФОНОВОЙ ЗАДАЧЕЙ, а не прямо в обработчике: на десяток статей уходят
    минуты работы модели, а у ответа на нажатие кнопки лимит Telegram ~15 сек.
    Тот же приём, что у пересборки базы знаний в /rag: защёлка в bot_data,
    итог отдельным сообщением, панель перерисовывается в конце.
    """
    import asyncio
    from services import quiz_bank

    if _gen_running(context):
        await _popup(query, context, chat_id, "⏳ Сборка уже идёт — дождитесь итога.")
        return

    left = quiz_bank.stats()["articles_left"]
    if not left:
        await _popup(query, context, chat_id,
                     "✅ По всем статьям базы знаний вопросы уже собраны. "
                     "Новые появятся, когда добавишь статьи.")
        return

    logger.info("🎮 Админ %s запустил сборку вопросов викторины (статей без вопросов: %d)",
                user_id, left)
    bot = context.bot

    async def _generate_and_report():
        result = None
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, quiz_bank.generate_batch)
        except Exception as e:
            logger.error("⚠️ Сборка вопросов викторины упала: %s", e)
        finally:
            context.application.bot_data["quiz_gen_running"] = False

        if result is None:
            text = "⚠️ Не удалось собрать вопросы — детали в логе."
        elif not result["saved"]:
            text = (f"⚠️ Из {result['articles']} статей не вышло ни одного вопроса. "
                    f"Похоже, модель недоступна — попробуй позже.")
        else:
            _audit(user_id, "quiz_generate", 0,
                   f"статей {result['articles']}, вопросов {result['saved']}")
            text = (f"🧠 Собрано вопросов: {result['saved']} "
                    f"(статей обработано: {result['articles']}).\n"
                    f"Они лежат в черновиках — посмотри и одобри те, что годятся.")
            if result["failed"]:
                text += f"\n⚠️ Статей без результата: {result['failed']}."
            if result["left"]:
                text += f"\n📥 Осталось статей на следующее нажатие: {result['left']}."
        try:
            msg = await bot.send_message(chat_id=chat_id, text=text)
            if msg:
                schedule_delete(bot, chat_id, msg.message_id, 120)
        except Exception as e:
            logger.warning("⚠️ Не удалось отправить итог сборки вопросов: %s", e)
        await send_quiz_panel(bot, chat_id, context)

    context.application.bot_data["quiz_gen_running"] = True
    context.application.create_task(_generate_and_report())
    await _popup(query, context, chat_id,
                 "⏳ Сборка запущена в фоне — итог придёт отдельным сообщением. "
                 "Бот продолжает работать как обычно.")
