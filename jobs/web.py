# ───────────────────────────────────────────────
#  jobs/web.py — веб-админка: седьмая фоновая задача (30.08.2026, этап 0)
#
#  Поднимает сайт ВНУТРИ процесса бота, на том же asyncio-цикле. Устройство
#  и причина такого решения — в config.py, блок «ВЕБ-АДМИНКА»; сами страницы
#  и адреса — в пакете web/.
#
#  ⚠️ Слушаем 127.0.0.1 — снаружи сайт виден только через Caddy, который
#  держит HTTPS. Задача НИЧЕГО не открывает в интернет сама.
#
#  ⚠️ Задача «тихая»: не поднялся сайт — бот работает дальше. Управление
#  кнопками в Telegram при этом никуда не девается, и это осознанный запас
#  прочности (решение Максима 30.08.2026: кнопки в боте остаются все).
# ───────────────────────────────────────────────

import asyncio
import logging

logger = logging.getLogger(__name__)


async def web_loop(application):
    """
    Держит сайт поднятым, пока живёт бот. Своего цикла у задачи нет: сервер
    работает событиями того же asyncio-цикла, а задача просто ждёт остановки,
    чтобы аккуратно закрыть сервер в post_shutdown.

    WEB_ENABLED=false (по умолчанию, то есть дома) — задача завершается сразу,
    как watchdog без URL или rag_catchup при выключенном RAG.
    """
    from config import WEB_ENABLED, WEB_HOST, WEB_PORT, WEB_PUBLIC_URL

    if not WEB_ENABLED:
        logger.info("🌐 Веб-админка выключена (WEB_ENABLED не задан в .env)")
        return

    try:
        from aiohttp import web as aioweb
        from web import build_app
    except ImportError as e:
        # Библиотеки нет — это не повод не запускать бота. Такое бывает после
        # обновления кода на сервере, где ещё не доехал pip install.
        logger.error("🌐 Веб-админка НЕ поднята: нет библиотеки (%s)", e)
        return

    # application нужен сайту ровно для одного действия — объявления
    # группам при выключении «Сам в разговор» (web/actions.py).
    runner = aioweb.AppRunner(build_app(application), access_log=None)
    try:
        await runner.setup()
        await aioweb.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    except Exception as e:
        logger.error("🌐 Веб-админка НЕ поднята: %s", e)
        await runner.cleanup()
        return

    where = WEB_PUBLIC_URL or f"http://{WEB_HOST}:{WEB_PORT}"
    logger.info("🌐 Веб-админка поднята на %s:%d — снаружи %s",
                WEB_HOST, WEB_PORT, where)
    if not WEB_PUBLIC_URL:
        logger.warning("🌐 WEB_PUBLIC_URL не задан: кнопка «Админка» в боте "
                       "не появится, вход через браузер работать не будет")

    try:
        # Ждём вечно. Останов приходит отменой задачи из post_shutdown —
        # ровно так же, как у остальных шести фоновых задач.
        await asyncio.Event().wait()
    finally:
        # ⚠️ shield: cleanup сам ждёт закрытия соединений, а мы уже отменены —
        # без защиты отмена прилетит в него же, и сервер останется висеть.
        await asyncio.shield(runner.cleanup())
        logger.info("🌐 Веб-админка остановлена")
