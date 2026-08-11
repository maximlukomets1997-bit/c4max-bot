# ───────────────────────────────────────────────
#  proactive.py — проактивное участие бота в разговоре групп («Сам в разговор»)
#
#  Бот слушает беседу (архив groups_messages пишет collect_group_message) и
#  сам решает, когда вставить реплику — как живой участник. Схема:
#
#    collect_group_message → consider_message (дешёвые фильтры, синхронно)
#      → отдельная задача _run_proactive (очередь апдейтов модель НЕ ждёт)
#        → gemini.ask_group_proactive (активная думающая модель решает и пишет)
#          → реплика в чат или тишина.
#
#  Судья удалён 2026-07-20 (решение Максима): быстрые модели без мышления
#  плохо следуют инструкциям — решение «вступать/молчать» и написание реплики
#  снова делает одна думающая модель (одношаговая схема).
#
#  Все счётчики и паузы живут в памяти процесса и обнуляются при рестарте —
#  та же политика, что у счётчиков антиспама. Живые настройки — в settings
#  (панель промптов /adm → «⚙️ Управление PROMPTами»):
#    proactive_enabled       — общий тумблер (по умолчанию ВЫКЛ)
#    proactive_min_msgs      — минимум новых сообщений между проверками
#    proactive_hands         — «руки»: можно ли самому выдавать мут
#
#  ⚠️ ВРЕМЕННЫХ ПАУЗ БОЛЬШЕ НЕТ (обе убраны 2026-07-20 по решению Максима):
#  пауза между проверками (PROACTIVE_JUDGE_MIN_SEC) и пауза после своей реплики
#  (proactive_cooldown_sec) делали одно и то же — тормозили бота по часам.
#  Что сдерживает бота теперь:
#    • порог новых сообщений proactive_min_msgs (у Максима 1 — то есть почти не
#      сдерживает);
#    • защёлка _in_flight — в одном чате не бывает двух проверок сразу;
#    • САМА МОДЕЛЬ: правило «ты недавно уже писал и добавить нечего» в промпте
#      участия. Это теперь ЕДИНСТВЕННЫЙ реальный тормоз, и он мягкий.
#  Если бот начнёт тараторить — чинить промптом (или вернуть порог), а не
#  заводить таймеры заново: их убрали намеренно, дважды.
#  Метки _last_judge_ts / _last_reply_ts пишутся, но ничего больше не тормозят.
#
#  СЧЁТ УЧАСТИЯ (2026-07-31), чтобы порог и промпт правились по цифрам,
#  а не по ощущению «тараторит / молчит». Считается в ДВУХ местах, и это
#  намеренно:
#    • дошедшие до модели проверки и их исход → таблица proactive_log в базе
#      (их единицы в минуту, история переживает перезапуск);
#    • отсев дешёвыми фильтрами → счётчик _skipped В ПАМЯТИ. Этот путь
#      работает на КАЖДОЕ сообщение группы, и походу в базу тут взяться
#      неоткуда. Не «привести к общему виду».
#  Показывают цифры панель промптов и её экран «📊 Подробнее об участии»,
#  плюс блок в суточном отчёте о расходах.
#
#  Значок логов: 🤖. Модуль «тихий»: любое исключение глушится, наружу
#  (в архиватор групп) не бросает НИКОГДА.
# ───────────────────────────────────────────────

import asyncio
import logging
import re
import time

from config import (
    PROACTIVE_ENABLED_DEFAULT,
    PROACTIVE_MIN_MSGS,
    PROACTIVE_HANDS_DEFAULT,
    PROACTIVE_MUTE_MAX_SEC,
    PROACTIVE_MUTE_PATTERN,
    PROACTIVE_VIDEO_MAX_BYTES,
)
from database.history import get_setting, log_proactive_check, save_group_message
from services.gemini import (ask_group_proactive, ask_group_proactive_media,
                             _proactive_describe_image,
                             _proactive_transcribe_audio, _proactive_describe_video)
from utils import should_respond_in_group, keep_chat_action
from utils_format import send_formatted, strip_thoughts

logger = logging.getLogger(__name__)

# ─── состояние в памяти (обнуляется при рестарте — как у антиспама) ──
_msgs_since_check: dict[int, int] = {}    # chat_id → новых сообщений с последней проверки
_last_judge_ts: dict[int, float] = {}     # chat_id → monotonic последней проверки моделью
_last_reply_ts: dict[int, float] = {}     # chat_id → monotonic последней реплики бота
_in_flight: set[int] = set()              # чаты с проверкой «в полёте» (защёлка от гонки)

# ─── счётчик отсева дешёвыми фильтрами (2026-07-31) ──────────────────
#
#  Сколько сообщений групп до модели НЕ дошло и почему. Отвечает на вопрос
#  «почему бот весь вечер молчал»: чаще всего окажется, что он не молчал,
#  а просто не добирал порог или всё время был «в полёте».
#
#  ⚠️ ЖИВЁТ В ПАМЯТИ И ОБНУЛЯЕТСЯ ПЕРЕЗАПУСКОМ — намеренно, как счётчики
#  антиспама. Этот путь работает на КАЖДОЕ сообщение группы, и походу в базу
#  тут взяться неоткуда: ради этого же в своё время завели кэш user_settings.
#  В базу (proactive_log) пишутся только проверки, ДОШЕДШИЕ до модели.
_skipped: dict[str, int] = {}
_stats_since: float = time.time()         # с какого момента считаем (для подписи)


def _skip(reason: str) -> None:
    """Отметить, что сообщение отсеяно фильтром. Дешевле некуда — счёт в памяти."""
    _skipped[reason] = _skipped.get(reason, 0) + 1


def skip_counts() -> tuple[dict, float]:
    """
    Отсев по причинам и момент начала счёта (unix-время последнего запуска).
    Читает панель промптов; копию отдаём наружу, чтобы её нельзя было испортить.
    """
    return dict(_skipped), _stats_since


def _int_setting(key: str, default: int) -> int:
    """settings хранит строки — безопасно приводим к int с фолбэком на дефолт."""
    try:
        return int(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """Глобальный тумблер проактивного участия (settings: proactive_enabled)."""
    return get_setting("proactive_enabled", PROACTIVE_ENABLED_DEFAULT) == "1"


def hands_enabled() -> bool:
    """
    Тумблер «руки» (settings: proactive_hands, по умолчанию ВЫКЛ): разрешено ли
    боту самому выдавать короткий мут. Проверяется ДВАЖДЫ — перед запросом
    (давать ли модели блок прав) и перед выдачей мута. Второй раз обязателен:
    между запросом и ответом проходят секунды, и тумблер могли выключить.
    """
    return get_setting("proactive_hands", PROACTIVE_HANDS_DEFAULT) == "1"


def _extract_mute(answer: str) -> tuple[str, int | None]:
    """
    Вырезает из ответа модели пометку [МУТ:секунды].

    Возвращает (ответ без пометки, секунды или None). Срок обрезается потолком
    PROACTIVE_MUTE_MAX_SEC — модель может попросить сколько угодно, но получит
    не больше разрешённого.

    Пометку ищем ТОЛЬКО в видимой части (без <thought>): рассуждая вслух,
    модель может упомянуть пометку как пример — это не команда. А вот вырезаем
    из ВСЕГО текста: если пометка всё же затесалась в мысли, показывать её
    в чате всё равно незачем.
    """
    visible = strip_thoughts(answer)
    m = re.search(PROACTIVE_MUTE_PATTERN, visible, re.IGNORECASE)
    cleaned = re.sub(PROACTIVE_MUTE_PATTERN, "", answer, flags=re.IGNORECASE).strip()
    if not m:
        return cleaned, None
    try:
        seconds = int(m.group(1))
    except (TypeError, ValueError):
        return cleaned, None
    if seconds <= 0:
        return cleaned, None
    return cleaned, min(seconds, PROACTIVE_MUTE_MAX_SEC)


async def _apply_mute(bot, chat_id: int, user_id: int | None, seconds: int,
                      trigger_text: str, message_id: int | None = None) -> bool:
    """
    Выдаёт мут, решённый моделью, и уведомляет владельцев.

    Цель НЕ выбирается моделью: user_id приходит из апдейта — это автор
    сообщения, на которое бот отвечает. Так «ткнуть не в того» невозможно
    даже при полностью выдуманном ответе.

    actor_id=None намеренно: в antispam._manual_guard это означает «персонал
    бота не трогаем» — владельцы и модераторы защищены от мута моделью.

    ⚠️ УДАЛЯЕТ СООБЩЕНИЕ, ЗА КОТОРОЕ ВЫДАН МУТ (2026-08-11, просьба Максима:
    «сообщение не удалилось, хотя такие функции делали когда-то»). Такие
    функции и правда есть — но у АВТОМАТИКИ антиспама (_delete_messages при
    флуде и ссылках); путь «мут решила модель» их не звал вовсе, и грубость
    оставалась висеть в чате при замолчавшем авторе.

    Возвращает True, если сообщение удалено, — вызывающий по этому признаку
    отправляет реплику БЕЗ Reply: ссылаться на удалённое незачем.
    Удаление тихое: нет прав или сообщение уже стёрли — мут всё равно выдан.
    """
    if not user_id:
        return False
    from services.antispam import mute_user, notify_owners_ai_mute

    err = await mute_user(bot, chat_id, user_id, seconds,
                          name=str(user_id), admin_name="бот (сам)",
                          actor_id=None, action="mute_ai")
    if err:
        logger.warning("🤖 Чат %s: бот решил замутить %s, но не вышло: %s", chat_id, user_id, err)
        return False

    logger.info("🤖 Чат %s: бот сам выдал мут %s на %d сек", chat_id, user_id, seconds)
    await notify_owners_ai_mute(bot, chat_id, user_id, str(user_id), seconds, trigger_text)

    deleted = False
    if message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted = True
            logger.info("🤖 Чат %s: сообщение %s, за которое выдан мут, удалено", chat_id, message_id)
        except Exception as e:
            logger.warning("🤖 Чат %s: мут выдан, но сообщение %s удалить не вышло: %s",
                           chat_id, message_id, e)
    return deleted


def note_bot_group_reply(chat_id: int) -> None:
    """
    Бот только что ответил в группе НАПРЯМУЮ (упоминание/Reply): обновляем
    метку «последней реплики», чтобы он не влезал сам сразу после того,
    как уже говорил. Зовётся из handlers/messages.py.
    """
    _last_reply_ts[chat_id] = time.monotonic()


def forget_conversations() -> None:
    """
    Кнопка «🧹Очистить РАЗГОВОРЫ» (панель промптов) подвела черту под
    стенограммой — сбрасываем и счётчик новых сообщений в памяти. Иначе чат,
    где порог уже накопился, тут же получил бы проверку по пустой стенограмме.

    Паузы (`_last_judge_ts`, `_last_reply_ts`) НЕ сбрасываем намеренно: их
    обнуление только приблизило бы такую проверку. Защёлку `_in_flight` тоже
    не трогаем — идущая проверка снимет её сама, а ручное снятие открыло бы
    дорогу второй параллельной.
    """
    _msgs_since_check.clear()


def consider_message(update, context) -> None:
    """
    Точка входа: зовётся в конце collect_group_message на КАЖДОЕ сообщение
    группы. Полностью синхронная (архиватор блокирующий — между проверкой
    и защёлкой нет await, значит нет гонки) и очень дешёвая: до settings
    доходит только после отсева в памяти. Сама проверка моделью уходит
    отдельной задачей — очередь апдейтов её не ждёт.
    """
    try:
        message = update.message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat or not user:
            return
        chat_id = chat.id

        # Каждое сообщение чата приближает следующую проверку — в том числе
        # команды и обращения к боту (это тоже живость беседы).
        _msgs_since_check[chat_id] = _msgs_since_check.get(chat_id, 0) + 1

        # ── Дешёвые фильтры (память, без БД) ──
        # Каждый отсев отмечаем в счётчике: порядок проверок НЕ менялся,
        # добавились только строчки _skip(...) — они ничего не читают.
        if chat_id in _in_flight:
            # ⚠️ ДОГОНЯЮЩЕЙ ПРОВЕРКИ БОЛЬШЕ НЕТ (заведена и убрана 2026-08-11 по
            # решению Максима: «работает как-то неправильно»). Была попытка не
            # терять сообщения, пришедшие во время проверки, — бот делал по
            # последнему из них ещё один проход. Сообщения при этом и так не
            # пропадают из виду: счётчик растёт ВЫШЕ этой проверки, и следующая
            # проверка увидит их в стенограмме. Не заводить заново без просьбы.
            _skip("в полёте")
            return
        if user.is_bot:
            # Сообщения других ботов не триггерят проверку — защита от
            # «пинг-понга» двух авто-отвечающих ботов в одном чате.
            _skip("другой бот")
            return
        text = message.text or message.caption or ""
        if text.startswith("/"):
            _skip("команда")
            return
        from services.user_settings import ai_ignored
        if ai_ignored(user.id):
            # В карточке пользователя (/users) стоит «бот игнорирует»: не
            # вступаем в разговор в ответ на его сообщение — иначе игнор
            # обходился бы проактивным режимом.
            _skip("игнор")
            return
        if should_respond_in_group(update, context.bot.username):
            # Прямое обращение (упоминание/Reply) — ответит основной путь.
            _skip("прямое обращение")
            return

        # ── Фильтры по настройкам (быстрые чтения settings) ──
        if not is_enabled():
            _skip("режим выключен")
            return
        now = time.monotonic()
        if _msgs_since_check[chat_id] < _int_setting("proactive_min_msgs", PROACTIVE_MIN_MSGS):
            _skip("недобор порога")
            return

        # ── Защёлка ДО запуска задачи (никаких await между проверкой и ней) ──
        _msgs_since_check[chat_id] = 0
        _last_judge_ts[chat_id] = now
        _in_flight.add(chat_id)

        # Инфо о медиа-вложениях собирает _media_args (2026-08-11 вынесено в
        # функцию: тот же сбор нужен очереди пропущенных сообщений, а два
        # одинаковых куска кода неизбежно разъедутся).
        (has_photo, photo_file_id, has_voice, voice_file_id,
         has_video, video_file_id, video_mime) = _media_args(message, chat_id)

        try:
            context.application.create_task(
                _run_proactive(context.bot, chat_id, message.message_id, text,
                               user.id, has_photo, photo_file_id, has_voice, voice_file_id,
                               has_video, video_file_id, video_mime)
            )
        except Exception:
            _in_flight.discard(chat_id)
            raise
    except Exception as e:
        logger.debug("🤖 Ошибка проактивной проверки: %s", e)


def _media_args(message, chat_id: int) -> tuple:
    """
    Что за вложение пришло и какие file_id отдавать проверке.
    Вынесено из consider_message 2026-08-11 — тем же кодом пользуется очередь
    пропущенных сообщений (_pending), иначе сбор пришлось бы дублировать.
    """
    has_photo = bool(message.photo)
    has_voice = bool(message.voice or message.audio)
    # Видео (2026-07-24) берём ТОЛЬКО в пределах PROACTIVE_VIDEO_MAX_BYTES:
    # проактив срабатывает на каждое видео в группе, и тяжёлые ролики тут
    # разбирать незачем (см. комментарий к константе в config.py).
    # Размер неизвестен (file_size пуст) — считаем, что ролик слишком большой:
    # лучше пропустить, чем вслепую тянуть 20 МБ на каждой проверке.
    _video = message.video
    has_video = bool(_video) and bool(_video.file_size) and _video.file_size <= PROACTIVE_VIDEO_MAX_BYTES

    # ⚠️ ПОЧЕМУ ПРОПУСТИЛИ — ГОВОРИМ ВСЛУХ (2026-08-10, после живого теста
    # Максима: он прислал ролик на 24 МБ, бот молча его не заметил, и в
    # логе стояло только «триггер пуст после анализа медиа»). Отказ без
    # причины неотличим от поломки — а тут отказ штатный.
    if _video and not has_video:
        if not _video.file_size:
            logger.info("🤖 Чат %s: Telegram не сообщил размер видео — разбор пропущен "
                        "(тянуть вслепую до 20 МБ на каждой проверке незачем)", chat_id)
        else:
            logger.info("🤖 Чат %s: видео %.1f МБ больше потолка %.0f МБ — разбор пропущен. "
                        "Выше не поднять: столько отдаёт ботам сам Telegram",
                        chat_id,
                        _video.file_size / (1024 * 1024),
                        PROACTIVE_VIDEO_MAX_BYTES / (1024 * 1024))
    photo_file_id = message.photo[-1].file_id if has_photo else None
    voice_file_id = (message.voice or message.audio).file_id if has_voice else None
    video_file_id = _video.file_id if has_video else None
    video_mime = (_video.mime_type or "video/mp4") if has_video else "video/mp4"
    return (has_photo, photo_file_id, has_voice, voice_file_id,
            has_video, video_file_id, video_mime)


async def _run_proactive(bot, chat_id: int, trigger_message_id: int, trigger_text: str,
                         trigger_user_id: int | None = None,
                         has_photo: bool = False, photo_file_id: str | None = None,
                         has_voice: bool = False, voice_file_id: str | None = None,
                         has_video: bool = False, video_file_id: str | None = None,
                         video_mime: str = "video/mp4"):
    """
    Фоновая часть: активная думающая модель И решает «вступать или молчать»,
    И пишет реплику (одношаговая схема). Статус «печатает…» НЕ включается
    на время самой проверки: большинство проверок кончаются тишиной, и группа
    постоянно видела бы «печатает…» без ответа. Он включается только после
    решения вступить — короткой «человеческой» паузой перед отправкой.

    Если триггер — фото, голосовое или видео (2026-07-24), ДО вызова
    ask_group_proactive медиа анализируется через vision-/audio-/video-модель
    (Gemini flash-lite), и результат подставляется вместо пустой подписи.
    Так бот «видит» и «слышит» контекст перед решением — как в личке.
    Ветки взаимоисключающие (elif): в одном сообщении Telegram может быть
    только одно вложение, а два разбора подряд — лишний расход впустую.

    Каждый исход пишется в журнал проверок (`proactive_log`, 2026-07-31) —
    вступил бот или промолчал, сколько думала модель, что было триггером.
    Запись стоит В ТЕХ ЖЕ ветках, что и строки лога, чтобы статистика и лог
    не могли разъехаться.
    """
    # Вид триггера — для журнала: медиа-проверка дороже текстовой (сначала
    # разбор вложения отдельной моделью, потом сам запрос).
    trigger_kind = ("photo" if has_photo else
                    "voice" if has_voice else
                    "video" if has_video else "text")
    # Модель на момент проверки. Это ИМЕННО активная модель, а не та, что
    # в итоге ответила: если основная откажет, ответит запасная из цепочки
    # подстраховки, и в журнале останется активная. Точную модель ответа знает
    # только gemini.py, а лезть туда ради подписи в статистике не стоило.
    model_at_check = get_setting("active_model", "")
    started = time.monotonic()

    def _note(outcome: str, reply_len: int = 0) -> None:
        """Записать исход проверки. «Тихая»: журнал не должен ломать режим."""
        try:
            log_proactive_check(chat_id, outcome, model_at_check,
                                time.monotonic() - started, reply_len, trigger_kind)
        except Exception as e:
            logger.debug("🤖 Не удалось записать проверку в журнал: %s", e)

    try:
        loop = asyncio.get_running_loop()

        # ── Анализ медиа-триггера (фото / голосовое) ──
        enriched_text = trigger_text
        # Файл держим под рукой: с 2026-08-11 на медиа отвечает та же
        # модель, что его смотрит (ask_group_proactive_media), и ей нужен
        # сам файл, а не пересказ. Пусто — значит скачать не вышло.
        media_b64 = media_mime = media_kind = ""
        if has_photo and photo_file_id:
            try:
                import base64 as _b64
                logger.info("🤖 Чат %s: триггер — фото, активная модель не видит картинки → описание через Gemini",
                            chat_id)
                photo_file = await bot.get_file(photo_file_id)
                file_bytes = await photo_file.download_as_bytearray()
                image_base64 = _b64.b64encode(file_bytes).decode('utf-8')
                media_b64, media_mime, media_kind = image_base64, "image/jpeg", "фото"
                description = await loop.run_in_executor(
                    None, _proactive_describe_image, image_base64,
                )
                if description:
                    # ⚠️ ВРЕМЕННО БЕЗ ОБРЕЗКИ (2026-08-10, просьба Максима на
                    # время теста): было description[:120] — отметка «сработало»,
                    # а не содержимое. Вернуть [:120] вместе с отладочным блоком
                    # «🧪 ЧТО УХОДИТ МОДЕЛИ» в gemini.py: разбор бывает на
                    # полстраницы, и в обычной работе он засоряет лог.
                    logger.info("🤖 Чат %s: фото проанализировано — «%s»",
                                chat_id, description)
                    # ⚠️ ОПИСАНИЕ ОБЁРНУТО В КВАДРАТНЫЕ СКОБКИ — 2026-08-10,
                    # разбор жалобы Максима «бот шутит про ИИ, людей это бесит».
                    # ⚠️ Внутри скобок ТОЛЬКО текст модели, без приписки «на
                    # фото:» (решение Максима в тот же день): разбор и так
                    # начинается словами «На изображении…», и приписка выходила
                    # тавтологией. Формат ровно такой: «Имя: [текст модели]».
                    # Дальше по пути (gemini.py, сборка стенограммы) этот текст
                    # ПОДМЕНЯЕТ СОБОЙ пометку [фото] и достаётся модели в виде
                    # «Вася: <текст>», то есть как ПРЯМАЯ РЕЧЬ участника. Без
                    # обёртки выходило, что живой человек вдруг заговорил
                    # машинным языком («Based on the silhouettes in the image…»),
                    # и бот совершенно логично отвечал ему шуткой про робота.
                    # Скобки возвращают правду: Вася прислал картинку, а внутри
                    # — что на ней. Голосовые НЕ оборачиваются намеренно: там
                    # расшифровка и ЕСТЬ слова участника, подмена честная.
                    # Переносы строк внутри описания схлопываем в пробелы: в
                    # стенограмме ОДНО сообщение — ОДНА строка «Имя: текст», а
                    # модель любит отвечать списком, и многострочный разбор
                    # разъезжался бы на несколько мнимых реплик.
                    described = "[" + " ".join(description.split()) + "]"
                    # ⚠️ ПОДПИСЬ ВПЕРЕДИ РАЗБОРА (решение Максима 2026-08-10):
                    # «Вася: смотрите какой бой [На изображении танк…]». Сначала
                    # то, что человек написал САМ, потом служебный разбор
                    # картинки — так строка читается как реплика с приложением,
                    # а не как машинный текст, к которому приписали пару слов.
                    # Через пробел, а не с новой строки: одно сообщение — одна
                    # строка стенограммы.
                    enriched_text = f"{trigger_text} {described}" if trigger_text else described
            except Exception as e:
                logger.debug("🤖 Чат %s: не удалось скачать/проанализировать фото: %s", chat_id, e)

        elif has_voice and voice_file_id:
            try:
                import base64 as _b64
                logger.info("🤖 Чат %s: триггер — голосовое, активная модель не принимает аудио → расшифровка через Gemini",
                            chat_id)
                voice_file = await bot.get_file(voice_file_id)
                file_bytes = await voice_file.download_as_bytearray()
                audio_base64 = _b64.b64encode(file_bytes).decode('utf-8')
                media_b64, media_mime, media_kind = audio_base64, "audio/ogg", "голосовое"
                transcription = await loop.run_in_executor(
                    None, _proactive_transcribe_audio, audio_base64,
                )
                if transcription:
                    logger.info("🤖 Чат %s: голосовое расшифровано — «%s»",
                                chat_id, transcription)   # ⚠️ ВРЕМЕННО без [:120], см. фото выше
                    enriched_text = transcription
            except Exception as e:
                logger.debug("🤖 Чат %s: не удалось скачать/расшифровать голосовое: %s", chat_id, e)

        elif has_video and video_file_id:
            try:
                import base64 as _b64
                logger.info("🤖 Чат %s: триггер — видео → краткое описание через Gemini", chat_id)
                video_file = await bot.get_file(video_file_id)
                file_bytes = await video_file.download_as_bytearray()
                video_base64 = _b64.b64encode(file_bytes).decode('utf-8')
                media_b64, media_mime, media_kind = video_base64, video_mime, "видео"
                description = await loop.run_in_executor(
                    None, _proactive_describe_video, video_base64, video_mime,
                )
                if description:
                    logger.info("🤖 Чат %s: видео проанализировано — «%s»",
                                chat_id, description)   # ⚠️ ВРЕМЕННО без [:120], см. фото выше
                    # Описание ПОВЕРХ подписи — как у фото, и так же в скобках
                    # (2026-08-10): причина та же, описание ролика — не слова
                    # участника. Подробности у фото выше.
                    described = "[" + " ".join(description.split()) + "]"
                    enriched_text = f"{trigger_text} {described}" if trigger_text else described
            except Exception as e:
                logger.debug("🤖 Чат %s: не удалось скачать/проанализировать видео: %s", chat_id, e)

        # ⚠️ РАЗБОР ОСЕДАЕТ В АРХИВЕ ГРУППЫ (2026-08-10, просьба Максима «в
        # стенограмме модель должна ВСЕГДА видеть дословную расшифровку, а не
        # [голосовое]»). До этого разбор жил только в памяти одной проверки:
        # стенограмма подставляла его ТОЛЬКО последнему сообщению, а всё, что
        # уехало вглубь истории, снова превращалось в «[голосовое]» и «[фото]»
        # — модель забывала, о чём был разговор двумя репликами выше.
        # Пишем ровно то, что уходит модели сейчас (подпись + разбор), чтобы
        # стенограмма читалась одинаково и в момент триггера, и потом.
        # «Тихо»: не смогли записать — проверка всё равно идёт своим ходом.
        if enriched_text.strip() and enriched_text != trigger_text:
            try:
                from database.history import update_last_group_message_text
                update_last_group_message_text(chat_id, trigger_user_id, enriched_text)
            except Exception as e:
                logger.debug("🤖 Чат %s: не удалось сохранить разбор медиа в архив: %s", chat_id, e)

        if not enriched_text.strip():
            logger.info("🤖 Чат %s: триггер пуст после анализа медиа — пропускаем", chat_id)
            return

        # ⚡ НА МЕДИА ОТВЕЧАЕТ ТА ЖЕ МОДЕЛЬ, ЧТО ЕГО СМОТРИТ (2026-08-11,
        # решение Максима). Активная модель картинок не видит вовсе и до этой
        # правки отвечала ПО ПЕРЕСКАЗУ — по строке разбора в стенограмме;
        # именно пересказ породил шутки про ИИ над живыми людьми 10 августа.
        # Теперь файл уходит Gemini из цепочки разбора вместе со всей
        # системной частью, и реплику пишет она.
        #
        # ⚠️ ЗАПАСНОЙ ПУТЬ ОБЯЗАТЕЛЕН: не ответила ни одна модель цепочки —
        # спрашиваем активную по стенограмме, как раньше. Разбор к этому
        # моменту уже сохранён в архив, так что ей есть что читать, и бот не
        # онемеет из-за отказа Google.
        answer = None
        if media_b64:
            answer = await loop.run_in_executor(
                None, ask_group_proactive_media, chat_id, bot.id, enriched_text,
                trigger_user_id, media_b64, media_mime, media_kind,
            )
        if answer is None:
            answer = await loop.run_in_executor(
                None, ask_group_proactive, chat_id, bot.id, enriched_text,
                trigger_user_id,
            )
        if not answer:
            logger.info("🤖 Чат %s: решение — промолчать", chat_id)
            _note("silent")
            return

        # ── Пометка мута: вырезаем из текста ДО отправки ──
        # Даже если руки успели выключить, пометку всё равно убираем —
        # показывать служебный маркер участникам нельзя ни при каких условиях.
        answer, mute_sec = _extract_mute(answer)
        if mute_sec and not hands_enabled():
            # Тумблер погасили, пока модель думала — реплику отправим, мут нет.
            logger.info("🤖 Чат %s: пометка мута отброшена — руки выключены", chat_id)
            mute_sec = None
        if not answer.strip():
            # Ответ состоял из одной пометки — говорить нечего, но наказать можно.
            logger.info("🤖 Чат %s: реплика пустая после разбора пометки", chat_id)
            if mute_sec:
                await _apply_mute(bot, chat_id, trigger_user_id, mute_sec,
                                  trigger_text, trigger_message_id)
            _note("empty")
            return

        # Мут — ДО реплики: между решением и словами человек не должен успеть
        # написать ещё пару сообщений.
        deleted = False
        if mute_sec:
            deleted = await _apply_mute(bot, chat_id, trigger_user_id, mute_sec,
                                        trigger_text, trigger_message_id)

        # Ответ приходит с блоком <thought> (в чате он станет свёрнутыми
        # «Мыслями»); паузу и лог считаем по ВИДИМОЙ части реплики.
        visible = strip_thoughts(answer)

        # Человеческая пауза с «печатает…» — длина по размеру реплики (1.5–6 с)
        delay = min(6.0, max(1.5, len(visible) / 25))
        async with keep_chat_action(bot, chat_id, "typing"):
            await asyncio.sleep(delay)

        # Reply на сообщение-триггер: даёт привязку к контексту и заодно
        # кладёт реплику в нужную тему форум-группы.
        # Сообщение-нарушение удалено — отвечать не на что: Reply на стёртое
        # показывается как «сообщение недоступно».
        await send_formatted(bot, chat_id, answer,
                             reply_to=None if deleted else trigger_message_id)

        # Свою реплику — в архив групп: следующая стенограмма увидит её («Ты: …»),
        # бот не будет повторяться. В архив — БЕЗ мыслей (как и личная память).
        try:
            save_group_message(chat_id, bot.id, bot.username or "", bot.first_name or "", visible, False)
        except Exception as e:
            logger.debug("🤖 Не удалось сохранить свою реплику в архив групп: %s", e)

        _last_reply_ts[chat_id] = time.monotonic()
        logger.info("🤖 Чат %s: бот сам вступил в разговор (%d символов)", chat_id, len(visible))
        _note("reply_mute" if mute_sec else "reply", len(visible))
    except Exception as e:
        logger.warning("⚠️ Не удалось выполнить проактивную проверку (чат %s): %s", chat_id, e)
        _note("error")
    finally:
        _in_flight.discard(chat_id)
