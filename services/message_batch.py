# ─────────────────────────────────────────────────────────────
#  services/message_batch.py — 🧩 склейка сообщений подряд (2026-10-09)
# ─────────────────────────────────────────────────────────────
# Зачем: обработчик текста работает ПАРАЛЛЕЛЬНО (block=False), и серия
# сообщений одного человека («Бот…», «Бооот…», «Ти знову завис?») давала
# столько же одновременных вопросов модели — каждый не видел остальных, и
# человек получал пачку ответов. 02.10.2026 после обрыва связи девять
# сообщений пришли разом — ушло восемь вопросов модели. По журналам 1–7.10
# сериями (пауза ≤ 3 с) пришло 22% сообщений боту.
#
# Как устроено (решение Максима 09.10.2026):
#  • Пачка — сообщения ОДНОГО человека в ОДНОМ чате. Бот ждёт wait_sec()
#    тишины (по умолчанию 3 с; каждое новое сообщение ждёт заново), но не
#    дольше MAX_WAIT_SEC от первого сообщения пачки, и отдаёт пачку модели
#    одним вопросом.
#  • Пока бот отвечает, новые сообщения этого человека копятся и после ответа
#    уходят ОДНИМ следующим вопросом — параллельного ответа нет.
#  • Регулятор «🧩 Склейка» (settings_spec, ключ batch_wait_sec): кнопки
#    ➖/➕ в «📡 Настройки API» и страница настроек сайта; 0 = выключено,
#    каждое сообщение отвечается сразу, как до 09.10.2026.
#
# Модуль не знает про Telegram: что делать в начале пачки (on_open) и как
# отвечать (on_flush), передаёт обработчик (handlers/messages.py). Так его
# можно проверить без поддельного Telegram (selftest).
#
# ⚠️ ТОЛЬКО ТЕКСТ. Фото, голосовые и видео идут как раньше — каждое своим
# ответом.
# ⚠️ Состояние живёт в памяти: перезапуск бота посреди ожидания теряет пачку
# так же, как и сейчас теряется ответ, который модель не успела дописать.
# ─────────────────────────────────────────────────────────────

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Ключ настройки в settings (подпись, пределы и начальное значение — в
# services/settings_spec.py, там же, где у остальных регуляторов).
SETTING_KEY = "batch_wait_sec"

# Общий потолок ожидания пачки от её ПЕРВОГО сообщения: человек, который
# пишет без остановки, не должен ждать ответа бесконечно.
MAX_WAIT_SEC = 10


def wait_sec() -> int:
    """Сколько секунд ждать тишины. 0 — склейка выключена. Сбой — выключена."""
    try:
        from services.settings_spec import read
        return max(0, int(read(SETTING_KEY)))
    except Exception:
        return 0


class _Slot:
    """Очередь одного человека в одном чате."""

    def __init__(self):
        self.pending: list = []              # сообщения, ещё не отданные модели
        self.first_ts = 0.0                  # когда пришло первое из pending
        self.last_ts = 0.0                   # когда пришло последнее из pending
        self.task: asyncio.Task | None = None


# (chat_id, user_id) → _Slot. Слот живёт, пока у человека есть что ответить.
_slots: dict[tuple[int, int], _Slot] = {}


def submit(key: tuple[int, int], item, on_open, on_flush) -> None:
    """
    Положить сообщение в пачку человека. Ответ будет позже — из задачи.

    on_open(first_item) — корутина, зовётся, когда пачка НАЧИНАЕТ копиться
        (в личке — черновик «💭 Думаю…»); что вернёт — получит on_flush.
    on_flush(items, opened) — корутина: отдать пачку модели и ответить.
    """
    now = time.monotonic()
    slot = _slots.get(key)
    if slot is None:
        slot = _slots[key] = _Slot()
    if not slot.pending:
        slot.first_ts = now
    slot.pending.append(item)
    slot.last_ts = now
    if slot.task is None or slot.task.done():
        slot.task = asyncio.create_task(_worker(key, slot, on_open, on_flush))


async def _worker(key, slot: _Slot, on_open, on_flush) -> None:
    """Пока у человека есть неотвеченное — ждём тишины и отвечаем пачкой."""
    try:
        while slot.pending:
            opened = None
            try:
                opened = await on_open(slot.pending[0])
            except Exception as e:
                logger.warning("🧩 Не удалось начать пачку %s: %s", key, e)
            # Ждём тишины: от последнего сообщения — wait_sec(), от первого —
            # не дольше MAX_WAIT_SEC. Пришло новое — пересчитываем.
            while True:
                now = time.monotonic()
                quiet_left = wait_sec() - (now - slot.last_ts)
                cap_left = MAX_WAIT_SEC - (now - slot.first_ts)
                left = min(quiet_left, cap_left)
                if left <= 0:
                    break
                await asyncio.sleep(left)
            items, slot.pending = slot.pending, []
            if len(items) > 1:
                logger.info("🧩 Склеено сообщений: %d (чат %s, человек %s)", len(items), key[0], key[1])
            try:
                await on_flush(items, opened)
            except Exception as e:
                logger.error("⚠️ Не удалось ответить на пачку сообщений %s: %s", key, e)
    finally:
        if _slots.get(key) is slot and not slot.pending:
            del _slots[key]
