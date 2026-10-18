# ─────────────────────────────────────────────────────────────
#  services/live_answer.py — 💬 ответ «на глазах» (2026-10-08)
# ─────────────────────────────────────────────────────────────
# Зачем: человек ждал ответа 10–40 секунд, видя только «печатает…». Потоковые
# модели (DeepSeek, Xiaomi) присылают ответ кусочками и раньше — бот
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
#  • Поток модели (services/gemini.py, через _live_call) зовёт feed(кусок),
#    think(кусок мыслей) — с 14.10.2026, мысли в черновике лички, см. раздел
#    ниже — и attempt() — «началась новая попытка цепочки»; насос run() в
#    цикле событий шлёт обновление, если показ изменился.
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
# «Думаю…» выдало бы его заранее (решение Максима 08.10.2026 — без него).
# Только прямые вопросы: текст, фото, голосовое и видео (handlers/messages.py
# через _start_live/_stop_live/_deliver/_reply_error). У голосового и видео
# (09.10.2026) первая стадия — label («🎧 Слушаю голосовое…» / «🎬 Смотрю
# видео…»), пока вспомогательная модель расшифровывает файл; потом «Думаю»
# и текст ответа активной модели. Запасной путь «файл слышащей/зрячей
# цепочке» отвечает одним куском — показ до конца стоит на первой стадии.
# ⚠️ Модели Gemini у нас отвечают одним куском (без потока) — с ними показ
# так и стоит на «Думаю…» до готового ответа.
# ─────────────────────────────────────────────────────────────

import asyncio
import logging
import re
import time

from config import PROACTIVE_MUTE_CLEANUP_PATTERN

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


# ── Пометка мута в показе по ходу (12.10.2026) ─────────────────────────
# С этого дня модель может закончить ответ на обращение в группе пометкой
# [МУТ:секунды] (services/proactive.py, «руки» при прямом обращении). Из
# готового ответа её вырезает обработчик, но показ по ходу идёт РАНЬШЕ — и
# без этого среза люди несколько секунд видели бы служебный маркер, а с ним и
# то, что человека сейчас накажут. Шаблон целой пометки — общий, из config;
# второй ловит НЕДОПИСАННУЮ в самом хвосте («[», «[МУ», «[МУТ:30»): поток
# приносит её по кусочкам. Чужая незакрытая скобка в хвосте тоже пропадёт из
# показа на один тик — и вернётся, как только за ней придёт что-то ещё.
_MUTE_TAG_RE = re.compile(PROACTIVE_MUTE_CLEANUP_PATTERN, re.IGNORECASE)
_MUTE_TAG_TAIL_RE = re.compile(
    r"\[\s*(?:(?:М|МУ|МУТ|M|MU|MUT|MUTE)|(?:МУТ|MUTE)\s*[:=][^\]\n]{0,24})?\s*$",
    re.IGNORECASE)


def _hide_mute_tag(text: str) -> str:
    """Текст показа без пометки мута — целой и недописанной в хвосте."""
    return _MUTE_TAG_TAIL_RE.sub("", _MUTE_TAG_RE.sub("", text))


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


# ── Мысли в черновике лички (14.10.2026, решение Максима) ────────────────
# До этого дня в черновик шёл только текст ответа, а мысли появлялись лишь в
# готовом сообщении — сверху. Выглядело так, будто сообщение исчезло и пришло
# заново: черновик убирался, и над тем же текстом вырастал блок «🧠 Мысли».
# Теперь мысли видны по ходу — тем же свёрнутым блоком и на том же месте, что
# в готовом ответе (сборка общая, utils_format.build_text_and_entities), а
# ответ пишется под ними. Живая проба 14.10.2026: Telegram показывает
# свёрнутую цитату и в черновике, подмена на сообщение незаметна.
#
# ⚠️ МЫСЛИ НЕ РЕЖУТСЯ (решение Максима). Черновик — один пузырь до 4096 знаков,
# а мысли бывают по 10 000+. Перестали влезать — заполнившаяся часть уходит
# ОБЫЧНЫМ сообщением (как раньше в конце, только по ходу), а черновик
# продолжается ниже. Готовый ответ досылает остаток мыслей и сам ответ
# (finish_text) — в итоге всё выглядит как раньше: несколько сообщений с
# мыслями и последнее с ответом.
#
# Работает, только когда включены ВСЕ ТРИ: этот тумблер, «🧠 МЫСЛИ ПОД
# КАПОТОМ» и «✍️ ОТВЕТ НА ГЛАЗАХ». Выключен любой — как до 14.10.2026.
THOUGHTS_SETTING_KEY = "live_thoughts_enabled"

# Предел текста черновика с запасом на служебные знаки (у Telegram 4096
# после разметки).
DRAFT_LIMIT_UTF16 = 4000
# Сколько сырых знаков мыслей уходит одним сообщением, когда они перерастают
# черновик; граница — перевод строки или пробел не дальше этого.
THOUGHTS_PART_CHARS = 3400


def live_thoughts_enabled() -> bool:
    """
    Тумблер «🧠 Мысли в черновике» (по умолчанию ВКЛЮЧЁН) — только своя
    настройка: её же показывает кнопка и сайт. Будут ли мысли в черновике на
    самом деле, решает _thoughts_wanted. Сбой чтения — выключен: тогда
    черновик как раньше.
    """
    try:
        from database.history import get_setting
        return get_setting(THOUGHTS_SETTING_KEY, "1") == "1"
    except Exception:
        return False


def _thoughts_wanted() -> bool:
    """Показывать ли мысли по ходу: свой тумблер И общий «мысли под капотом»."""
    try:
        from utils_format import thoughts_enabled
        return live_thoughts_enabled() and thoughts_enabled()
    except Exception:
        return False


class LiveDraft:
    """
    Черновик одного ответа в личке.

    feed(), attempt() и thinking() зовёт поток исполнителя (запрос к модели),
    run() — цикл событий. Общего у них — список кусков и флаги; присваивания
    и добавление в список в Python атомарны, замок не нужен.

    label (09.10.2026) — первая стадия для голосового и видео: «🎧 Слушаю
    голосовое…» / «🎬 Смотрю видео…», пока вспомогательная модель
    расшифровывает файл. Как только основной запрос начался (thinking() из
    gemini.ask_gemini), стадия сменяется на «💭 Думаю…». Без label — сразу
    «Думаю».
    """

    def __init__(self, chat_id: int, draft_id: int, label: str | None = None):
        self.chat_id = chat_id
        self.draft_id = draft_id or 1        # Telegram требует ненулевой номер
        self._parts: list[str] = []          # куски ответа ТЕКУЩЕЙ попытки
        self._restarted = False              # прошлая попытка успела показать текст
        self._label = label                  # стадия до «Думаю» (голосовое, видео)
        self._thinking = label is None       # основной запрос к модели уже идёт
        self._started = time.monotonic()
        self._stopped = False
        self._dead = False                   # Telegram отказал — больше не шлём
        self._last_sent = None
        self.sent = 0                        # сколько черновиков ушло (для лога и проверки)
        # Мысли по ходу (14.10.2026, см. _thoughts_wanted). Решается ОДИН раз на
        # ответ: щёлкнул тумблером посреди ответа — этот ответ дойдёт как начался.
        self._show_thoughts = self._THOUGHTS_ALLOWED and _thoughts_wanted()
        self._thoughts: list[str] = []       # куски мыслей ТЕКУЩЕЙ попытки (сырые)
        self._committed = 0                  # сколько сырых знаков мыслей ушло сообщениями
        self._committed_ids: list[int] = []  # эти сообщения — текущей попытки
        self._stale_ids: list[int] = []      # сообщения сбившейся попытки — убрать
        self._epoch = 0                      # номер попытки: сменился посреди отправки — сообщение устарело
        self._draft_seq = 0                  # сколько раз черновик начинался заново под сообщением

    # ── зовёт поток модели ───────────────────────────────────
    def thinking(self) -> None:
        """Начался основной запрос к модели: стадия «Слушаю/Смотрю» позади."""
        self._thinking = True

    def attempt(self) -> None:
        """
        Началась новая попытка цепочки: прежний текст недействителен. Мысли —
        тоже, а их части, уже ушедшие сообщениями, насос уберёт из чата.
        """
        if self._parts or (self._show_thoughts and (self._thoughts or self._committed_ids)):
            self._restarted = True
        self._parts = []
        self._thoughts = []
        if self._committed_ids:
            self._stale_ids.extend(self._committed_ids)
        self._committed_ids = []
        self._committed = 0
        self._epoch += 1

    def feed(self, piece: str) -> None:
        """Пришёл очередной кусок ответа (без мыслей)."""
        self._parts.append(piece)

    def think(self, piece: str) -> None:
        """Пришёл очередной кусок мыслей (14.10.2026). Мысли не показываем — отбрасываем."""
        if self._show_thoughts:
            self._thoughts.append(piece)

    # ── показ ────────────────────────────────────────────────
    # Прятать ли из показа пометку мута. В личке прав на мут у модели нет, и
    # резать там нечего; в группе — см. LiveGroupMessage и _hide_mute_tag.
    _HIDE_MUTE_TAG = False
    # Можно ли этому виду показа показывать мысли по ходу (14.10.2026): да —
    # только черновику лички, см. LiveGroupMessage.
    _THOUGHTS_ALLOWED = True

    def _answer_text(self) -> str:
        """Текст ответа для показа: без пометки мута (группа), длинный — с многоточием."""
        text = "".join(self._parts)
        if self._HIDE_MUTE_TAG:
            text = _hide_mute_tag(text)
        text = text.strip()
        if len(text) > DRAFT_MAX_CHARS:
            text = text[:DRAFT_MAX_CHARS].rstrip() + " …"
        return text

    def _thoughts_rest(self) -> str:
        """
        Мысли, ещё НЕ ушедшие сообщениями, — в том виде, в каком их покажет
        готовый ответ: лишние пустые строки сжаты тем же правилом, что у всего
        ответа (gemini.compress_newlines), иначе черновик и сообщение
        различались бы, и подмена была бы видна.
        """
        if not self._show_thoughts:
            return ""
        # Локальный импорт: gemini грузится раньше и к этому модулю не тянется.
        from services.gemini import compress_newlines
        return compress_newlines("".join(self._thoughts)[self._committed:]).strip()

    def _view(self, seconds: bool) -> str:
        """Показ по ходу (сырой Markdown); seconds — тикающий счётчик ожидания.
        Мысли (если показываются) — блоком <thought> перед ответом, ровно так,
        как их получит готовое сообщение."""
        text = self._answer_text()
        rest = self._thoughts_rest()
        if rest:
            return f"<thought>{rest}</thought>{text}"
        if text:
            return text
        if self._restarted:
            return RETRY_TEXT
        sec = int(time.monotonic() - self._started)
        if not self._thinking and self._label:
            return f"{self._label} {sec} с" if seconds else self._label
        return THINK_TEXT.format(sec=sec) if seconds else GROUP_THINK_TEXT

    def view(self) -> str:
        """Что сейчас должно стоять в черновике лички (со счётчиком секунд)."""
        return self._view(seconds=True)

    def _render(self, raw: str, header: bool | None = None):
        """
        Показ → (текст, выделения Telegram). С мыслями — той же сборкой, что
        готовое сообщение (свёрнутая цитата сверху); шапка «🧠 Мысли» — только
        у первой части: продолжение мыслей ниже идёт без неё, как раньше при
        нарезке длинного ответа.
        """
        from utils_format import _to_ptb, build_text_and_entities, convert_md
        if raw.startswith("<thought>"):
            text, entities = build_text_and_entities(
                raw, header=(self._committed == 0) if header is None else header)
            return text, _to_ptb(entities)
        return convert_md(raw)

    def _current_draft_id(self) -> int:
        """Номер черновика: после каждого ушедшего сообщения — новый, ниже него."""
        return self.draft_id if not self._draft_seq else self.draft_id * 100 + self._draft_seq

    async def _settle_thoughts(self, bot) -> None:
        """
        Перед обновлением черновика: убрать из чата мысли сбившейся попытки и
        вынести ОБЫЧНЫМ сообщением часть мыслей, которая перестала влезать в
        черновик (см. шапку раздела). Начался ответ — мысли кончились, и если
        вместе с ним они не влезают, уходят все сразу; иначе — по границе
        строки или слова. Отказ Telegram — черновик умолкает до конца ответа,
        а невынесенные мысли дошлёт готовый ответ.
        """
        from utils_format import utf16_len
        from services.gemini import compress_newlines
        for mid in list(self._stale_ids):
            self._stale_ids.remove(mid)
            try:
                await bot.delete_message(chat_id=self.chat_id, message_id=mid)
            except Exception as e:
                logger.debug("💬 Не удалось убрать мысли сбившейся попытки (чат %s): %s",
                             self.chat_id, e)
        for _ in range(20):                  # потолок проходов — страховка от вечного цикла
            rest_raw = "".join(self._thoughts)[self._committed:]
            if not rest_raw.strip():
                return
            shown, _ = self._render(self.view())
            if utf16_len(shown) <= DRAFT_LIMIT_UTF16:
                return
            cut = 0
            if self._answer_text():
                whole, _ = self._render(f"<thought>{compress_newlines(rest_raw).strip()}</thought>")
                if utf16_len(whole) <= DRAFT_LIMIT_UTF16:
                    cut = len(rest_raw)
            if not cut:
                limit = min(len(rest_raw), THOUGHTS_PART_CHARS)
                cut = max(rest_raw.rfind("\n", 0, limit), rest_raw.rfind(" ", 0, limit)) + 1
                if cut < limit // 2:
                    cut = limit
            part_raw = compress_newlines(rest_raw[:cut]).strip()
            if not part_raw:
                self._committed += cut
                continue
            text, entities = self._render(f"<thought>{part_raw}</thought>")
            epoch, first = self._epoch, not self._committed_ids and self._committed == 0
            try:
                msg = await bot.send_message(
                    chat_id=self.chat_id, text=text, entities=entities,
                    reply_to_message_id=self.draft_id if first else None)
            except Exception as e:
                self._dead = True
                logger.warning("💬 Мысли по ходу не ушли сообщением (чат %s): %s — "
                               "остаток дошлёт готовый ответ", self.chat_id, e)
                return
            if epoch != self._epoch:
                # Попытка сменилась, пока слали: это мысли сбившейся модели.
                self._stale_ids.append(msg.message_id)
                return
            self._committed += cut
            self._committed_ids.append(msg.message_id)
            self._draft_seq += 1
            self._last_sent = None

    async def finish_text(self, bot, answer: str) -> tuple[str, bool]:
        """
        Готовый ответ с учётом мыслей, уже ушедших сообщениями по ходу.
        Возвращает (что отправить, «это продолжение»): продолжение уходит без
        шапки мыслей и без Reply — шапка и Reply были у первой части.

        Ушедших частей нет — ответ как есть. Ответ пришёл без мыслей (вся
        цепочка легла и пришла заглушка, или ответила модель без потока) —
        ушедшие части принадлежат сбившейся попытке и убираются из чата.
        """
        for mid in list(self._stale_ids):
            self._stale_ids.remove(mid)
            try:
                await bot.delete_message(chat_id=self.chat_id, message_id=mid)
            except Exception:
                pass
        if not self._committed_ids:
            return answer, False
        from utils_format import THOUGHT_RE, strip_thoughts
        if not THOUGHT_RE.search(answer or ""):
            for mid in self._committed_ids:
                try:
                    await bot.delete_message(chat_id=self.chat_id, message_id=mid)
                except Exception:
                    pass
            self._committed_ids = []
            return answer, False
        rest, body = self._thoughts_rest(), strip_thoughts(answer)
        return (f"<thought>{rest}</thought>{body}" if rest else body), True

    async def send_once(self, bot) -> None:
        """Отправить черновик, если показ изменился. Отказ — черновик умолкает."""
        if self._dead:
            return
        if self._show_thoughts:
            await self._settle_thoughts(bot)
            if self._dead:
                return
        raw = self.view()
        if raw == self._last_sent:
            return
        try:
            text, entities = self._render(raw)
            if not text.strip():
                return
            await bot.send_message_draft(chat_id=self.chat_id, draft_id=self._current_draft_id(),
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

    # Ответ на обращение в группе может кончаться пометкой мута — участники
    # её видеть не должны и по ходу тоже (см. _hide_mute_tag).
    _HIDE_MUTE_TAG = True
    # Мыслей по ходу в группе нет (14.10.2026): черновиков там не бывает, а
    # правки идут скачками раз в GROUP_TICK_SEC и тратят запас Telegram.
    _THOUGHTS_ALLOWED = False

    def __init__(self, chat_id: int, reply_to: int, label: str | None = None):
        super().__init__(chat_id, 1, label)
        self.reply_to = reply_to
        self.message_id = None               # сообщение, которое правим

    def _tick(self) -> float:
        return GROUP_TICK_SEC

    def view(self) -> str:
        # Без счётчика секунд — см. GROUP_TICK_SEC.
        return self._view(seconds=False)

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
