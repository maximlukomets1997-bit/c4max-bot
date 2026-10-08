# ─────────────────────────────────────────────────────────────
#  services/live_answer.py — 💬 ответ «на глазах» (2026-10-08)
# ─────────────────────────────────────────────────────────────
# Зачем: человек ждал ответа 10–40 секунд, видя только «печатает…». Потоковые
# модели (Qwen, DeepSeek, Xiaomi) присылают ответ кусочками и раньше — бот
# просто копил их до конца. Теперь кусочки видны по ходу:
#  • ЛИЧКА — ЧЕРНОВИК Telegram (sendMessageDraft, LiveDraft): пока модель
#    думает — «💭 Думаю… N с», потом текст дописывается раз в секунду.
#    Готовый ответ уходит как и раньше — send_formatted.
#  • ГРУППА — черновиков у Telegram там нет, поэтому бот сразу шлёт
#    сообщение «💭 Думаю…» ответом на вопрос и ПРАВИТ его по ходу раз в
#    GROUP_TICK_SEC (LiveGroupMessage); последней правкой оно становится
#    готовым ответом (finish) — с разметкой и мыслями.
#
# Как устроено:
#  • Поток модели (services/gemini.py, через _live_call) зовёт feed(кусок) и
#    attempt() — «началась новая попытка цепочки»; насос run() в цикле
#    событий шлёт обновление, если показ изменился.
#  • Выключатель ОДИН на личку и группы (решение Максима): кнопка
#    «✍️ ОТВЕТ НА ГЛАЗАХ» в «📡 Настройки API» и тумблер на странице настроек
#    сайта, ключ live_answer_enabled. Выключен — везде по-старому.
#
# ⚠️ ПОКАЗ ПО ХОДУ — ТОЛЬКО ПОКАЗ. В память, архив группы, журнал и учёт денег
# идёт то, что вернула модель целиком, как и без него. Любой отказ Telegram
# прекращает обновления ДО КОНЦА ЭТОГО ОТВЕТА; готовый ответ придёт всё равно
# (в группе — правкой, а не вышло — новым сообщением).
# ⚠️ В ГРУППЕ ПРАВОК МЕНЬШЕ И «ДУМАЮ» БЕЗ СЧЁТЧИКА: у Telegram предел около
# 20 сообщений в минуту на группу, и правки тратят тот же запас. Попросил
# подождать (RetryAfter) — правки по ходу прекращаются.
# ⚠️ «Сам в разговор» сюда не ходит: там бот может решить промолчать, и
# «Думаю…» выдало бы его заранее. Только прямые вопросы (handle_message).
# ⚠️ Модели Gemini у нас отвечают одним куском (без потока) — с ними показ
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

# Группа: правка сообщения раз в 3 секунды. За минуту это не больше 20 правок
# на один ответ — ровно предел Telegram на группу, а обычный ответ пишется
# за 5–15 секунд, то есть 2–5 правок. «Думаю» без счётчика: тикающие секунды
# тратили бы запас правок впустую.
GROUP_TICK_SEC = 3.0
GROUP_THINK_TEXT = "💭 Думаю…"

# Сколько готов ждать последнюю правку, если Telegram попросил подождать
# (RetryAfter). Дольше — готовый ответ уходит новым сообщением, а не висит.
FINISH_WAIT_MAX_SEC = 30


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

    def _tick(self) -> float:
        return TICK_SEC

    async def run(self, bot) -> None:
        """
        Насос: раз в _tick() секунд обновляет показ, пока не позовут stop().

        ⚠️ Гасится МЯГКО (stop будит паузу, начатая отправка доходит до конца),
        а не отменой задачи: в группе отмена посреди отправки первого сообщения
        оставила бы «💭 Думаю…» без номера — бот не смог бы его править, и в
        чате остались бы два сообщения вместо одного.
        """
        self._wake = asyncio.Event()
        try:
            while not self._stopped:
                await self.send_once(bot)
                try:
                    await asyncio.wait_for(self._wake.wait(), self._tick())
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        """Остановить насос: зовётся из цикла событий, будит его паузу."""
        self._stopped = True
        wake = getattr(self, "_wake", None)
        if wake is not None:
            wake.set()


def _retry_after_sec(e) -> float | None:
    """Сколько Telegram просит подождать (RetryAfter) или None, если ошибка другая.
    retry_after в разных версиях библиотеки — число секунд или timedelta."""
    from telegram.error import RetryAfter
    if not isinstance(e, RetryAfter):
        return None
    wait = e.retry_after
    return wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)


def _not_modified(e) -> bool:
    """Telegram отказал править, потому что текст тот же самый — это не сбой."""
    return "not modified" in str(e).lower()


class LiveGroupMessage(LiveDraft):
    """
    Ответ «на глазах» в группе: настоящее сообщение ответом на вопрос, которое
    бот правит по ходу и последней правкой (finish) превращает в готовый ответ.

    ⚠️ Первое сообщение («💭 Думаю…») — единственное, что присылает человеку
    уведомление: правки уведомлений не дают. Так задумано и обсуждено с
    Максимом 08.10.2026.
    """

    def __init__(self, chat_id: int, reply_to: int):
        super().__init__(chat_id, 1)
        self.reply_to = reply_to
        self.message_id = None               # сообщение, которое правим

    def _tick(self) -> float:
        return GROUP_TICK_SEC

    def view(self) -> str:
        shown = super().view()
        # Без счётчика секунд — см. GROUP_TICK_SEC.
        return GROUP_THINK_TEXT if shown.startswith("💭") else shown

    async def send_once(self, bot) -> None:
        """Первый раз — отправить, дальше — править, если показ изменился."""
        if self._dead:
            return
        raw = self.view()
        if raw == self._last_sent:
            return
        try:
            from telegram import LinkPreviewOptions
            from utils_format import convert_md
            text, entities = convert_md(raw)
            if not text.strip():
                return
            no_preview = LinkPreviewOptions(is_disabled=True)
            if self.message_id is None:
                msg = await bot.send_message(chat_id=self.chat_id, text=text, entities=entities,
                                             reply_to_message_id=self.reply_to,
                                             link_preview_options=no_preview)
                self.message_id = msg.message_id
            else:
                await bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id,
                                            text=text, entities=entities,
                                            link_preview_options=no_preview)
            self._last_sent = raw
            self.sent += 1
        except Exception as e:
            if _not_modified(e):
                self._last_sent = raw
                return
            self._dead = True
            logger.warning("💬 Ответ на глазах в группе %s: Telegram отказал (%s) — "
                           "дальше без правок по ходу, готовый ответ придёт в конце",
                           self.chat_id, e)

    async def finish(self, bot, raw_answer: str) -> None:
        """
        Последняя правка: сообщение становится готовым ответом — с разметкой и
        мыслями, как у send_formatted. Длинный ответ: первая часть — правкой,
        остальные — новыми сообщениями следом. Сообщения по ходу не было —
        обычный send_formatted. Править не вышло — недописанное сообщение
        убираем и шлём готовый ответ новым (send_formatted): ответ человек
        получит в любом случае.
        """
        from telegram import LinkPreviewOptions
        from utils_format import (MAX_UTF16, _to_ptb, build_text_and_entities,
                                  send_formatted, split_entities)
        if self.message_id is None:
            await send_formatted(bot, self.chat_id, raw_answer, reply_to=self.reply_to)
            return
        no_preview = LinkPreviewOptions(is_disabled=True)
        try:
            text, entities = build_text_and_entities(raw_answer)
            chunks = list(split_entities(text, entities, MAX_UTF16))
            first_text, first_entities = chunks[0]
            for attempt in range(2):
                try:
                    await bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id,
                                                text=first_text, entities=_to_ptb(first_entities),
                                                link_preview_options=no_preview)
                    break
                except Exception as e:
                    if _not_modified(e):
                        break
                    wait = _retry_after_sec(e)
                    if attempt == 0 and wait is not None and wait <= FINISH_WAIT_MAX_SEC:
                        await asyncio.sleep(wait)
                        continue
                    raise
            for chunk_text, chunk_entities in chunks[1:]:
                try:
                    await bot.send_message(chat_id=self.chat_id, text=chunk_text,
                                           entities=_to_ptb(chunk_entities),
                                           link_preview_options=no_preview)
                except Exception as e:
                    logger.warning("⚠️ Не удалось отправить продолжение ответа в группу %s: %s",
                                   self.chat_id, e)
        except Exception as e:
            logger.warning("💬 Ответ на глазах в группе %s: последняя правка не вышла (%s) — "
                           "шлю готовый ответ новым сообщением", self.chat_id, e)
            try:
                await bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)
            except Exception:
                pass
            await send_formatted(bot, self.chat_id, raw_answer, reply_to=self.reply_to)
