# ───────────────────────────────────────────────
#  jobs/reports.py — Суточный и недельный отчёты о расходах + ночная копия базы
#
#  Выделен из jobs.py 2026-08-04 разрезом БЕЗ изменения логики
#  (файл был на 837 строк и держал шесть независимых циклов).
#  Копия базы живёт ВНУТРИ отчётного цикла, а не своим: два цикла, проснувшихся
#  в одну полночь, о порядке «копия следом за отчётом» не договорятся.
# ───────────────────────────────────────────────

import logging
import asyncio
import html
import os

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────
#  Суточный отчёт о расходах (00:00 по Киеву, в личку владельцу)
# ───────────────────────────────────────────────

async def daily_report_loop(application):
    """
    Раз в сутки в 00:00 по Киеву присылает ВЛАДЕЛЬЦУ в личку отчёт о расходах
    за прошедшие сутки: вызовы по каждой модели, расход $ по провайдерам и
    сожжённые токены Qwen — те же счётчики, что в панели «📡 Настройки API».

    Как считается: панельные счётчики накопительные, поэтому в полночь бот
    делает «фотографию» их значений и показывает разницу с прошлой полуночью
    (services/daily_report.py). Вызовы берутся точно, по времени из api_calls.

    Первый запуск после внедрения: снимков ещё нет — просто ставится точка
    отсчёта, отчёт придёт в ближайшую полночь.

    Бот был выключен в полночь: условие отправки — «по Киеву наступили новые
    сутки с момента снимка», поэтому пропущенный отчёт уходит сразу при
    запуске, а период в заголовке подписан честно («с 24.07 00:00 по 26.07
    12:40»). Перезапуск в течение суток отчёт НЕ порождает.

    Каждый ПОНЕДЕЛЬНИК в 00:00 следом за суточным уходит ВТОРОЕ сообщение —
    недельный отчёт за пн–вс. Он считается не разницей снимков за 7 дней, а
    суммой уже посчитанных суток (копилка в settings, см. services/daily_report.py):
    первого числа месяца бот обнуляет счётчики, и прямая разница в такую неделю
    соврала бы. Проспал понедельник — отчёт уйдёт при первом запуске, с честной
    подписью «дней: N».

    Сообщение НЕ регистрируется в гигиене панелей (как «Итоги месяца») —
    история расходов должна оставаться в чате, панели её не затирают.
    Спим до полуночи кусками не больше часа: так цикл переживает и спящий
    компьютер, и переход на зимнее/летнее время.

    С 2026-07-31 этот же цикл СЛЕДОМ ЗА ОТЧЁТАМИ отправляет ночную копию базы
    (nightly_backup ниже — там же написано, почему она живёт здесь, а не своим
    циклом). У копии своё условие отправки: отчёт мог не уйти вовсе, а копия
    нужна в любом случае.
    """
    from telegram.constants import ParseMode
    from config import ADMIN_IDS
    from services import daily_report

    await asyncio.sleep(15)  # даём боту подняться (как у остальных циклов)
    logger.info("🚀 Запущен фоновый цикл суточного отчёта о расходах и ночной копии базы (00:00 по Киеву, владельцу в личку)")

    try:
        daily_report.start_snapshot_if_needed()
    except Exception as e:
        logger.error("⚠️ Не удалось поставить первый снимок счётчиков: %s", e)

    while True:
        try:
            if daily_report.period_closed():
                text = daily_report.midnight_report()
                if text:
                    for admin_id in ADMIN_IDS:
                        try:
                            await application.bot.send_message(
                                chat_id=admin_id, text=text, parse_mode=ParseMode.HTML
                            )
                        except Exception as e:
                            logger.warning("⚠️ Не удалось отправить суточный отчёт владельцу %s: %s", admin_id, e)
                    logger.info("📊 Суточный отчёт о расходах отправлен")

            # Недельный отчёт — ВТОРЫМ сообщением, сразу после суточного:
            # проверка идёт после него намеренно, чтобы последние закрытые
            # сутки успели попасть в копилку и в отчёт за неделю.
            if daily_report.week_closed():
                weekly = daily_report.weekly_report()
                if weekly:
                    for admin_id in ADMIN_IDS:
                        try:
                            await application.bot.send_message(
                                chat_id=admin_id, text=weekly, parse_mode=ParseMode.HTML
                            )
                        except Exception as e:
                            logger.warning("⚠️ Не удалось отправить недельный отчёт владельцу %s: %s", admin_id, e)
                    logger.info("📅 Недельный отчёт о расходах отправлен")
        except Exception as e:
            logger.error("⚠️ Не удалось подготовить отчёт о расходах: %s", e)

        # Ночная копия базы — СВОИМ try, отдельно от отчётов: сорвавшаяся
        # копия не должна мешать отчётам, а сорвавшийся отчёт — копии.
        try:
            await nightly_backup(application)
        except Exception as e:
            logger.error("⚠️ Ночная копия базы не сделана: %s", e)

        # 📊 Недельный дайджест группы — тоже своим try и тоже в этом цикле:
        # он единственный, кто умеет просыпаться по киевскому времени кусками
        # не больше часа. Уходит НЕ в полночь, а утром понедельника.
        try:
            await weekly_group_digest(application)
        except Exception as e:
            logger.error("⚠️ Недельный дайджест группы не отправлен: %s", e)

        # 🕛 Вопрос дня — тоже своим try и в этом же цикле, по той же причине,
        # что и дайджест: он просыпается по киевскому времени кусками не больше
        # часа, переживает спящий сервер и перевод часов. Уходит в каждый срок
        # из config.QUIZ_AUTO_HOURS (сейчас полдень и вечер).
        try:
            await daily_quiz(application)
        except Exception as e:
            logger.error("⚠️ Вопрос дня не отправлен: %s", e)

        try:
            delay = min(daily_report.seconds_to_next_midnight(), 3600)
        except Exception:
            delay = 3600
        await asyncio.sleep(delay)


# ───────────────────────────────────────────────
#  📊 Недельный дайджест группы (2026-08-04)
# ───────────────────────────────────────────────

async def weekly_group_digest(application) -> bool:
    """
    По понедельникам в 10:00 по Киеву присылает ВЛАДЕЛЬЦУ В ЛИЧКУ итоги
    недели по каждой известной группе: сколько сообщений и людей, топ
    активных, самый живой день и час пик, викторина, новости, участие бота,
    новички и работа DDoS-Guard. Возвращает True, если дайджест ушёл.

    ⚠️ В ГРУППУ САМ НЕ ПИШЕТ (решение Максима 2026-08-04). Под текстом стоит
    кнопка «📤 Отправить в группу» — отправляет человек, посмотрев на цифры.
    Автоматическую отправку в чат без отдельной просьбы не заводить.

    ⚠️ ЖИВЁТ В ЦИКЛЕ СУТОЧНОГО ОТЧЁТА, как и ночная копия базы: этот цикл
    уже умеет просыпаться по киевскому времени кусками не больше часа и
    переживать спящий сервер. Отсюда и час отправки — «первое пробуждение
    после 10:00 понедельника», а не ровно 10:00.

    ⚠️ Проспанный понедельник НЕ досылается (см. group_digest.due_now):
    дайджест — чтение к утру понедельника, в среду он уже не то же самое.
    Метка недели ставится ТОЛЬКО после удачной доставки — то же правило,
    что у ночной копии.
    """
    from telegram.constants import ParseMode
    from config import ADMIN_IDS
    from services import group_digest
    from database.history import get_known_chats
    from handlers.admin.panel_digest import digest_keyboard

    if not group_digest.due_now():
        return False

    chats = get_known_chats()
    if not chats:
        logger.info("📊 Дайджест недели: групп бот не знает — считать нечего")
        group_digest.note_sent()      # чтобы не пытаться каждый час до вторника
        return False

    delivered = 0
    for i, chat in enumerate(chats):
        title = chat.get("title") or str(chat["chat_id"])
        try:
            # save_quiz=True ТОЛЬКО у первой группы: снимок викторины один на
            # весь бот, и обновить его нужно ровно раз за отправку — иначе
            # вторая группа получила бы нулевую викторину.
            text = group_digest.build(chat["chat_id"], title, save_quiz=(i == 0),
                                      bot_id=application.bot.id)
        except Exception as e:
            logger.error("⚠️ 📊 Не удалось собрать дайджест группы %s: %s", title, e)
            continue
        for admin_id in ADMIN_IDS:
            try:
                # БЕЗ register_and_clean_bot_message — как отчёты о расходах:
                # это история, панели не должны её затирать.
                await application.bot.send_message(
                    chat_id=admin_id, text=text, parse_mode=ParseMode.HTML,
                    reply_markup=digest_keyboard(chat["chat_id"]),
                )
                delivered += 1
            except Exception as e:
                logger.warning("⚠️ Не удалось отправить дайджест владельцу %s: %s", admin_id, e)

    if delivered:
        group_digest.note_sent()
        logger.info("📊 Недельный дайджест группы отправлен владельцу (сообщений: %d)", delivered)
        return True
    logger.error("⚠️ 📊 Дайджест собран, но НЕ доставлен ни одному владельцу")
    return False


# ───────────────────────────────────────────────
#  🕛 Вопрос дня (2026-08-20)
# ───────────────────────────────────────────────

async def daily_quiz(application) -> bool:
    """
    В каждый срок расписания (config.QUIZ_AUTO_HOURS — 12:00 и 18:00 по Киеву)
    отправляет ОДИН вопрос викторины во ВСЕ группы, где есть бот, и удаляет там
    предыдущий опрос. Возвращает True, если хоть куда-то ушло.

    ⚠️ ПОРЯДОК ВАЖЕН: сначала отправляем новый, потом удаляем старый. Если
    новый почему-то не ушёл, предыдущий остаётся висеть — лучше вчерашний
    вопрос, чем пустое место (решение Максима 2026-08-20).

    ⚠️ Отметка срока ставится ТОЛЬКО после удачной отправки — как у дайджеста
    и ночной копии: не ушло никуда, значит бот попробует снова в ближайший час.

    ⚠️ Группы берём из known_chats (там только группы — их пишет архиватор
    сообщений). Подписка на новости тут ни при чём: на неё подписаны и личные
    чаты, а вопрос дня — групповая затея.

    Запись об отправленном опросе кладётся в settings (services/quiz_daily.py):
    ею же в следующий срок удаляется предыдущий опрос, и ею же ответы
    считаются после перезапуска бота.
    """
    from services import quiz_daily
    from database.history import get_known_chats, get_random_quiz_question, note_quiz_question_asked
    from handlers.quiz import send_quiz_question

    if not quiz_daily.due_now():
        return False

    chats = [c for c in get_known_chats() if c["chat_id"] < 0]
    if not chats:
        logger.info("🎮 Вопрос дня: групп бот не знает — отправлять некуда")
        return False

    # ⚠️ ВОПРОС БЕРЁТСЯ ОДИН РАЗ НА ВСЕ ГРУППЫ (2026-08-20, решение Максима).
    # Раньше каждый чат тянул свой: счётчик «сколько раз задавали» растёт между
    # отправками, и во вторую группу уходил следующий по очереди — банк
    # расходовался вдвое быстрее, чем идут дни. Отметку «задан» ставим ниже,
    # один раз и только если хоть куда-то ушло.
    question = get_random_quiz_question()
    if not question:
        logger.info("🎮 Вопрос дня: банк вопросов пуст — отправлять нечего")
        return False

    was = quiz_daily.active()
    delivered = 0
    for chat in chats:
        chat_id = chat["chat_id"]
        title = chat.get("title") or str(chat_id)
        try:
            record = await send_quiz_question(chat_id, application, auto=True,
                                              question=question)
        except Exception as e:
            logger.error("⚠️ 🎮 Вопрос дня не ушёл в группу %s: %s", title, e)
            continue
        if not record:
            # Отказ Telegram — предыдущий в этой группе НЕ трогаем.
            continue
        delivered += 1
        quiz_daily.remember(chat_id, record)

        # Предыдущий опрос убираем ПОСЛЕ удачной отправки нового. С двумя
        # сроками в сутках (2026-08-23) это значит, что полуденный вопрос
        # уходит в 18:00, а вечерний доживает до завтрашнего полудня — решение
        # Максима: в чате всегда висит ровно один вопрос.
        old = was.get(str(chat_id)) or {}
        old_msg = old.get("message_id")
        if old_msg:
            try:
                await application.bot.delete_message(chat_id=chat_id, message_id=old_msg)
                logger.info("🎮 Предыдущий вопрос удалён из группы %s", title)
            except Exception as e:
                # Права забрали, сообщение слишком старое, удалено руками —
                # не беда: новый вопрос уже на месте.
                logger.debug("🎮 Не удалось удалить предыдущий вопрос в %s: %s", title, e)
        old_poll = old.get("poll_id")
        if old_poll:
            # Ответы на удалённый опрос больше не придут — чистим память.
            try:
                from handlers.quiz import ACTIVE_QUIZZES
                ACTIVE_QUIZZES.pop(old_poll, None)
            except Exception:
                pass

    if delivered:
        # Отметка «вопрос задан» — одна на все группы, иначе банк тает быстрее
        # расписания. Ставится только после удачной отправки.
        try:
            note_quiz_question_asked(question["id"])
        except Exception as e:
            logger.warning("⚠️ 🎮 Не удалось отметить вопрос дня заданным: %s", e)
        quiz_daily.note_sent()
        logger.info("🎮 Вопрос дня разослан (групп: %d из %d)", delivered, len(chats))
        return True
    logger.error("⚠️ 🎮 Вопрос дня не доставлен НИ В ОДНУ группу (%d)", len(chats))
    return False


# ───────────────────────────────────────────────
#  💾 Ночная копия базы (2026-07-31)
# ───────────────────────────────────────────────

async def nightly_backup(application) -> bool:
    """
    Раз в сутки делает копию history.db и отправляет её ВЛАДЕЛЬЦУ в личку.
    Возвращает True, если копия в эту ночь была сделана.

    ⚠️ ЖИВЁТ ВНУТРИ ЦИКЛА СУТОЧНОГО ОТЧЁТА, а не своим циклом, и это
    осознанно. Во-первых, порядок: Максим выбрал получать копию СЛЕДОМ за
    отчётом, а два независимых цикла, проснувшихся в одну полночь, о порядке
    не договорятся. Во-вторых, этот цикл уже умеет всё, что нужно копии:
    просыпаться к полуночи по Киеву кусками не больше часа (переживая спящий
    компьютер и перевод часов) и догонять пропущенное после простоя.
    Заводить второй такой же цикл было бы копией сложной части ради простой.

    Своя метка «за какой день копия сделана» (services/backup.py) — потому
    что условие отправки отчёта тут не годится: отчёт мог не уйти вовсе,
    а копия нужна всё равно.

    Работа с диском (снимок + сжатие) уходит в отдельный поток: она тяжёлая
    по меркам бота, и на её время он не должен переставать отвечать людям.
    """
    from telegram.constants import ParseMode
    from config import ADMIN_IDS
    from services import backup

    if not backup.due_today():
        return False

    loop = asyncio.get_running_loop()
    path, size = await loop.run_in_executor(None, backup.make_backup)

    with open(path, "rb") as f:
        blob = f.read()
    name = os.path.basename(path)

    caption = (
        f"💾 <b>Копия базы бота</b>\n"
        f"<code>{html.escape(name)}</code> · {backup.human_size(size)}\n"
        f"<i>Сохрани у себя: в ней промпты, настройки, счета и квоты, личные "
        f"дела, звания и журналы — всего этого нет на GitHub. Если сервер "
        f"пропадёт, восстановиться можно только отсюда.</i>"
    )

    # ⚠️ БЕЗ register_and_clean_bot_message и БЕЗ schedule_delete. Панели
    # затирают друг друга, файлы логов самоудаляются через минуту — здесь
    # ни то, ни другое недопустимо: это и есть та самая последняя копия,
    # ради которой всё затевалось. Она обязана остаться в переписке навсегда.
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.send_document(
                chat_id=admin_id, document=blob, filename=name,
                caption=caption, parse_mode=ParseMode.HTML,
            )
            delivered += 1
        except Exception as e:
            logger.warning("⚠️ Не удалось отправить копию базы владельцу %s: %s", admin_id, e)

    if delivered:
        # Метку ставим ТОЛЬКО после удачной доставки: не дошло — на следующем
        # круге (через час) попробуем снова, а не махнём рукой до завтра.
        # То же правило, что у тревоги сторожа 2026-07-27.
        backup.note_done()
        logger.info("💾 Ночная копия базы отправлена владельцу: %s (%s)",
                    name, backup.human_size(size))
    else:
        logger.error("⚠️ Копия базы сделана (%s), но НЕ доставлена ни одному владельцу", name)

    # ── Статьи базы знаний вторым файлом (2026-08-12) ────────────────────
    # ⚠️ ЦЕЛИКОМ ПОД try, и метку дня не трогает. Копия базы — главное ради
    # чего этот цикл существует; она к этому моменту уже ушла, и упавший
    # архив статей не должен ни отменить её, ни заставить цикл слать базу
    # заново через час. Не собрался — строка в логе, и ждём следующей ночи.
    try:
        kb_path, kb_size, kb_ok, kb_wait = await loop.run_in_executor(None, backup.make_kb_backup)
        with open(kb_path, "rb") as f:
            kb_blob = f.read()
        kb_name = os.path.basename(kb_path)
        kb_delivered = 0
        for admin_id in ADMIN_IDS:
            try:
                await application.bot.send_document(
                    chat_id=admin_id, document=kb_blob, filename=kb_name,
                    caption=backup.kb_caption(kb_name, kb_size, kb_ok, kb_wait),
                    parse_mode=ParseMode.HTML,
                )
                kb_delivered += 1
            except Exception as e:
                logger.warning("⚠️ Не удалось отправить архив статей владельцу %s: %s", admin_id, e)
        if kb_delivered:
            logger.info("📚 Архив статей базы знаний отправлен владельцу: %s (%s)",
                        kb_name, backup.human_size(kb_size))
        else:
            logger.error("⚠️ Архив статей собран (%s), но НЕ доставлен ни одному владельцу", kb_name)
    except Exception as e:
        logger.error("⚠️ Архив статей базы знаний не собран: %s", e)

    return True
