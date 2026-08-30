# ───────────────────────────────────────────────
#  web/longjobs.py — долгие работы, запущенные с сайта (30.08.2026, этап 4).
#
#  Две операции идут минутами: пересборка базы знаний и сборка вопросов
#  викторины. Держать на них страницу нельзя — браузер отвалится по таймауту,
#  а работа продолжится вслепую.
#
#  ⚠️ ЗАЩЁЛКИ ТЕ ЖЕ, ЧТО У КНОПОК В БОТЕ (`kb_rebuild_running`,
#  `quiz_gen_running` в `bot_data`). Это не мелочь: два одновременных прогона
#  по одним и тем же файлам и одной базе — верный способ получить половину
#  индекса и вопросы-дубли. Заведи сайт свою защёлку, и кнопка в боте о ней
#  бы не знала.
#
#  ⚠️ Работа уходит в РАБОЧИЙ ПОТОК (run_in_executor), а не выполняется в
#  цикле бота: обе операции синхронные и сетевые, и в цикле они заморозили бы
#  ответы всем.
# ───────────────────────────────────────────────

import asyncio
import logging

logger = logging.getLogger(__name__)

# Куда кладём итог последней работы, чтобы страница его показала.
_RESULT_SUFFIX = "_web_result"


def is_running(application, latch: str) -> bool:
    """Идёт ли сейчас эта работа — хоть с сайта, хоть по кнопке в боте."""
    if application is None:
        return False
    return bool(application.bot_data.get(latch))


def last_result(application, latch: str) -> str:
    """Что вышло в прошлый раз. Пустая строка — итога ещё не было."""
    if application is None:
        return ""
    return application.bot_data.get(latch + _RESULT_SUFFIX, "")


def forget_result(application, latch: str) -> None:
    """Убирает прошлый итог — чтобы он не висел на странице вечно."""
    if application is not None:
        application.bot_data.pop(latch + _RESULT_SUFFIX, None)


def start(application, latch: str, work, describe) -> str:
    """
    Запускает долгую работу в фоне.

    work     — синхронная функция без аргументов (уйдёт в рабочий поток);
    describe — функция, превращающая её результат в строку для человека;
               получает None, если работа сорвалась.

    Возвращает строку о том, что вышло с ЗАПУСКОМ (не с самой работой).
    """
    if application is None:
        return "Нет доступа к боту — работа не запущена."
    if is_running(application, latch):
        return "⏳ Уже идёт — дождитесь итога."

    async def _run():
        result = None
        try:
            result = await asyncio.get_running_loop().run_in_executor(None, work)
        except Exception as e:
            logger.error("🌐 Долгая работа «%s» упала: %s", latch, e)
        finally:
            application.bot_data[latch] = False
        try:
            application.bot_data[latch + _RESULT_SUFFIX] = describe(result)
        except Exception as e:
            logger.warning("⚠️ Не удалось описать итог работы «%s»: %s", latch, e)
            application.bot_data[latch + _RESULT_SUFFIX] = "Работа закончилась, итог непонятен."

    application.bot_data[latch] = True
    forget_result(application, latch)
    application.create_task(_run())
    return "⏳ Запущено. Страница сама покажет итог — она обновляется."
