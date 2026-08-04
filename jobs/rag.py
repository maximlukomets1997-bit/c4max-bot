# ───────────────────────────────────────────────
#  jobs/rag.py — Ежечасный добор базы знаний (RAG)
#
#  Выделен из jobs.py 2026-08-04 разрезом БЕЗ изменения логики
#  (файл был на 837 строк и держал шесть независимых циклов).
#  Дотягивает статьи, не проиндексированные из-за лимита Google.
# ───────────────────────────────────────────────

import logging
import asyncio

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────
#  Ежечасный добор базы знаний (RAG)
# ───────────────────────────────────────────────

async def rag_catchup_loop(application):
    """
    Раз в час проверяет, полон ли векторный индекс базы знаний, и дотягивает
    недостающие статьи обычной синхронизацией (только новые/изменённые файлы,
    с паузами при лимите Google — см. services/rag.py). Нужен после сбоев
    квоты: раньше неполная база ждала ручного перезапуска или нажатия кнопки.

    Проверка при полной базе бесплатна: ни запросов к Google, ни строк в логе.
    Уведомления админам в личку — по границам инцидента (решение Максима
    2026-07-19): одно «⚠️ база неполная» при первой неудаче добора и одно
    «✅ снова полная» при выздоровлении; между ними — только строки в логе.
    Пока идёт ручная пересборка (защёлка kb_rebuild_running в bot_data, её же
    ставит кнопка панели /rag) — пропускает свой час; на время собственной
    работы ставит ту же защёлку, чтобы панель вежливо просила подождать.
    """
    from config import RAG_ENABLED, ADMIN_IDS
    from services import rag

    if not RAG_ENABLED:
        return  # RAG выключен глобально — цикл не нужен (включение = рестарт бота)

    async def _notify_admins(text: str) -> None:
        # Ошибка отправки одному админу не мешает остальным (как в итогах месяца)
        for admin_id in ADMIN_IDS:
            try:
                await application.bot.send_message(chat_id=admin_id, text=text)
            except Exception as e:
                logger.warning("⚠️ Не удалось отправить уведомление о доборе базы админу %s: %s", admin_id, e)

    notified_broken = False  # об ЭТОМ инциденте в личку уже жаловались?
    while True:
        # Стартовую синхронизацию делает main.py при запуске — первый час пропускаем
        await asyncio.sleep(3600)
        try:
            if application.bot_data.get("kb_rebuild_running"):
                continue  # идёт ручная пересборка — не мешаем, проверим через час
            loop = asyncio.get_running_loop()
            lag = await loop.run_in_executor(None, rag.index_lag)
            if not lag:
                notified_broken = False  # база полная; следующий сбой — новый инцидент
                continue

            logger.info("🚀 Добор базы знаний: индекс неполный (статей без векторов: %d) — дотягиваю", lag)
            application.bot_data["kb_rebuild_running"] = True
            try:
                result = await loop.run_in_executor(None, rag.sync_knowledge_base)
            finally:
                application.bot_data["kb_rebuild_running"] = False
            if result is None:
                continue
            indexed, total = result
            if indexed >= total:
                logger.info("🚀 Добор базы знаний: база снова полная (%d из %d)", indexed, total)
                await _notify_admins(f"✅ База знаний снова полная: {indexed} из {total} статей (автоматический добор).")
                notified_broken = False
            else:
                logger.warning("⚠️ Добор базы знаний: удалось не всё (%d из %d) — следующая попытка через час", indexed, total)
                if not notified_broken:
                    notified_broken = True
                    await _notify_admins(
                        f"⚠️ База знаний неполная: проиндексировано {indexed} из {total} статей — "
                        f"лимит Google пока не пускает.\n"
                        f"Буду пытаться добрать каждый час и напишу, когда база снова станет полной."
                    )
        except Exception as e:
            logger.error("⚠️ Не удалось выполнить цикл добора базы знаний: %s", e)
