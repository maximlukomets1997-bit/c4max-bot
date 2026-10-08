# ─────────────────────────────────────────────────────────────
#  services/live_answer.py — 💬 ответ «на глазах» в личке (2026-10-08)
# ─────────────────────────────────────────────────────────────
# Зачем: человек ждал ответа 10–40 секунд, видя только «печатает…». Потоковые
# модели (Qwen, DeepSeek, Xiaomi) присылают ответ кусочками и раньше — бот
# просто копил их до конца. Теперь кусочки показываются ЧЕРНОВИКОМ Telegram
# (sendMessageDraft): пока модель думает — «💭 Думаю… N с», потом текст
# дописывается на глазах. Готовый ответ уходит как и раньше — send_formatted,
# с разметкой, мыслями и ответом на сообщение человека.
#
# Как устроено:
#  • LiveDraft — состояние одного ответа. Поток модели (services/gemini.py,
#    через _live_call) зовёт feed(кусок) и attempt() — «началась новая
#    попытка цепочки»; насос run() в цикле событий раз в TICK_SEC шлёт
#    черновик, если показ изменился.
#  • Тумблер «Ответ на глазах» — страница настроек сайта (settings_spec,
#    ключ live_answer_enabled). Выключен — бот отвечает по-старому.
#
# ⚠️ ЧЕРНОВИК — ТОЛЬКО ПОКАЗ. В память, журнал и учёт денег идёт то, что
# вернула модель целиком, как и без него. Любой отказ Telegram выключает
# черновик ДО КОНЦА ЭТОГО ОТВЕТА, и ответ приходит по-старому.
# ⚠️ ТОЛЬКО ЛИЧКА: черновики Telegram существуют лишь в личных чатах.
# ⚠️ Модели Gemini у нас отвечают одним куском (без потока) — с ними черновик
# так и стоит на «Думаю…» до готового ответа.
# ─────────────────────────────────────────────────────────────

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Ключ тумблера в settings (и в services/settings_spec.py — там подпись и
# начальное значение для сайта).
SETTING_KEY = "live_answer_enabled"

# Как часто обновлять черновик. Telegram 08.10.2026 принял 16 черновиков
# подряд по два в секунду без единого отказа; раз в секунду — с запасом, а
# Telegram сам плавно анимирует добавку между обновлениями.
TICK_SEC = 1.0

# Сколько сырого текста ответа помещать в черновик. Предел Telegram — 4096
# знаков ПОСЛЕ разметки; длинный ответ в черновике обрывается многоточием,
# целиком (и нарезанным на части) он придёт готовым сообщением.
DRAFT_MAX_CHARS = 3500

THINK_TEXT = "💭 Думаю… {sec} с"
RETRY_TEXT = "⏳ Модель сбилась, спрашиваю другую…"


def live_answer_enabled() -> bool:
    """
    Тумблер «Ответ на глазах» (по умолчанию ВКЛЮЧЁН). Сбой чтения настройки —
    считаем выключенным: тогда бот просто отвечает по-старому.
    """
    try:
        from database.history import get_setting
        return get_setting(SETTING_KEY, "1") == "1"
    except Exception:
        return False


class LiveDraft:
    """
    Черновик одного ответа в личке.

    feed() и attempt() зовёт поток исполнителя (запрос к модели), run() —
    цикл событий. Общего у них — список кусков и два флага; присваивания и
    добавление в список в Python атомарны, замок не нужен.
    """

    def __init__(self, chat_id: int, draft_id: int):
        self.chat_id = chat_id
        self.draft_id = draft_id or 1        # Telegram требует ненулевой номер
        self._parts: list[str] = []          # куски ответа ТЕКУЩЕЙ попытки
        self._restarted = False              # прошлая попытка успела показать текст
        self._started = time.monotonic()
        self._stopped = False
        self._dead = False                   # Telegram отказал — больше не шлём
        self._last_sent = None
        self.sent = 0                        # сколько черновиков ушло (для лога и проверки)

    # ── зовёт поток модели ───────────────────────────────────
    def attempt(self) -> None:
        """Началась новая попытка цепочки: прежний текст недействителен."""
        if self._parts:
            self._restarted = True
        self._parts = []

    def feed(self, piece: str) -> None:
        """Пришёл очередной кусок ответа (без мыслей)."""
        self._parts.append(piece)

    # ── показ ────────────────────────────────────────────────
    def view(self) -> str:
        """Что сейчас должно стоять в черновике (сырой Markdown)."""
        text = "".join(self._parts).strip()
        if text:
            if len(text) > DRAFT_MAX_CHARS:
                text = text[:DRAFT_MAX_CHARS].rstrip() + " …"
            return text
        if self._restarted:
            return RETRY_TEXT
        return THINK_TEXT.format(sec=int(time.monotonic() - self._started))

    async def send_once(self, bot) -> None:
        """Отправить черновик, если показ изменился. Отказ — черновик умолкает."""
        if self._dead:
            return
        raw = self.view()
        if raw == self._last_sent:
            return
        try:
            from utils_format import convert_md
            text, entities = convert_md(raw)
            if not text.strip():
                return
            await bot.send_message_draft(chat_id=self.chat_id, draft_id=self.draft_id,
                                         text=text, entities=entities)
            self._last_sent = raw
            self.sent += 1
        except Exception as e:
            self._dead = True
            logger.warning("💬 Черновик ответа не принят (чат %s): %s — дальше отвечаю по-старому",
                           self.chat_id, e)

    async def run(self, bot) -> None:
        """Насос: раз в TICK_SEC обновляет черновик, пока не позовут stop()."""
        try:
            while not self._stopped:
                await self.send_once(bot)
                await asyncio.sleep(TICK_SEC)
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        self._stopped = True
