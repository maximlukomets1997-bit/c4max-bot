import logging
import asyncio
import sys
import os
import shutil
import atexit
import subprocess
import traceback

from telegram.error import NetworkError, TimedOut

# Настраиваем логирование ДО импорта модулей бота, чтобы ловить всё с самого старта.
# archive_old_logs зовётся позже, в main() — уже после остановки старой копии.
from logging_setup import setup_logging, archive_old_logs
setup_logging()

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeDefault, BotCommandScopeAllChatAdministrators
from telegram.ext import ApplicationBuilder

from config import TELEGRAM_TOKEN, ADMIN_IDS, GEMINI_API_KEY, RAG_ENABLED, IS_DOCKER, DB_PATH
from database.history import init_db
from handlers import setup_handlers
from utils import register_and_clean_bot_message
from jobs import news_polling_loop
from jobs import cleanup_loop
from jobs import rag_catchup_loop
from jobs import daily_report_loop
from jobs import watchdog_loop

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Защита от двойного запуска через PID-файл (Выключается, если IS_DOCKER=True)
# ─────────────────────────────────────────────────────────────────────────────
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.pid")


def _write_pid() -> None:
    """Записывает PID текущего процесса в файл."""
    if IS_DOCKER:
        return
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def _remove_pid() -> None:
    """Удаляет PID-файл при выходе."""
    if IS_DOCKER:
        return
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def _kill_existing_instance() -> None:
    """
    Если bot.pid существует — пытается завершить старый процесс.
    Безопасно обрабатывает права доступа и не удаляет PID при ошибках остановки.
    """
    if IS_DOCKER or not os.path.exists(PID_FILE):
        return

    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            old_pid = int(f.read().strip())
    except (ValueError, IOError):
        _remove_pid()
        return

    if old_pid == os.getpid():
        _remove_pid()
        return

    logger.info("🚀 Найдена старая копия бота — выключаю её, чтобы не работали две сразу")

    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(old_pid)],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                logger.info("🚀 Старая копия бота выключена")
                _remove_pid()
            else:
                logger.warning(f"⚠️ Не удалось закрыть старый процесс через taskkill: {result.stderr.strip()}")
        else:
            import signal
            os.kill(old_pid, signal.SIGTERM)
            logger.info(f"🚀 Старый процесс PID {old_pid} получил сигнал остановки")
            _remove_pid()

        # Небольшая пауза — даём старому процессу время на полное завершение
        import time
        time.sleep(2)
            
    except ProcessLookupError:
        logger.info(f"🚀 Старый процесс PID {old_pid} уже не работает — очищаю устаревший PID-файл")
        _remove_pid()
    except PermissionError as e:
        logger.error(f"⚠️ Не удалось закрыть старый процесс PID {old_pid} (нет прав): {e} — PID-файл НЕ удалён")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось остановить старый процесс: {e}")


def _seed_db_if_missing() -> None:
    """
    Первый запуск на новом месте: базы ещё нет, но рядом лежит seed.db из
    репозитория. Копируем её — и бот стартует сразу с характером: промпты,
    настройки, права персонала, подписки на новости, карточки участников.

    Личных переписок в seed.db нет — только настройки и справочные данные
    (её собирает отдельный скрипт, а не бот).

    ⚠️ Если база УЖЕ есть — не трогаем ничего. Накопленное на сервере важнее
    заготовки из репозитория, и «обновить настройки из seed.db» здесь не
    делается специально: это молча затирало бы правки, сделанные через
    админ-панель.
    """
    if os.path.exists(DB_PATH):
        return

    seed = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed.db")
    if not os.path.exists(seed):
        return

    try:
        # Сначала убеждаемся, что заготовка вообще похожа на базу: битый или
        # обрезанный файл иначе доедет до init_db и уронит бота на старте
        # сообщением про «malformed database» — на чужом сервере это больно.
        # immutable=1 — читаем, ничего не создавая рядом. Без него SQLite у
        # базы в режиме WAL заводит файлы -shm/-wal просто на чтении, и они
        # мусорят в папке проекта (наступили 2026-07-27: уехали на GitHub).
        import sqlite3
        probe = sqlite3.connect("file:%s?immutable=1" % seed.replace("?", "%3f"), uri=True)
        try:
            probe.execute("SELECT COUNT(*) FROM settings").fetchone()
        finally:
            probe.close()
    except Exception as e:
        logger.warning("⚠️ Заготовка seed.db повреждена или это не база (%s) — "
                       "стартую с пустой базой", e)
        return

    try:
        shutil.copyfile(seed, DB_PATH)
        logger.info("🚀 База не найдена — развёрнута заготовка из seed.db "
                    "(промпты, настройки, персонал, подписки)")
    except Exception as e:
        logger.warning("⚠️ Не удалось развернуть seed.db: %s — стартую с пустой базой", e)


def _restart_self() -> None:
    """
    Запускает новую копию бота (кнопка перезапуска из админ-панели).
    Вызывается из main() уже ПОСЛЕ полной остановки run_polling, поэтому
    две копии никогда не опрашивают Telegram одновременно.

    В Windows новая копия получает СВОЁ окно консоли (CREATE_NEW_CONSOLE,
    решение Максима 2026-07-24 — вернули прежнее поведение): открывается
    чистое окно, а старое закрывается вместе с завершающимся процессом.
    Без флага Windows отдаёт копии окно родителя, и логи всех перезапусков
    копятся в одном окне сплошной лентой. На файлы логов это не влияло
    никогда — там каждый запуск пишет свой файл.
    Флаг существует ТОЛЬКО в Windows: на Linux и в Docker его передавать
    нельзя (ValueError), поэтому проверка sys.platform обязательна.

    PID-файл удаляется заранее, а его удаление при выходе отменяется: дальше
    файл принадлежит новой копии, и она не должна пытаться завершать текущий
    (уже завершающийся) процесс.

    В управляемой среде (IS_DOCKER=true: панель хостинга, Docker, systemd)
    новая копия НЕ запускается — процесс просто завершается, и поднять его
    должен управляющий. Порождать копию там нельзя: она становится дочерним
    процессом умирающего родителя, панель считает бота остановленным и
    добивает сироту вместе с ним. На Linux это поведение включено по
    умолчанию (см. config.py::IS_DOCKER).
    """
    if IS_DOCKER:
        logger.info("🚀 Перезапуск: процессом управляет хостинг — просто завершаюсь, "
                    "поднять новую копию должен он")
        return

    atexit.unregister(_remove_pid)
    _remove_pid()
    script_path = os.path.abspath(__file__)
    # Отдельное окно консоли — только в Windows (см. докстринг).
    extra = {"creationflags": subprocess.CREATE_NEW_CONSOLE} if sys.platform == "win32" else {}
    try:
        subprocess.Popen([sys.executable, script_path],
                         cwd=os.path.dirname(script_path), **extra)
        logger.info("🚀 Перезапуск: новая копия бота запущена в новом окне"
                    if extra else "🚀 Перезапуск: новая копия бота запущена")
    except Exception as e:
        logger.error("⚠️ Не удалось запустить новую копию бота: %s", e)

# ─────────────────────────────────────────────────────────────────────────────

def _staff_recipients() -> list:
    """
    Кому слать служебные сообщения о запуске/остановке: владельцы + модераторы,
    у которых есть хоть одно право (модератору без прав панель бесполезна, и
    дёргать его при каждом перезапуске незачем).

    ВАЖНО: это ТОЛЬКО про запуск и остановку. Итоги месяца, расходы, сбои
    моделей и состояние базы знаний остаются владельцам (jobs.py, gemini.py).
    """
    ids = list(ADMIN_IDS)
    try:
        from services.roles import list_moderators, has_any_perm
        ids += [uid for uid in list_moderators()
                if uid not in ADMIN_IDS and has_any_perm(uid)]
    except Exception as e:
        logger.debug("⚠️ Не удалось получить список модераторов: %s", e)
    return ids


def _friendly_error(err: Exception) -> str:
    """Переводит стандартные ошибки Telegram на русский."""
    msg = str(err)
    if "bot was blocked by the user" in msg:
        return "пользователь заблокировал бота"
    if "bot can't initiate conversation" in msg:
        return "пользователь не начинал диалог с ботом"
    if "user is deactivated" in msg:
        return "аккаунт пользователя удалён"
    if "chat not found" in msg:
        return "чат не найден"
    if "bot was kicked" in msg or "bot is not a member" in msg:
        return "бот исключён из чата"
    return msg


async def _notify_admins(bot, text: str):
    """
    Шлёт короткое служебное сообщение всему персоналу в личку (chat_id == их user_id).
    Идёт через register_and_clean_bot_message — поэтому новое служебное сообщение
    УДАЛЯЕТ предыдущее «панельное» сообщение бота в этом чате (так «🔄 перезапуск…»,
    «🛑 остановлен», «✅ запущен» сменяют друг друга, не копясь). Хранилище в SQLite,
    поэтому работает и между перезапусками (разными процессами).
    Любая ошибка по отдельному человеку не должна мешать запуску/остановке — глушим.
    """
    for admin_id in _staff_recipients():
        try:
            sent = await bot.send_message(chat_id=admin_id, text=text)
            if sent:
                await register_and_clean_bot_message(bot, admin_id, sent.message_id)
        except Exception as e:
            logger.warning("⚠️ Не удалось уведомить %s: %s", admin_id, _friendly_error(e))


async def post_init(application):
    commands = [
        BotCommand("start", "Начать работу / перезапуск"),
        BotCommand("help", "Помощь и список команд"),
        BotCommand("imagine", "Сгенерировать картинку"),
        BotCommand("rank", "Моя статистика"),
        BotCommand("subscribe", "Подписаться на новости C4_Max"),
        BotCommand("clear", "Очистить текущий контекст диалога"),
    ]
    await application.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    # Публичные команды видны всем в личке и в группах.
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())

    # В меню показываем ОДНУ админ-команду — /adm (главная панель, из неё
    # открываются статистика и модерация). Остальные админ-команды (/stats,
    # /mod, unmute, prompt_*, news_prompt_*) остаются рабочими, но в меню
    # не светятся. /adm стоит В САМОМ ВЕРХУ меню (порядок в меню Telegram =
    # порядок в списке set_my_commands).
    adm_first = [
        BotCommand("adm", "Админ-панель (Админ)"),
    ]
    # ВАЖНО: админ-команды НЕ должны светиться в меню групп. Групповой
    # админ-scope явно перезаписываем публичным списком (на случай, если
    # прошлый деплой что-то туда записал) — иначе чат-админы видели бы /stats и т.п.
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllChatAdministrators())
    # Админ-команды показываем ТОЛЬКО в личке ПЕРСОНАЛА — владельцев и
    # модераторов (в приватном чате chat_id == user_id). Модератору /adm
    # откроет его панель, собранную по правам; без строки в меню он бы просто
    # не узнал, что панель существует. Список персонала обновляется и в момент
    # выдачи прав — см. handlers/admin/panel_users.py::_sync_staff_menu.
    try:
        from services.roles import list_moderators
        for staff_id in list(ADMIN_IDS) + list_moderators():
            try:
                await application.bot.set_my_commands(
                    adm_first + commands,
                    scope=BotCommandScopeChat(chat_id=staff_id)
                )
            except Exception as e:
                logger.debug("⚠️ Меню команд для %s не установлено: %s", staff_id, e)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось установить админские команды: {e}")

    # Сохраняем хэндлы фоновых задач, чтобы корректно отменить их при остановке
    # (иначе при Ctrl+C asyncio пишет «Task was destroyed but it is pending!»).
    application.bot_data["background_tasks"] = [
        asyncio.create_task(news_polling_loop(application)),
        # application нужен циклу очистки для месячного сброса статистики API
        # (отправка «Итогов месяца» админам в личку).
        asyncio.create_task(cleanup_loop(application)),
        # Ежечасный добор базы знаний после сбоев лимита Google;
        # при выключенном RAG задача сама завершается сразу.
        asyncio.create_task(rag_catchup_loop(application)),
        # Суточный отчёт о расходах: в 00:00 по Киеву владельцу в личку
        # (application нужен для отправки сообщения).
        asyncio.create_task(daily_report_loop(application)),
        # «Отметки живости» внешнему сторожу (healthchecks.io): по их пропаже
        # владелец узнаёт о выключенном компьютере, пропавшем интернете или
        # умершем боте. WATCHDOG_URL не задан в .env — задача завершается сразу.
        asyncio.create_task(watchdog_loop(application)),
    ]

    # Сообщаем админам, что бот поднялся (после обычного старта И после перезапуска
    # кнопкой — новый процесс всегда проходит через post_init).
    await _notify_admins(application.bot, "✅ Бот запущен и готов к работе.")

    # Сразу за этим автоматически показываем каждому из персонала его панель.
    # Панель отправляется через тот же механизм очистки, поэтому она УДАЛЯЕТ
    # только что отправленное «✅ Бот запущен…» — в итоге остаётся готовая
    # к работе панель. У модератора она собрана по его правам (send_adm_panel
    # спрашивает services/roles.py) — второй аргумент обязателен, иначе в личке
    # panel возьмёт chat_id и получится то же самое, но полагаться на это не надо.
    try:
        from handlers.admin import send_adm_panel
        for staff_id in _staff_recipients():
            try:
                await send_adm_panel(application.bot, staff_id, staff_id)
            except Exception as e:
                logger.warning("⚠️ Не удалось показать панель %s: %s", staff_id, _friendly_error(e))
    except Exception as e:
        logger.warning("⚠️ Не удалось автоматически показать /adm при запуске: %s", e)

    logger.info("🚀 Бот запущен и готов к работе")


async def post_stop(application):
    """
    Вызывается python-telegram-bot ПОСЛЕ остановки, но ДО закрытия бота
    (bot ещё «живой», сообщение успеет уйти). Сюда попадаем и при Ctrl+C,
    и при SIGTERM от кнопки перезапуска.

    Если остановка — это перезапуск кнопкой (флаг shutdown_reason == "restart"),
    молчим: в панели уже показан текст перезапуска, а новый процесс сам сообщит
    о запуске. Иначе это ручная остановка (Ctrl+C) — честно сообщаем об этом.
    """
    if application.bot_data.get("shutdown_reason") == "restart":
        return

    # ── «Крик умирающего» (2026-07-27, решение Максима) ──
    # Сообщаем внешнему сторожу, что уходим НАМЕРЕННО, — он поднимет тревогу
    # СРАЗУ, не выжидая Period + Grace. Иначе об остановке (в том числе при
    # выключении компьютера) владелец узнавал бы только по таймауту.
    # Стоит ПЕРВЫМ: если Windows выключается, у процесса считанные секунды,
    # и короткий запрос успеет уйти скорее, чем отправка в Telegram.
    # ⚠️ Сюда мы не попадаем при перезапуске кнопкой (проверка выше) — иначе
    # каждый перезапуск оборачивался бы ложной тревогой.
    # Честная оговорка: при закрытии окна крестиком и при выключении Windows
    # Python не всегда успевает отработать этот хук — тогда сработает обычный
    # срок. Крик — ускорение, а не гарантия.
    _cry_for_help()

    try:
        await _notify_admins(application.bot, "🛑 Бот остановлен вручную.")
    except Exception as e:
        logger.warning("⚠️ Не удалось отправить уведомление об остановке: %s", e)


def _cry_for_help() -> None:
    """
    Короткий запрос на <адрес сторожа>/fail — «я останавливаюсь намеренно».
    Синхронный и с крошечным таймаутом: висеть на остановке нельзя, а времени
    при выключении компьютера может не быть вовсе. Все ошибки гасятся —
    невозможность крикнуть не должна мешать боту выключаться.
    """
    from config import WATCHDOG_URL
    if not WATCHDOG_URL:
        return
    try:
        from services.http import session
        session().get(WATCHDOG_URL.rstrip("/") + "/fail", timeout=5)
        logger.info("🐕 Сторожу отправлен сигнал об остановке — тревога придёт сразу")
    except Exception as e:
        logger.warning("🐕 Не удалось предупредить сторожа об остановке: %s", e)


async def post_shutdown(application):
    """
    Вызывается python-telegram-bot при остановке Application (в т.ч. по Ctrl+C),
    пока цикл событий ещё жив. Отменяем фоновые задачи и дожидаемся их завершения,
    чтобы не оставалось «висящих» задач.
    """
    tasks = application.bot_data.get("background_tasks", [])
    for task in tasks:
        task.cancel()
    if tasks:
        # return_exceptions=True — собираем CancelledError, не поднимая их наверх
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("🚀 Фоновые задачи остановлены")

    # Соединение с базой одно на весь процесс (2026-07-27) — закрываем его здесь,
    # после остановки фоновых задач: пока они живы, они ещё могут писать.
    # Закрытие сворачивает WAL-файл в саму базу; «тихое» — сбой не мешает выходу.
    try:
        from database.history import close_db
        close_db()
    except Exception as e:
        logger.debug("🚀 Не удалось закрыть базу при остановке: %s", e)


async def _error_handler(update, context) -> None:
    """Глушит шум от сетевых сбоев; всё остальное логирует как ошибку."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("⚠️ Временная сетевая ошибка: %s", err)
        return
    logger.error("⚠️ Необработанное исключение: %s\n%s", err, "".join(traceback.format_exception(type(err), err, err.__traceback__)))


def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.error("⚠️ TELEGRAM_TOKEN не установлен — проверьте файл .env")
        sys.exit(1)

    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        logger.error("⚠️ GEMINI_API_KEY не установлен — проверьте файл .env")
        sys.exit(1)

    # ── Защита от двойного запуска (активна только вне Docker) ───────────────
    if not IS_DOCKER:
        _kill_existing_instance()   
        _write_pid()                
        atexit.register(_remove_pid)  
    else:
        logger.info("🚀 Процессом управляет хостинг (IS_DOCKER=true) — "
                    "защита через bot.pid отключена, перезапуском занимается он")
    # ────────────────────────────────────────────────────────────────────────

    # Логи прошлых запусков → в общий архив logs/archive.log. ИМЕННО ЗДЕСЬ:
    # старая копия уже остановлена и свой файл дописала до конца.
    archive_old_logs()

    # Новое место (свежий сервер) — развернуть заготовку базы из репозитория.
    # ОБЯЗАТЕЛЬНО до init_db(): та создаёт пустую базу, и заготовка бы не пригодилась.
    _seed_db_if_missing()

    init_db()

    # Персональные настройки участников (карточка пользователя, /users) —
    # в память ОДИН раз при старте: их спрашивают на каждое сообщение группы,
    # ходить за ними в БД на горячем пути нельзя. Дальше кэш обновляет сама
    # панель при каждой правке (services/user_settings.py::set_field).
    try:
        import services.user_settings as user_settings
        user_settings.load()
    except Exception as e:
        logger.error("⚠️ Не удалось загрузить персональные настройки: %s", e)

    # Модераторы и их права — тоже в память при старте: право проверяется на
    # КАЖДОЕ нажатие кнопки и каждую команду (services/roles.py).
    try:
        import services.roles as roles
        roles.load()
    except Exception as e:
        logger.error("⚠️ Не удалось загрузить модераторов: %s", e)

    if RAG_ENABLED:
        try:
            import services.rag as rag_module
            logger.info("🚀 Загрузка базы знаний RAG")
            rag_module.sync_knowledge_base()
        except Exception as e:
            logger.error(f"⚠️ Не удалось инициализировать RAG: {e}")

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .build()
    )
    setup_handlers(application)
    application.add_error_handler(_error_handler)

    logger.info("🚀 Бот запускается — для остановки нажмите Ctrl+C")
    
    try:
        application.run_polling()
    except (KeyboardInterrupt, SystemExit):
        logger.info("🚀 Получен сигнал остановки — завершаю работу служб бота")
    finally:
        # Кнопка перезапуска в админ-панели выставляет флаг shutdown_reason
        # и корректно останавливает run_polling (Application.stop_running) —
        # к этому моменту все хуки остановки уже отработали.
        restart_requested = application.bot_data.get("shutdown_reason") == "restart"
        if restart_requested:
            _restart_self()
        logger.info("🚀 Бот полностью остановлен")

        # ⚠️ ВЫХОД С КОДОМ ОШИБКИ — НАМЕРЕННО (2026-07-27, хостинг UkrLine).
        # В управляемой среде новую копию поднимает панель, но она делает это
        # только после СБОЯ: корректный выход (код 0) она считает за «остановлен
        # вручную» и бота не поднимает. Проверено живьём: кнопка гасила бота
        # насмерть. Ненулевой код — единственный способ сказать «это не я решил
        # уйти, подними меня обратно».
        # Обычную остановку (Ctrl+C, кнопка «Стоп» в панели) это не затрагивает:
        # там shutdown_reason не выставлен, и выход остаётся нулевым.
        if restart_requested and IS_DOCKER:
            logger.info("🚀 Выхожу с кодом 1 — так хостинг поймёт, что копию надо поднять")
            sys.exit(1)


if __name__ == '__main__':
    main()