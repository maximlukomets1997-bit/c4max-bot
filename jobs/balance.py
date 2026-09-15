# ───────────────────────────────────────────────
#  jobs/balance.py — сверка остатка на счету с платформой провайдера
#  (2026-09-14, решение Максима).
#
#  ЗАЧЕМ. Остаток бот считал сам: вычитал из него стоимость каждого запроса.
#  Стоимость берётся из отчёта о токенах, который провайдер присылает В КОНЦЕ
#  ответа, — а если поток оборвался (потолок GEMINI_STREAM_DEADLINE, 90 с),
#  отчёта нет, хотя деньги уже списаны. Каждый обрыв тихо занижал и расход,
#  и остаток. Замер 14.09.2026: 4 обрыва из 114 запросов, отставание от
#  платформы — 4.7 цента.
#
#  ⚠️ СВЕРЯЕТСЯ ПОКА ТОЛЬКО DeepSeek, и это не забывчивость: остаток по своему
#  адресу отдаёт именно он. Проверит кто-то такой же запрос у Xiaomi — сюда
#  добавится вторая строка списка, а не второй цикл.
#
#  ⚠️ ЭКРАНЫ НЕ ТРОГАЛИСЬ РАДИ ЭТОГО ЦИКЛА. Он пишет настоящий остаток в тот же
#  ключ settings, откуда его и так читают панель API, «Счета и квоты» и
#  суточный отчёт. Что делать с расхождением, решает database.money —
#  plan_balance_sync.
# ───────────────────────────────────────────────

import logging
import asyncio

logger = logging.getLogger(__name__)

#  Кого сверяем: имя провайдера в реестре config.PROVIDERS → чем спросить
#  остаток. Функция синхронная (requests), зовётся через run_in_executor,
#  как все сетевые вызовы бота.
def _providers():
    from services.gemini import fetch_deepseek_balance
    return {"deepseek": fetch_deepseek_balance}


async def sync_balances_once() -> None:
    """
    Один проход сверки по всем провайдерам, которые умеют отдавать остаток.

    ⚠️ ПЛАТФОРМА НЕ ОТВЕТИЛА — НИЧЕГО НЕ ТРОГАЕМ. Ни обнуления, ни «примерно
    столько»: прежняя цифра и прежнее время сверки остаются на месте, в лог
    идёт строка. Отметка «сверено» под несвежим числом хуже, чем отсутствие
    отметки.
    """
    from config import BALANCE_SYNC_EPSILON
    import database.history as hist

    loop = asyncio.get_running_loop()
    for provider, ask in _providers().items():
        try:
            real = await loop.run_in_executor(None, ask)
        except Exception as e:                      # сеть, ключ, что угодно
            logger.warning("⚠️ Сверка остатка %s сорвалась: %s", provider, e)
            continue
        if real is None:
            continue                                # причину уже написал сам запрос

        try:
            new_balance, added, reason = hist.sync_provider_balance(
                provider, real, BALANCE_SYNC_EPSILON)
        except Exception as e:
            logger.error("⚠️ Остаток %s не записать: %s", provider, e)
            continue

        if reason == "недостача":
            logger.info("💰 Сверка %s: остаток $%.6f (было больше на $%.6f — "
                        "эти деньги списаны за оборванные ответы, доначислил в расход)",
                        provider, new_balance, added)
        elif reason == "пополнение":
            logger.info("💰 Сверка %s: остаток $%.6f (больше нашего — счёт пополнен, "
                        "расход не трогаю)", provider, new_balance)
        # «совпало» в лог не пишем: это рутина каждой сверки, а не событие.


async def balance_sync_loop(application):
    """
    Раз в 10 минут спрашивает у платформы настоящий остаток на счету.

    Первая сверка — почти сразу после запуска (пауза 15 секунд, как у
    остальных циклов: даём боту подняться). Иначе после каждого перезапуска
    экраны до 10 минут показывали бы собственную оценку.

    Ошибка одного провайдера не роняет цикл и не мешает остальным: каждый
    разбирается внутри sync_balances_once.
    """
    from config import DEEPSEEK_BALANCE_SYNC_SEC

    await asyncio.sleep(15)
    while True:
        try:
            await sync_balances_once()
        except Exception as e:
            logger.error("⚠️ Цикл сверки остатков споткнулся: %s", e)
        await asyncio.sleep(DEEPSEEK_BALANCE_SYNC_SEC)
