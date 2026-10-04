"""
handlers/admin/panel_quiz.py — 🎮 панель викторины (2026-08-05, решение Максима)

Вопросы викторины НЕ лежат в коде: их пишет Claude по статьям базы знаний и
привозит файлом quiz/questions.json, кнопка «📥 Мои вопросы в черновики»
переносит их в таблицу quiz_bank, а в игру идут ТОЛЬКО одобренные здесь.
Машинная сборка (кнопка «🧠 Собрать вопросы», повтор неудачных, экран «⚠️ Что
не вышло») удалена 04.10.2026, решение Максима. Вместо неё — экран
«📋 Статьи без вопросов»: по каким статьям ещё нечего спросить.

⚠️ ОДОБРЕНИЕ РУЧНОЕ И ЭТО ГЛАВНОЕ В ПАНЕЛИ (решение Максима): новый вопрос
ложится ЧЕРНОВИКОМ и людям не показывается, пока владелец не нажмёт «✅ В игру».
Вопрос с неверным ответом бьёт по доверию ко всему боту — тем более что бот же
его и «объясняет».

Панель открывается двумя путями (как /rag и /mod): командой /quizadm и кнопкой
«🎮 Настройки Викторины» в /adm (⚠️ НЕ путать с публичной кнопкой «🎮 Викторина»
— та шлёт `quiz_start` и запускает игру). Второй сборки текста заводить нельзя —
разъедутся.
"""

import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database.history import (
    get_all_quiz_stats,
    delete_all_quiz_questions,
    delete_quiz_drafts,
    delete_quiz_question,
    get_quiz_bank_counts,
    get_quiz_question,
    list_quiz_questions,
    reset_all_quiz_stats,
    set_quiz_question_approved,
)
from utils import delete_user_message_safe, schedule_delete

from .common import (_adm_back_row, _audit, _is_group_chat, _onoff, _require,
                     _send_panel_message)

logger = logging.getLogger(__name__)

_ICON = "🎮"

# Разделитель панелей — 27 символов, единый стандарт всех панелей бота.
_LINE = "───────────────────────────"

# Сколько названий показывает экран «📋 Статьи без вопросов». Больше — строка
# «…и ещё N»: 60 названий по ~40 знаков укладываются в лимит Telegram
# (4096) с запасом, а длиннее список читать в чате всё равно никто не станет.
_NOQ_SHOW = 60


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
    players = len(get_all_quiz_stats())

    if kb["articles_left"]:
        status = (f"📥 Статей без вопросов: <code>{kb['articles_left']}</code> — "
                  f"попроси Claude написать по ним вопросы.")
    else:
        status = "✅ По всем статьям базы знаний вопросы уже есть."

    # 📄 Сверка эталонного файла с банком (2026-09-01, решение Максима).
    # ⚠️ Ради чего строка существует. Кнопка загрузки пропускает вопрос,
    # который в банке уже есть, ЦЕЛИКОМ — значит правка разбора, вариантов или
    # верного ответа в файле обычной отправкой кода не доезжает никуда, и
    # расхождение файла с игрой ничем не видно. Теперь видно здесь.
    seed = quiz_bank.seed_stats()
    diff = quiz_bank.seed_diff() if seed["questions"] else None
    seed_line = ""
    if diff and diff["file_ok"]:
        marks = []
        if diff["changed"]:
            marks.append(f"⚠️ разошлись <code>{diff['changed']}</code>")
        if diff["missing"]:
            marks.append(f"не залито <code>{diff['missing']}</code>")
        tail = " · ".join(marks) if marks else "всё сошлось"
        seed_line = f"📄 Файл вопросов: <code>{diff['total']}</code> · {tail}\n"

    # 🕛 Вопрос дня (2026-08-20): состояние тумблера и когда уйдёт следующий.
    # Цифры живые, считаются при каждом открытии панели.
    from services import quiz_daily
    auto_on = quiz_daily.is_enabled()
    # Метка состояния — ТОЛЬКО через общий _onoff: он единственный источник
    # надписей «🟢ВКЛ» / «🔴ВЫКЛ» на все 18 тумблеров бота. Собранная руками
    # (как было у этой кнопки с 2026-08-20 до вечера того же дня) разъезжается
    # со всеми соседними экранами.
    auto_line = (f"🕛 Вопрос дня: <b>{_onoff(auto_on)}</b>"
                 + (f" · следующий {quiz_daily.next_run_label()}" if auto_on else "")
                 + "\n")

    text = (
        f"{_ICON} <b>ВИКТОРИНА</b>\n"
        f"{_LINE}\n"
        f"Вопросы пишет Claude по статьям базы знаний и привозит с "
        f"обновлением. В игру идут только одобренные — люди не увидят того, "
        f"что ты не посмотрел.\n"
        f"{_LINE}\n"
        f"✅ В игре: <code>{counts['approved']}</code>\n"
        f"📝 Черновиков: <code>{counts['drafts']}</code>\n"
        f"📚 Статей в базе знаний: <code>{kb['articles_total']}</code>\n"
        f"{seed_line}"
        f"🎖 Игроков со статистикой: <code>{players}</code>\n"
        f"{auto_line}"
        f"{_LINE}\n"
        f"{status}"
    )

    rows = [
        # Часы в надписи — ИЗ РАСПИСАНИЯ (quiz_daily.hours_label), а не строкой:
        # зашитое «В 12:00» пережило добавление вечернего срока и начало бы
        # врать — те же грабли, что с именем модели в логе.
        [InlineKeyboardButton(f"🕛 ВОПРОС ДНЯ {quiz_daily.hours_label()}: {_onoff(auto_on)}",
                              callback_data="quiz:auto")],
        [
            InlineKeyboardButton(f"📝 Черновики ({counts['drafts']})", callback_data="quiz:list:draft"),
            InlineKeyboardButton(f"✅ В игре ({counts['approved']})", callback_data="quiz:list:live"),
        ],
    ]
    # 📋 Список статей без вопросов (04.10.2026, решение Максима) — вместо
    # удалённой машинной сборки. Только когда такие статьи есть: кнопка с
    # пустым списком за ней врала бы, что работа ждёт.
    if kb["articles_left"]:
        rows.append([InlineKeyboardButton(f"📋 Статьи без вопросов ({kb['articles_left']})",
                                          callback_data="quiz:noq")])
    if counts["drafts"]:
        rows.append([InlineKeyboardButton("🗑 Очистить черновики", callback_data="quiz:wipe")])

    # ♻️ Догнать банк до файла. Кнопка есть ТОЛЬКО когда есть что догонять:
    # она переписывает варианты, верный ответ и разбор у вопросов, которые в
    # банке уже лежат, — единственный способ доставить такую правку, кроме
    # ручной работы в боевой базе.
    if diff and diff["changed"]:
        rows.append([InlineKeyboardButton(f"♻️ Обновить из файла ({diff['changed']})",
                                          callback_data="quiz:reseed")])

    # Эталонный набор — вопросы, написанные вручную и приехавшие файлом в
    # обновлении кода (2026-08-05, решение Максима после негодной машинной
    # сборки). Кнопка показывается, только когда файл на месте и в нём что-то
    # есть, иначе она врала бы про несуществующий набор.
    if seed["questions"]:
        # Слово «в черновики» в подписи намеренно: кнопка меняла назначение
        # 28.08.2026, и без него пришлось бы каждый раз вспоминать, куда она
        # грузит — сразу людям или на проверку.
        rows.append([InlineKeyboardButton(f"📥 Мои вопросы в черновики ({seed['questions']})",
                                          callback_data="quiz:seed")])
    if counts["approved"] or counts["drafts"]:
        rows.append([InlineKeyboardButton("🗑 Стереть ВСЕ вопросы", callback_data="quiz:nuke")])
    # ⚠️ Обнуление статистики ИГРОКОВ стоит отдельным рядом и ниже всего
    # остального: соседняя кнопка стирает ВОПРОСЫ, эта — заслуги людей, и
    # перепутать их нажатием не должно быть возможно.
    if players:
        rows.append([InlineKeyboardButton(f"🧹 Обнулить статистику игроков ({players})",
                                          callback_data="quiz:zero")])
    rows.append(_adm_back_row())
    return text, InlineKeyboardMarkup(rows)


async def send_quiz_panel(bot, chat_id: int, context):
    """Показывает панель викторины (команда /quizadm и кнопка «🎮 Настройки Викторины» в /adm)."""
    text, markup = _build_panel(context)
    await _send_panel_message(bot, chat_id, text, markup)


async def cmd_quiz_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /quizadm — панель викторины (владелец).

    ⚠️ Здесь только загрузка и одобрение вопросов; саму игру в чате запускает
    кнопка «🎮 Викторина» (`quiz_start`). Прежняя оговорка «не путать с /quiz»
    снята 28.08.2026: той команды больше нет, она была написана, но никогда
    не регистрировалась. Как все панельные команды, работает лишь в личке —
    в группе молча выходим (сообщение уже удалено).
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
                + ("Пока пусто. Новые вопросы приезжают с обновлением — их "
                   "приносит кнопка «📥 Мои вопросы в черновики»." if mode == "draft"
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


def _build_noq_screen():
    """
    Экран «📋 Статьи без вопросов» (04.10.2026, решение Максима): одобренные
    статьи базы знаний, по которым в банке нет ни одного вопроса. Вопросы по
    ним пишет Claude — экран говорит, когда и о чём его просить. Названия
    берутся из шапок статей (knowledge_store.list_articles), а это чужой
    текст: экранируем.

    Возвращает (текст, клавиатура) — отдельно от отправки, чтобы preflight
    собирал экран и мерил его по лимитам Telegram, как остальные панели.
    """
    from services import quiz_bank
    left = quiz_bank.articles_without_questions()
    lines = [f"{_ICON} <b>СТАТЬИ БЕЗ ВОПРОСОВ</b>", _LINE]
    if not left:
        lines.append("По всем статьям базы знаний вопросы уже есть.")
    else:
        lines += [f"Статей: <code>{len(left)}</code>. Попроси Claude написать по ним "
                  f"вопросы — они приедут с обновлением, а в черновики их принесёт "
                  f"кнопка «📥 Мои вопросы в черновики».", ""]
        for article in left[:_NOQ_SHOW]:
            name = article.get("title") or article["fname"]
            lines.append(f"• {html.escape(name)}")
        if len(left) > _NOQ_SHOW:
            lines.append(f"…и ещё {len(left) - _NOQ_SHOW}")
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ К викторине", callback_data="quiz:panel")]])
    return "\n".join(lines), keyboard


async def _handle_quiz_callback(query, context, data: str, chat_id: int, user_id: int):
    """
    Ветки quiz:<действие>. Все владельческие: в `_CALLBACK_RULES` приставки
    `quiz:` нет, а кнопка без записи в таблице доступна только владельцу
    (запрет по умолчанию — см. services/roles.py).

      quiz:panel              — главный экран панели
      quiz:auto               — тумблер «🕛 Вопрос дня»
      quiz:noq                — список статей, по которым вопросов ещё нет
      quiz:list:<draft|live>  — открыть первый вопрос списка
      quiz:card:<режим>:<id>  — конкретный вопрос (листание)
      quiz:ok:<id>            — одобрить: вопрос уходит в игру
      quiz:back:<id>          — вернуть игровой вопрос в черновики
      quiz:del:<режим>:<id>   — удалить вопрос совсем
      quiz:wipe / quiz:wipe_yes — очистить ВСЕ черновики (с подтверждением)
      quiz:seed               — загрузить вопросы из файла репозитория В ЧЕРНОВИКИ
      quiz:reseed             — догнать банк до эталонного файла
      quiz:nuke / quiz:nuke_yes — стереть ВЕСЬ банк, включая игровые
      quiz:zero / quiz:zero_yes — обнулить статистику викторины У ВСЕХ ИГРОКОВ
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

    # 🕛 Тумблер вопроса дня (2026-08-20, просьба Максима). По умолчанию ВЫКЛ:
    # механизм сам пишет в группы, включаться молча он не должен.
    if action == "auto":
        from services import quiz_daily
        new_val = not quiz_daily.is_enabled()
        quiz_daily.set_enabled(new_val)
        state = "включён" if new_val else "выключен"
        logger.info("🔧 Владелец %s: вопрос дня %s", user_id, state)
        _audit(user_id, "quiz_auto", 0, f"вопрос дня {state}")
        await query.answer(
            f"🕛 Вопрос дня {state}."
            + (f" Следующий уйдёт {quiz_daily.next_run_label()} по Киеву." if new_val else ""),
            show_alert=True,
        )
        await send_quiz_panel(context.bot, chat_id, context)
        return

    if action == "noq":
        await query.answer()
        text, keyboard = _build_noq_screen()
        await _send_panel_message(context.bot, chat_id, text, keyboard)
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

    if action == "seed":
        from services import quiz_bank
        seed = quiz_bank.seed_stats()
        if not seed["questions"]:
            await _popup(query, context, chat_id,
                         "⚠️ Файла с вопросами нет — он приезжает вместе с обновлением кода.")
            return
        # ⚠️ В ЧЕРНОВИКИ, А НЕ СРАЗУ В ИГРУ (28.08.2026, решение Максима).
        # До этого грузили сразу в игру — тогда заезжала пачка из 219 вопросов,
        # выверенных вручную, и одобрять их по одному было работой ради работы.
        # Теперь набор пополняется по 2–3 вопроса на новую статью, и Максим
        # хочет смотреть их перед тем, как они попадут людям.
        result = quiz_bank.load_seed(approved=False)
        _audit(user_id, "quiz_seed", 0,
               f"добавлено в черновики {result['added']}, дублей {result['skipped']}")
        logger.info("🎮 Админ %s загрузил эталонные вопросы В ЧЕРНОВИКИ: добавлено %d, дублей %d, негодных %d",
                    user_id, result["added"], result["skipped"], result["bad"])
        text = f"📥 Загружено вопросов: {result['added']} — в ЧЕРНОВИКИ, на одобрение."
        if result["added"]:
            text += "\nОткрой «📝 Черновики» и одобри те, что годятся."
        if result["skipped"]:
            text += f"\nУже были в банке: {result['skipped']}."
        if result["bad"]:
            text += f"\n⚠️ Не прошли проверку: {result['bad']}."
        await _popup(query, context, chat_id, text)
        await send_quiz_panel(context.bot, chat_id, context)
        return

    if action == "reseed":
        # ♻️ Догнать банк до эталонного файла (2026-09-01, решение Максима).
        # Кнопка загрузки этого не умеет и уметь не должна: она добавляет
        # новое, а эта чинит уже залитое — правку разбора, вариантов или
        # верного ответа, которую сверка по имени вопроса пропускает молча.
        from services import quiz_bank
        result = quiz_bank.seed_apply()
        if not result["changed"]:
            await _popup(query, context, chat_id,
                         "✅ Файл и банк и так сошлись — обновлять нечего.")
            await send_quiz_panel(context.bot, chat_id, context)
            return
        _audit(user_id, "quiz_reseed", 0, f"догнано до файла: {result['updated']}")
        logger.info("🎮 Админ %s догнал банк до эталонного файла: поправлено %d из %d",
                    user_id, result["updated"], result["changed"])
        await _popup(query, context, chat_id,
                     f"♻️ Обновлено вопросов: {result['updated']}.\n"
                     f"Поправлены варианты, верный ответ и разбор — "
                     f"статус «в игре / черновик» не менялся.")
        await send_quiz_panel(context.bot, chat_id, context)
        return

    if action == "nuke":
        # Полная очистка спрашивает подтверждения, как и очистка черновиков:
        # тут сносятся и вопросы, которые уже играют.
        await query.answer()
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup([[
                InlineKeyboardButton("❗️ Да, стереть ВСЁ", callback_data="quiz:nuke_yes"),
                InlineKeyboardButton("Отмена", callback_data="quiz:panel"),
            ]]))
        except Exception as e:
            logger.warning("⚠️ Не удалось показать подтверждение полной очистки: %s", e)
        return

    if action == "nuke_yes":
        removed = delete_all_quiz_questions()
        _audit(user_id, "quiz_nuke", 0, f"стёрто вопросов: {removed}")
        logger.info("🎮 Админ %s стёр ВЕСЬ банк вопросов (%d шт.)", user_id, removed)
        await query.answer(f"🗑 Стёрто вопросов: {removed}")
        await send_quiz_panel(context.bot, chat_id, context)
        return

    if action == "zero":
        # Подтверждение обязательно: заслуги людей восстановить нечем.
        await query.answer()
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup([[
                InlineKeyboardButton("❗️ Да, обнулить всем", callback_data="quiz:zero_yes"),
                InlineKeyboardButton("Отмена", callback_data="quiz:panel"),
            ]]))
        except Exception as e:
            logger.warning("⚠️ Не удалось показать подтверждение обнуления статистики: %s", e)
        return

    if action == "zero_yes":
        removed = reset_all_quiz_stats()
        _audit(user_id, "quiz_zero", 0, f"обнулено игроков: {removed}")
        logger.info("🎮 Админ %s обнулил статистику викторины у всех (%d чел.)", user_id, removed)
        await _popup(query, context, chat_id,
                     f"🧹 Статистика обнулена: {removed} чел.\n"
                     f"Все начинают путь заново — с Рядового.")
        await send_quiz_panel(context.bot, chat_id, context)
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
