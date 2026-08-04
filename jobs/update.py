# ───────────────────────────────────────────────
#  jobs/update.py — Самообновление: бот сам забирает новый код с GitHub
#
#  Выделен из jobs.py 2026-08-04 разрезом БЕЗ изменения логики
#  (файл был на 837 строк и держал шесть независимых циклов).
#  Забирает только в тишину — перезапуск оборвал бы разговор.
# ───────────────────────────────────────────────

import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────
#  Самообновление: бот сам забирает новый код
# ───────────────────────────────────────────────

async def auto_update_loop(application):
    """
    Раз в AUTO_UPDATE_INTERVAL_SEC (10 минут) спрашивает GitHub, нет ли новой
    версии, и если есть — забирает её и перезапускается. Решение Максима
    2026-07-27.

    ⚠️ БОТ ПРИ ЭТОМ НЕ ЗАМИРАЕТ: сама проверка и скачивание выполняются в
    отдельном потоке (services.deploy.update → run_in_executor), поэтому люди
    в чатах продолжают получать ответы. Единственная пауза — сам перезапуск.

    ⚠️ ЖДЁМ ТИШИНЫ. Перезапуск обрывает разговор: тот, кто ждал ответа, его не
    получит. Поэтому проверка вообще не начинается, пока в чатах не станет тихо
    AUTO_UPDATE_QUIET_SEC секунд (решение Максима: минута). Шумно — молча ждём
    следующего круга. Тишину проверяем ДО обновления, а не после: иначе на
    диске лежал бы новый код, а в памяти работал старый.

    Дома цикл завершается сразу (can_update() = False): там лежат ещё не
    отправленные правки Максима, и скачивание дралось бы с его работой.
    """
    from config import (ADMIN_IDS, AUTO_UPDATE_ENABLED_DEFAULT, AUTO_UPDATE_INTERVAL_SEC,
                        AUTO_UPDATE_QUIET_SEC, AUTO_UPDATE_TICK_SEC)
    from config import UPDATE_NOTICE_MSGS_KEY
    from database.history import get_setting, set_setting
    from services import deploy

    await asyncio.sleep(20)  # даём боту подняться (как у остальных циклов)

    if not deploy.can_update():
        logger.info("⬇️ Самообновление выключено: здесь код с GitHub не забирается")
        return

    logger.info("⬇️ Запущен фоновый цикл самообновления (проверка каждые %d мин, "
                "перезапуск только при тишине %d сек)",
                AUTO_UPDATE_INTERVAL_SEC // 60, AUTO_UPDATE_QUIET_SEC)

    async def _forget_previous_notice() -> None:
        """
        Стирает ПРЕДЫДУЩЕЕ сообщение об обновлении (2026-08-04, просьба
        Максима: «пусть удаляет старое своё сообщение об обновлении»). Иначе
        в личке владельца копится лента одинаковых уведомлений — бот
        обновляется тем чаще, чем чаще идут правки.

        Координаты лежат в settings (`UPDATE_NOTICE_MSGS_KEY`), а не в памяти:
        сразу после «⬇️ Обновился сам…» бот ПЕРЕЗАПУСКАЕТСЯ, и память не
        переживает даже собственного сообщения.

        Тихая: сообщение мог удалить сам владелец, а старше 48 часов Telegram
        ботам удалять не даёт — оба случая штатные, ругаться на них нечего.
        ⚠️ Гигиену панелей (`register_and_clean_bot_message`) здесь применять
        НЕЛЬЗЯ: она стирает ПОСЛЕДНЕЕ сообщение бота в чате, каким бы оно ни
        было, — уведомление об обновлении снесло бы открытую админ-панель.
        Поэтому у него свой, отдельный след.
        """
        raw = get_setting(UPDATE_NOTICE_MSGS_KEY, "")
        if not raw:
            return
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            items = []
        for pair in items:
            try:
                chat_id, message_id = pair
                await application.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception as e:
                logger.debug("⬇️ Не удалось удалить прошлое уведомление %s: %s", pair, e)

    async def _notify(text: str) -> None:
        # Сначала убираем прошлое уведомление, потом шлём новое: если бот
        # не достучится до Телеграма вовсе, старое хотя бы уже стёрто и
        # ленты не будет.
        await _forget_previous_notice()
        sent = []
        # Ошибка отправки одному владельцу не мешает остальным (как в итогах месяца)
        for admin_id in ADMIN_IDS:
            try:
                msg = await application.bot.send_message(chat_id=admin_id, text=text)
                if msg:
                    sent.append([admin_id, msg.message_id])
            except Exception as e:
                logger.warning("⚠️ Не удалось сообщить об обновлении %s: %s", admin_id, e)
        # ⚠️ ЗАПИСЫВАЕМ ДО ПЕРЕЗАПУСКА — вызывающий код останавливает бота
        # сразу после этой строки, и всё несохранённое пропадёт.
        set_setting(UPDATE_NOTICE_MSGS_KEY, json.dumps(sent, ensure_ascii=False))

    last_check = time.monotonic()
    while True:
        await asyncio.sleep(AUTO_UPDATE_TICK_SEC)
        try:
            if get_setting("auto_update_enabled", AUTO_UPDATE_ENABLED_DEFAULT) != "1":
                continue
            if time.monotonic() - last_check < AUTO_UPDATE_INTERVAL_SEC:
                continue
            if deploy.quiet_for() < AUTO_UPDATE_QUIET_SEC:
                continue  # в чатах разговор — подождём, перезапуск его оборвёт

            last_check = time.monotonic()
            # quiet_nochange: «нового кода нет» в лог не пишем — проверка идёт
            # каждые 10 минут, и эта строка забивала бы лог целиком. Все прочие
            # исходы (обновился, откатился, не достучался) пишутся как обычно.
            res = await deploy.update(quiet_nochange=True)
            status = res.get("STATUS")

            if status == "UPDATED":
                await _notify(f"⬇️ Обновился сам: {res.get('MSG', '')}\nПерезапускаюсь…")
                logger.info("⬇️ Самообновление: перезапуск на %s", res.get("VERSION"))
                # Тот же путь, что у кнопки: помечаем остановку перезапуском
                # (post_stop промолчит) и просим библиотеку остановиться —
                # поднимет systemd.
                application.bot_data["shutdown_reason"] = "restart"
                application.stop_running()
                return
            if status == "ROLLBACK":
                await _notify(f"⚠️ Самообновление: {res.get('MSG', '')}\n"
                              f"Бот продолжает работать на прежней версии.")
            # NOCHANGE и NETFAIL — молча, только в логе (решение Максима:
            # 144 сообщения в сутки «нового нет» никому не нужны).
        except Exception as e:
            logger.error("⚠️ Цикл самообновления споткнулся: %s", e)
