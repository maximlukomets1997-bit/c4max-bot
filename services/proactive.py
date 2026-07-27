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
from database.history import get_setting, save_group_message
from services.gemini import (ask_group_proactive, _proactive_describe_image,
                             _proactive_transcribe_audio, _proactive_describe_video)
from utils import should_respond_in_group, keep_chat_action
from utils_format import send_formatted, strip_thoughts

logger = logging.getLogger(__name__)

# ─── состояние в памяти (обнуляется при рестарте — как у антиспама) ──
_msgs_since_check: dict[int, int] = {}    # chat_id → новых сообщений с последней проверки
_last_judge_ts: dict[int, float] = {}     # chat_id → monotonic последней проверки моделью
_last_reply_ts: dict[int, float] = {}     # chat_id → monotonic последней реплики бота
_in_flight: set[int] = set()              # чаты с проверкой «в полёте» (защёлка от гонки)


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
                      trigger_text: str) -> None:
    """
    Выдаёт мут, решённый моделью, и уведомляет владельцев.

    Цель НЕ выбирается моделью: user_id приходит из апдейта — это автор
    сообщения, на которое бот отвечает. Так «ткнуть не в того» невозможно
    даже при полностью выдуманном ответе.

    actor_id=None намеренно: в antispam._manual_guard это означает «персонал
    бота не трогаем» — владельцы и модераторы защищены от мута моделью.
    """
    if not user_id:
        return
    from services.antispam import mute_user, notify_owners_ai_mute

    err = await mute_user(bot, chat_id, user_id, seconds,
                          name=str(user_id), admin_name="бот (сам)",
                          actor_id=None, action="mute_ai")
    if err:
        logger.warning("🤖 Чат %s: бот решил замутить %s, но не вышло: %s", chat_id, user_id, err)
        return

    logger.info("🤖 Чат %s: бот сам выдал мут %s на %d сек", chat_id, user_id, seconds)
    await notify_owners_ai_mute(bot, chat_id, user_id, str(user_id), seconds, trigger_text)


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
        if chat_id in _in_flight:
            return
        if user.is_bot:
            # Сообщения других ботов не триггерят проверку — защита от
            # «пинг-понга» двух авто-отвечающих ботов в одном чате.
            return
        text = message.text or message.caption or ""
        if text.startswith("/"):
            return
        from services.user_settings import ai_ignored
        if ai_ignored(user.id):
            # В карточке пользователя (/users) стоит «бот игнорирует»: не
            # вступаем в разговор в ответ на его сообщение — иначе игнор
            # обходился бы проактивным режимом.
            return
        if should_respond_in_group(update, context.bot.username):
            # Прямое обращение (упоминание/Reply) — ответит основной путь.
            return

        # ── Фильтры по настройкам (быстрые чтения settings) ──
        if not is_enabled():
            return
        now = time.monotonic()
        if _msgs_since_check[chat_id] < _int_setting("proactive_min_msgs", PROACTIVE_MIN_MSGS):
            return

        # ── Защёлка ДО запуска задачи (никаких await между проверкой и ней) ──
        _msgs_since_check[chat_id] = 0
        _last_judge_ts[chat_id] = now
        _in_flight.add(chat_id)

        # Инфо о медиа-вложениях: если триггером стало фото, голосовое или видео,
        # передаём file_id в _run_proactive — она скачает и проанализирует
        # медиа через vision-/audio-/video-модель ДО вызова ask_group_proactive.
        has_photo = bool(message.photo)
        has_voice = bool(message.voice or message.audio)
        # Видео (2026-07-24) берём ТОЛЬКО в пределах PROACTIVE_VIDEO_MAX_BYTES:
        # проактив срабатывает на каждое видео в группе, и тяжёлые ролики тут
        # разбирать незачем (см. комментарий к константе в config.py).
        # Размер неизвестен (file_size пуст) — считаем, что ролик слишком большой:
        # лучше пропустить, чем вслепую тянуть 20 МБ на каждой проверке.
        _video = message.video
        has_video = bool(_video) and bool(_video.file_size) and _video.file_size <= PROACTIVE_VIDEO_MAX_BYTES
        photo_file_id = message.photo[-1].file_id if has_photo else None
        voice_file_id = (message.voice or message.audio).file_id if has_voice else None
        video_file_id = _video.file_id if has_video else None
        video_mime = (_video.mime_type or "video/mp4") if has_video else "video/mp4"

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
    """
    try:
        loop = asyncio.get_running_loop()

        # ── Анализ медиа-триггера (фото / голосовое) ──
        enriched_text = trigger_text
        if has_photo and photo_file_id:
            try:
                import base64 as _b64
                logger.info("🤖 Чат %s: триггер — фото, активная модель не видит картинки → описание через Gemini",
                            chat_id)
                logger.info("🤖 Запрос к модели gemini-3.1-flash-lite (описание фото для proactive)")
                photo_file = await bot.get_file(photo_file_id)
                file_bytes = await photo_file.download_as_bytearray()
                image_base64 = _b64.b64encode(file_bytes).decode('utf-8')
                description = await loop.run_in_executor(
                    None, _proactive_describe_image, image_base64,
                )
                if description:
                    logger.info("🤖 Чат %s: фото проанализировано — «%s»",
                                chat_id, description[:120])
                    # Описание ПОВЕРХ подписи: если пользователь подписал фото,
                    # подпись уже в trigger_text — добавляем описание перед ней.
                    enriched_text = f"{description}\n{trigger_text}" if trigger_text else description
            except Exception as e:
                logger.debug("🤖 Чат %s: не удалось скачать/проанализировать фото: %s", chat_id, e)

        elif has_voice and voice_file_id:
            try:
                import base64 as _b64
                logger.info("🤖 Чат %s: триггер — голосовое, активная модель не принимает аудио → расшифровка через Gemini",
                            chat_id)
                logger.info("🤖 Запрос к модели gemini-3.1-flash-lite (расшифровка аудио для proactive)")
                voice_file = await bot.get_file(voice_file_id)
                file_bytes = await voice_file.download_as_bytearray()
                audio_base64 = _b64.b64encode(file_bytes).decode('utf-8')
                transcription = await loop.run_in_executor(
                    None, _proactive_transcribe_audio, audio_base64,
                )
                if transcription:
                    logger.info("🤖 Чат %s: голосовое расшифровано — «%s»",
                                chat_id, transcription[:120])
                    enriched_text = transcription
            except Exception as e:
                logger.debug("🤖 Чат %s: не удалось скачать/расшифровать голосовое: %s", chat_id, e)

        elif has_video and video_file_id:
            try:
                import base64 as _b64
                logger.info("🤖 Чат %s: триггер — видео → краткое описание через Gemini", chat_id)
                logger.info("🤖 Запрос к модели gemini-3.1-flash-lite (описание видео для proactive)")
                video_file = await bot.get_file(video_file_id)
                file_bytes = await video_file.download_as_bytearray()
                video_base64 = _b64.b64encode(file_bytes).decode('utf-8')
                description = await loop.run_in_executor(
                    None, _proactive_describe_video, video_base64, video_mime,
                )
                if description:
                    logger.info("🤖 Чат %s: видео проанализировано — «%s»",
                                chat_id, description[:120])
                    # Описание ПОВЕРХ подписи — как у фото: подпись пользователя
                    # уже лежит в trigger_text, описание добавляем перед ней.
                    enriched_text = f"{description}\n{trigger_text}" if trigger_text else description
            except Exception as e:
                logger.debug("🤖 Чат %s: не удалось скачать/проанализировать видео: %s", chat_id, e)

        if not enriched_text.strip():
            logger.info("🤖 Чат %s: триггер пуст после анализа медиа — пропускаем", chat_id)
            return

        answer = await loop.run_in_executor(
            None, ask_group_proactive, chat_id, bot.id, enriched_text,
            trigger_user_id,
        )
        if not answer:
            logger.info("🤖 Чат %s: решение — промолчать", chat_id)
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
                await _apply_mute(bot, chat_id, trigger_user_id, mute_sec, trigger_text)
            return

        # Мут — ДО реплики: между решением и словами человек не должен успеть
        # написать ещё пару сообщений.
        if mute_sec:
            await _apply_mute(bot, chat_id, trigger_user_id, mute_sec, trigger_text)

        # Ответ приходит с блоком <thought> (в чате он станет свёрнутыми
        # «Мыслями»); паузу и лог считаем по ВИДИМОЙ части реплики.
        visible = strip_thoughts(answer)

        # Человеческая пауза с «печатает…» — длина по размеру реплики (1.5–6 с)
        delay = min(6.0, max(1.5, len(visible) / 25))
        async with keep_chat_action(bot, chat_id, "typing"):
            await asyncio.sleep(delay)

        # Reply на сообщение-триггер: даёт привязку к контексту и заодно
        # кладёт реплику в нужную тему форум-группы.
        await send_formatted(bot, chat_id, answer, reply_to=trigger_message_id)

        # Свою реплику — в архив групп: следующая стенограмма увидит её («Ты: …»),
        # бот не будет повторяться. В архив — БЕЗ мыслей (как и личная память).
        try:
            save_group_message(chat_id, bot.id, bot.username or "", bot.first_name or "", visible, False)
        except Exception as e:
            logger.debug("🤖 Не удалось сохранить свою реплику в архив групп: %s", e)

        _last_reply_ts[chat_id] = time.monotonic()
        logger.info("🤖 Чат %s: бот сам вступил в разговор (%d символов)", chat_id, len(visible))
    except Exception as e:
        logger.warning("⚠️ Не удалось выполнить проактивную проверку (чат %s): %s", chat_id, e)
    finally:
        _in_flight.discard(chat_id)
