import logging
import base64
import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatType, ParseMode

from config import ADMIN_IDS, VIDEO_MAX_BYTES
from database.history import get_setting
from services.gemini import ask_gemini, ask_gemini_audio, ask_gemini_video
from utils import should_respond_in_group, clean_mention, keep_chat_action
from utils_format import send_formatted

logger = logging.getLogger(__name__)


async def _archive_bot_group_reply(bot, chat_id: int, answer: str):
    """
    Пишет ПРЯМОЙ ответ бота в группе (на упоминание/Reply) в архив групп:
    собственные сообщения бота не приходят апдейтами, и без этой записи
    стенограмма проактивного режима («Сам в разговор») не видела бы, что бот
    уже говорил. Заодно ставит метку «последней реплики» (с 2026-07-20 она
    ничего не тормозит — паузы удалены). Мысли <thought> вырезаются.
    Никогда не бросает исключений — потеря строки архива не должна ломать ответ.
    """
    try:
        from database.history import save_group_message
        from services.proactive import note_bot_group_reply
        from utils_format import strip_thoughts
        save_group_message(chat_id, bot.id, bot.username or "", bot.first_name or "",
                           strip_thoughts(answer), False)
        note_bot_group_reply(chat_id)
    except Exception as e:
        logger.debug("🤖 Не удалось сохранить ответ бота в архив групп: %s", e)


# Потолок на цитату из сообщения, на которое отвечают. Reply прилетает и на
# простыни (длинная новость, чужой пересказ), а целиком они в запросе не нужны.
_REPLY_CONTEXT_MAX = 4000


def _reply_context_block(message, bot) -> str:
    """
    Блок «на какое сообщение отвечают» (2026-08-16, решение Максима).

    Зачем: Reply Telegram присылает ОТДЕЛЬНЫМ полем, в тексте сообщения его
    нет вовсе. Из-за этого бот получал голое «а это точно?» и не знал, о чём
    речь, — заметнее всего на рассылке новостей: их он отправляет фоновой
    задачей, и в его личной переписке с человеком новости нет ни строчкой.

    Работает и в группе, и в личке, и на ответ боту, и на ответ другому
    человеку (тогда цитата подписывается его именем).

    ⚠️ У длинной новости картинки уходят ОТДЕЛЬНЫМ сообщением без подписи
    (jobs/news.py: подпись к альбому не длиннее 1024 символов). Reply на такую
    картинку текста не даёт вовсе, и блок получится пустым. С 2026-08-20 этот
    случай закрывает КОНТЕКСТ: сводка ложится в память получателя при отправке
    (jobs/news.py) и видна там, пока не уедет из окна.

    Возвращает готовый блок или "" (не Reply / пустая цитата / любая ошибка —
    справка не должна ронять ответ).
    """
    try:
        src = getattr(message, "reply_to_message", None)
        if src is None:
            return ""
        text = (src.text or src.caption or "").strip()
        if not text:
            return ""
        if len(text) > _REPLY_CONTEXT_MAX:
            text = text[:_REPLY_CONTEXT_MAX] + "…"

        author = getattr(src, "from_user", None)
        if author and bot and author.id == bot.id:
            whose = "на твоё собственное сообщение"
        elif author:
            name = author.first_name or (f"@{author.username}" if author.username
                                         else f"участника {author.id}")
            whose = f"на сообщение участника {name}"
        else:
            whose = "на сообщение в чате"

        return (
            "[На какое сообщение отвечают]\n"
            f"Человек отвечает {whose}:\n«{text}»"
        )
    except Exception as e:
        logger.debug("🤖 Не удалось собрать справку об ответе (Reply): %s", e)
        return ""


def _ai_replies_muted_for_admin(user_id: int, is_group: bool) -> bool:
    """
    True, если ответы ИИ выключены тумблером «💬 ОТВЕТЫ ИИ» в панели промптов
    (/adm → «⚙️ Управление PROMPTами») для лички админа.
    Глушим ТОЛЬКО приватный чат админа (чтобы общаться с Claude без помех);
    в группах и в личках обычных пользователей бот отвечает как обычно.
    """
    if is_group or user_id not in ADMIN_IDS:
        return False
    return get_setting("ai_replies_enabled", "1") == "0"


def _ai_ignored(user_id: int) -> bool:
    """
    True, если в карточке пользователя (/users) включено «бот игнорирует этого
    человека»: ИИ не отвечает ему ни в личке, ни на упоминание в группе.
    Настройка живёт в памяти (services/user_settings.py) — похода в БД нет.
    Ошибку глушим: сбой настроек не должен обрывать ответы всем подряд.
    """
    try:
        from services.user_settings import ai_ignored
        return ai_ignored(user_id)
    except Exception:
        return False


# ── Ответ «на глазах» (08.10.2026, services/live_answer.py) ──────────────
# Общие шаги для текста и фото: два одинаковых куска в двух обработчиках со
# временем разъехались бы. Выключатель один на личку и группы: в личке —
# черновик Telegram, в группе — сообщение «💭 Думаю…», которое правится по ходу.

def _start_live(bot, chat_id: int, reply_to: int, is_group: bool, label: str | None = None):
    """
    Завести показ по ходу и его насос. Выключатель выключен — (None, None).
    label — первая стадия у голосового и видео («🎧 Слушаю голосовое…»): пока
    файл расшифровывают, показ говорит её, потом — «💭 Думаю…» и текст.
    """
    from services.live_answer import LiveDraft, LiveGroupMessage, live_answer_enabled
    if not live_answer_enabled():
        return None, None
    draft = (LiveGroupMessage(chat_id, reply_to, label) if is_group
             else LiveDraft(chat_id, reply_to, label))
    return draft, asyncio.create_task(draft.run(bot))


async def _stop_live(draft, pump, chat_id: int) -> None:
    """
    Погасить насос ДО готового ответа: запоздалое обновление после него снова
    показало бы недописанный текст. Мягко — начатая отправка доходит до конца
    (см. LiveDraft.run); повисла — отменяем. Повторный вызов безвреден.
    """
    if pump is None:
        return
    draft.stop()
    try:
        await asyncio.wait_for(pump, timeout=10)
    except Exception:
        pump.cancel()
    logger.debug("💬 Ответ на глазах: обновлений %d (чат %s)", draft.sent, chat_id)


async def _deliver(bot, chat_id: int, draft, answer: str, reply_to: int) -> None:
    """
    Отправить готовый ответ. В группе с показом по ходу — последней правкой
    того же сообщения (LiveGroupMessage.finish; его не было — finish шлёт как
    обычно), иначе — send_formatted: форматирование (telegramify), свёрнутые
    мысли и безопасная нарезка длинных ответов.
    """
    from services.live_answer import LiveGroupMessage
    if isinstance(draft, LiveGroupMessage):
        await draft.finish(bot, answer)
    else:
        await send_formatted(bot, chat_id, answer, reply_to=reply_to)


async def _reply_error(bot, message, draft, text: str) -> None:
    """
    Сообщить об ошибке разбора вложения. В группе с показом по ходу там уже
    висит «Думаю…» (или «Слушаю…») — превращаем его в ошибку, а не шлём
    второе сообщение рядом; иначе — ответом на сообщение человека, как всегда.
    """
    from services.live_answer import LiveGroupMessage
    if isinstance(draft, LiveGroupMessage) and draft.message_id is not None:
        await draft.finish(bot, text)
    else:
        await message.reply_text(text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None or not message.photo:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    bot_username = context.bot.username
    is_group = update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    if is_group and not should_respond_in_group(update, bot_username):
        return

    user_text = message.caption or ""
    if is_group:
        user_text = clean_mention(user_text, bot_username)

    user = update.effective_user
    uname = f"@{user.username}" if user.username else "без ника"
    logger.info("👤 Фото от %s (%s): %s", user.full_name, uname, user_text or "(без подписи)")

    if _ai_ignored(user_id):
        logger.info("👤 Фото пропущено: бот игнорирует пользователя %s (карточка /users)", user_id)
        return

    if _ai_replies_muted_for_admin(user_id, is_group):
        logger.info("👤 Сообщение пропущено (пользователь %s): ответы ИИ выключены", user_id)
        return

    # Ответ «на глазах» — с самого начала: «Думаю…» закрывает и скачивание фото.
    draft, pump = _start_live(context.bot, chat_id, message.message_id, is_group)
    delivered = False
    try:
        # Статус «печатает…» висит всё время анализа фото
        # (keep_chat_action обновляет его, пока модель думает).
        async with keep_chat_action(context.bot, chat_id, "typing"):
            photo_file = await message.photo[-1].get_file()
            file_bytes = await photo_file.download_as_bytearray()
            image_base64 = base64.b64encode(file_bytes).decode('utf-8')

            loop = asyncio.get_running_loop()
            # Пятым аргументом — на какое сообщение отвечают (пусто, если это
            # не Reply): фото тоже присылают ответом на чужое сообщение.
            from functools import partial
            try:
                answer = await loop.run_in_executor(
                    None, partial(ask_gemini, chat_id, user_id, user_text, image_base64,
                                  _reply_context_block(message, context.bot), progress=draft)
                )
            finally:
                await _stop_live(draft, pump, chat_id)

        # Текст ответа в лог не пишем: модель, время и токены уже логирует
        # services/gemini.py строкой «♊️/🐪/🐋/🍚 Ответ от …» (значок провайдера).
        await _deliver(context.bot, chat_id, draft, answer, message.message_id)
        delivered = True

        # Свой ответ — в архив групп (стенограмма проактивного режима)
        if is_group:
            await _archive_bot_group_reply(context.bot, chat_id, answer)

    except Exception as e:
        logger.error("⚠️ Не удалось обработать фото: %s", e)
        if delivered:
            return  # ответ уже у человека — поверх него ошибку не показываем
        await _stop_live(draft, pump, chat_id)   # сорвалось ещё до модели (скачивание)
        await _reply_error(context.bot, message, draft, "❌ Произошла ошибка при анализе фотографии.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сначала проверяем, что сообщение вообще есть (у отредактированных
    # сообщений и постов каналов update.message пуст), и только потом
    # достаём из него голосовое — иначе AttributeError.
    message = update.message
    if message is None:
        return
    voice = message.voice or message.audio
    if not voice:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    bot_username = context.bot.username
    is_group = update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    if is_group and not should_respond_in_group(update, bot_username):
        return

    user = update.effective_user
    uname = f"@{user.username}" if user.username else "без ника"
    logger.info("👤 Голосовое от %s (%s)", user.full_name, uname)

    if _ai_ignored(user_id):
        logger.info("👤 Голосовое пропущено: бот игнорирует пользователя %s (карточка /users)", user_id)
        return

    if _ai_replies_muted_for_admin(user_id, is_group):
        logger.info("👤 Сообщение пропущено (пользователь %s): ответы ИИ выключены", user_id)
        return

    # Ответ «на глазах» (09.10.2026): показ сам проходит стадии «🎧 Слушаю
    # голосовое…» → «💭 Думаю…» → текст; в группе это одно сообщение, которое
    # становится ответом. Отдельный статус «Слушаю…» с удалением — только
    # при выключенном выключателе, как было.
    draft, pump = _start_live(context.bot, chat_id, message.message_id, is_group,
                              label="🎧 Слушаю голосовое…")
    status_msg = None
    if draft is None:
        status_msg = await message.reply_text("🎧 _Слушаю голосовое..._", parse_mode=ParseMode.MARKDOWN)
    delivered = False

    try:
        # Статус «печатает…» висит всю обработку голосового —
        # в дополнение к показу «Слушаю голосовое…» выше.
        async with keep_chat_action(context.bot, chat_id, "typing"):
            voice_file = await voice.get_file()
            file_bytes = await voice_file.download_as_bytearray()
            audio_base64 = base64.b64encode(file_bytes).decode('utf-8')

            loop = asyncio.get_running_loop()
            from functools import partial
            try:
                answer = await loop.run_in_executor(
                    None, partial(ask_gemini_audio, chat_id, user_id, audio_base64,
                                  progress=draft))
            finally:
                await _stop_live(draft, pump, chat_id)

        if status_msg is not None:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
            except Exception:
                pass

        # Единая отправка (см. _deliver). Текст ответа в лог не пишем — см. handle_photo.
        await _deliver(context.bot, chat_id, draft, answer, message.message_id)
        delivered = True

        # Свой ответ — в архив групп (стенограмма проактивного режима).
        # Само голосовое архивирует collect_group_message, а текстовый ответ
        # бота апдейтом не приходит — его сохраняем здесь.
        if is_group:
            await _archive_bot_group_reply(context.bot, chat_id, answer)

    except Exception as e:
        logger.error("⚠️ Не удалось обработать голосовое: %s", e)
        if delivered:
            return  # ответ уже у человека — поверх него ошибку не показываем
        await _stop_live(draft, pump, chat_id)   # сорвалось ещё до модели (скачивание)
        if status_msg is not None:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
            except Exception:
                pass
        await _reply_error(context.bot, message, draft,
                           "❌ Произошла ошибка при обработке голосового сообщения.")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Разбор ВИДЕО (2026-07-24). Устроен как handle_voice, с двумя отличиями:
      • берётся ТОЛЬКО message.video — кружочки (video_note) и гифки (animation)
        обрабатывать Максим не захотел, обработчик на них и не зарегистрирован;
      • перед скачиванием проверяется размер: Telegram не отдаёт ботам файлы
        больше 20 МБ (VIDEO_MAX_BYTES), и молча падать на этом нельзя —
        человек должен понять, почему бот не ответил.
    Подпись к видео (caption) уходит модели как вопрос пользователя.
    """
    message = update.message
    if message is None:
        return
    video = message.video
    if not video:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    bot_username = context.bot.username
    is_group = update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    if is_group and not should_respond_in_group(update, bot_username):
        return

    user_text = message.caption or ""
    if is_group:
        user_text = clean_mention(user_text, bot_username)

    user = update.effective_user
    uname = f"@{user.username}" if user.username else "без ника"
    logger.info("👤 Видео от %s (%s): %s", user.full_name, uname, user_text or "(без подписи)")

    if _ai_ignored(user_id):
        logger.info("👤 Видео пропущено: бот игнорирует пользователя %s (карточка /users)", user_id)
        return

    if _ai_replies_muted_for_admin(user_id, is_group):
        logger.info("👤 Сообщение пропущено (пользователь %s): ответы ИИ выключены", user_id)
        return

    # Размер известен ДО скачивания — проверяем сразу, чтобы не тянуть впустую.
    # file_size у Telegram иногда отсутствует: тогда пробуем скачать, и если
    # файл окажется больше лимита, get_file упадёт — это ловит общий except.
    if video.file_size and video.file_size > VIDEO_MAX_BYTES:
        mb = video.file_size / (1024 * 1024)
        logger.info("👤 Видео отклонено: %.1f МБ больше лимита Telegram", mb)
        await message.reply_text(
            f"🎬 Видео слишком большое ({mb:.1f} МБ).\n"
            f"Telegram не отдаёт ботам файлы больше 20 МБ — пришли ролик покороче."
        )
        return

    # Ответ «на глазах» — как у голосового: «🎬 Смотрю видео…» → «💭 Думаю…»
    # → текст; отдельный статус с удалением — только при выключенном выключателе.
    draft, pump = _start_live(context.bot, chat_id, message.message_id, is_group,
                              label="🎬 Смотрю видео…")
    status_msg = None
    if draft is None:
        status_msg = await message.reply_text("🎬 _Смотрю видео..._", parse_mode=ParseMode.MARKDOWN)
    delivered = False

    try:
        # Статус «печатает…» висит весь разбор — в дополнение к показу «Смотрю видео…».
        async with keep_chat_action(context.bot, chat_id, "typing"):
            video_file = await video.get_file()
            file_bytes = await video_file.download_as_bytearray()
            video_base64 = base64.b64encode(file_bytes).decode('utf-8')
            mime = video.mime_type or "video/mp4"

            loop = asyncio.get_running_loop()
            from functools import partial
            try:
                answer = await loop.run_in_executor(
                    None, partial(ask_gemini_video, chat_id, user_id, video_base64, user_text,
                                  mime, progress=draft))
            finally:
                await _stop_live(draft, pump, chat_id)

        if status_msg is not None:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
            except Exception:
                pass

        await _deliver(context.bot, chat_id, draft, answer, message.message_id)
        delivered = True

        # Свой ответ — в архив групп (стенограмма проактивного режима).
        if is_group:
            await _archive_bot_group_reply(context.bot, chat_id, answer)

    except Exception as e:
        logger.error("⚠️ Не удалось обработать видео: %s", e)
        if delivered:
            return  # ответ уже у человека — поверх него ошибку не показываем
        await _stop_live(draft, pump, chat_id)   # сорвалось ещё до модели (скачивание)
        if status_msg is not None:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
            except Exception:
                pass
        await _reply_error(context.bot, message, draft, "❌ Произошла ошибка при разборе видео.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    chat     = update.effective_chat
    chat_id  = chat.id
    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    # В группе — отвечаем только при обращении к боту
    if is_group:
        bot_username = context.bot.username
        if not should_respond_in_group(update, bot_username):
            return
        user_text = clean_mention(message.text, bot_username)
    else:
        user_text = message.text

    if not user_text:
        await message.reply_text("Напиши мне что-нибудь 😊")
        return

    user = update.effective_user
    uname = f"@{user.username}" if user.username else "без ника"
    logger.info("👤 Сообщение от %s (%s): %s", user.full_name, uname, user_text)

    # Режим «🔍 Проверить поиск» панели RAG: вопрос админа в личке уходит в
    # диагностику поиска, а не в ИИ (включается кнопкой в /rag, работает
    # даже при выключенных ответах ИИ — поэтому проверяется до тумблера).
    # Режим проверки поиска — часть панели базы знаний, а она только у владельца.
    from services.roles import is_owner
    if not is_group and is_owner(user.id) and context.user_data.get("kb_test_mode"):
        from handlers.admin import handle_kb_test_query
        await handle_kb_test_query(update, context, user_text)
        return

    # Экран «💰 Счета и квоты» (панель API) ждёт число — остаток на счету или
    # квоту токенов. Сообщение уходит туда, а не в ИИ. Как и проверка поиска,
    # это часть панели владельца, поэтому стоит до тумблера «ответы ИИ».
    # Ожидание снимается кнопкой «Отмена», любой другой кнопкой (router.py)
    # и любой командой (log_incoming_command).
    if not is_group and is_owner(user.id) and context.user_data.get("balance_edit"):
        from handlers.admin import handle_balance_input
        if await handle_balance_input(update, context, user_text):
            return

    # Экран «🎯 Счёт викторины» (карточка участника) ждёт число — верные ответы
    # или попытки. Устроен ровно как экран счетов выше и гаснет там же:
    # кнопкой «Отмена», любой другой кнопкой (router.py) и любой командой.
    if not is_group and is_owner(user.id) and context.user_data.get("quiz_edit"):
        from handlers.admin import handle_quiz_score_input
        if await handle_quiz_score_input(update, context, user_text):
            return

    # Игнор проверяем ПОСЛЕ режима «Проверить поиск» (он для админа и к ИИ
    # отношения не имеет) и до всего остального.
    if _ai_ignored(user.id):
        logger.info("👤 Сообщение пропущено: бот игнорирует пользователя %s (карточка /users)", user.id)
        return

    if _ai_replies_muted_for_admin(user.id, is_group):
        logger.info("👤 Сообщение пропущено (пользователь %s): ответы ИИ выключены", user.id)
        return

    # 🧩 Склейка сообщений подряд (09.10.2026, services/message_batch.py):
    # серия сообщений одного человека — один вопрос модели и один ответ.
    # Регулятор 0 — склейки нет, ответ сразу, как раньше.
    from services import message_batch
    if message_batch.wait_sec() <= 0:
        await _answer_text(context, chat_id, user, is_group, [(message, user_text)])
        return

    async def on_open(first):
        # Пачка начала копиться. В личке — сразу черновик «💭 Думаю…» (если
        # включён «ответ на глазах»), чтобы ожидание не выглядело зависанием;
        # в группе сообщение «Думаю…» появится, только когда пачка уйдёт
        # модели, — ответом на ПОСЛЕДНЕЕ сообщение пачки.
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        if is_group:
            return None
        return _start_live(context.bot, chat_id, first[0].message_id, is_group)

    async def on_flush(items, opened):
        await _answer_text(context, chat_id, user, is_group, items, opened)

    message_batch.submit((chat_id, user.id), (message, user_text), on_open, on_flush)


async def _answer_text(context, chat_id: int, user, is_group: bool, items: list,
                       live=None) -> None:
    """
    Ответ модели на одно текстовое сообщение или на склеенную пачку
    (items — [(message, текст)] по порядку). Модели — все тексты отдельными
    строками; ответ — на ПОСЛЕДНЕЕ сообщение. live — показ «на глазах»,
    заведённый ещё до ожидания пачки (личка), иначе заводится здесь.
    """
    # Пока бот ждал, не допишет ли человек ещё, антиспам мог замутить его за
    # флуд — тогда отвечать на эти сообщения нельзя (их уже и удалили). А
    # фильтр ссылок мог удалить часть пачки — такие сообщения в вопрос модели
    # не берём (09.10.2026); удалены все — молчим.
    if is_group:
        from services.antispam import is_muted_now, was_deleted
        if is_muted_now(chat_id, user.id):
            logger.info("🧩 Пачка сообщений пропущена: %s замучен в чате %s", user.id, chat_id)
            if live:
                await _stop_live(live[0], live[1], chat_id)
            return
        kept = [it for it in items if not was_deleted(chat_id, it[0].message_id)]
        if len(kept) < len(items):
            logger.info("🧩 Из пачки выкинуто удалённых модерацией сообщений: %d из %d (чат %s)",
                        len(items) - len(kept), len(items), chat_id)
        if not kept:
            if live:
                await _stop_live(live[0], live[1], chat_id)
            return
        items = kept

    message = items[-1][0]

    user_text = "\n".join(text for _, text in items)
    # На какое сообщение отвечают: берём справку у ПОСЛЕДНЕГО сообщения пачки,
    # у которого она есть (Reply обычно стоит на одном из них).
    reply_block = ""
    for msg, _ in reversed(items):
        reply_block = _reply_context_block(msg, context.bot)
        if reply_block:
            break

    # 💬 Ответ «на глазах» — см. _start_live.
    draft, pump = live if live else _start_live(context.bot, chat_id, message.message_id, is_group)

    # Статус «печатает…» висит всё время, пока модель думает
    # (общая поддерживалка utils.keep_chat_action).
    async with keep_chat_action(context.bot, chat_id, "typing"):
        # Запрос к модели в отдельном потоке, чтобы не блокировать event loop
        loop   = asyncio.get_running_loop()
        # Четвёртым аргументом картинки нет (None), пятым — на какое сообщение
        # отвечают: без него бот получал голое «а это точно?» без предмета.
        from functools import partial
        try:
            answer = await loop.run_in_executor(
                None, partial(ask_gemini, chat_id, user.id, user_text, None,
                              reply_block, progress=draft)
            )
        finally:
            await _stop_live(draft, pump, chat_id)

    # Единая отправка (см. _deliver). Текст ответа в лог не пишем — см. handle_photo.
    await _deliver(context.bot, chat_id, draft, answer, message.message_id)

    # Свой ответ — в архив групп (стенограмма проактивного режима)
    if is_group:
        await _archive_bot_group_reply(context.bot, chat_id, answer)


# ───────────────────────────────────────────────
#  Сборщик сообщений группы (Stage 3 — контекстный бот)
# ───────────────────────────────────────────────

async def collect_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Passthrough-хендлер: тихо сохраняет в БД текст, фото, звук и видео групп.
    Не отвечает пользователю — только архивирует для будущего анализа контекста.
    Работает в handler group=1 параллельно с основными хендлерами.
    """
    message = update.message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    text = message.text or message.caption or ""
    has_photo = bool(message.photo)
    has_voice = bool(message.voice or message.audio)
    has_video = bool(message.video)

    # Лог КАЖДОГО входящего сообщения группы (архив для будущей разработки).
    if has_voice:
        type_mark = "[голосовое] "
    elif has_photo:
        type_mark = "[фото] "
    elif has_video:
        type_mark = "[видео] "
    else:
        type_mark = ""
    uname = f"@{user.username}" if user.username else "без ника"
    logger.info("👥 Чат %s | %s (%s): %s%s",
                chat.id, user.first_name or "—", uname, type_mark, text or "(без текста)")

    try:
        from database.history import save_group_message
        save_group_message(
            chat_id=chat.id,
            user_id=user.id,
            username=user.username or "",
            first_name=user.first_name or "",
            text=text,
            has_photo=has_photo,
            has_voice=has_voice,
            has_video=has_video,
        )
    except Exception as e:
        logger.debug("👥 Не удалось сохранить групповое сообщение: %s", e)

    # Личное дело: +1 сообщение (вечный счётчик — стаж и активность для
    # статуса «проверенный», см. services/antispam.py::trust_info).
    # Имя и ник пишутся туда же: список пользователей в админ-панели берёт их
    # из дела, а архив групп чистится каждые 10 дней.
    try:
        from database.history import dossier_add_message
        dossier_add_message(user.id, user.username or "", user.first_name or "")
    except Exception as e:
        logger.debug("👥 Не удалось обновить личное дело: %s", e)

    # Список своих групп: обновляет название и время. Чужая группа сюда не
    # попадёт — её сообщения останавливает заслон services/group_guard.py.
    try:
        from database.history import remember_chat
        remember_chat(chat.id, chat.title or "")
    except Exception as e:
        logger.debug("👥 Не удалось запомнить группу: %s", e)

    # Антиспам: проверяем частоту сообщений и при флуде выдаём временный мут.
    # Полностью самодостаточно и «тихо» — не влияет на архивацию выше.
    muted = False
    try:
        from services.antispam import check_and_mute
        # media_group_id — номер альбома: Telegram шлёт альбом фотографий как
        # несколько сообщений, антиспам считает их за одно отправление.
        muted = await check_and_mute(context.bot, chat.id, user, text,
                                     message_id=message.message_id, has_photo=has_photo,
                                     media_group_id=message.media_group_id or "")
    except Exception as e:
        logger.debug("🛡 Ошибка антиспама в collect_group_message: %s", e)

    # Фильтр ссылок (тумблер «🔗» в /mod): удаляет сообщения со ссылками не из
    # белого списка. После мута не запускаем — сообщение уже удалено всплеском.
    deleted = False
    if not muted:
        try:
            from services.antispam import check_and_delete_links
            deleted = await check_and_delete_links(context.bot, chat.id, user, message)
        except Exception as e:
            logger.debug("🛡 Ошибка фильтра ссылок в collect_group_message: %s", e)

    # Проактивное участие в разговоре («Сам в разговор», services/proactive.py):
    # бот сам решает, вступить ли в беседу. Фильтры дешёвые и синхронные, сама
    # проверка моделью уходит отдельной задачей — очередь апдейтов её не ждёт.
    # После мута или удалённой ссылки не запускаем: нельзя отвечать Reply на
    # сообщение, которого больше нет, и вообще реагировать на спам.
    if not muted and not deleted:
        try:
            from services.proactive import consider_message
            consider_message(update, context)
        except Exception as e:
            logger.debug("🤖 Ошибка проактивного модуля в collect_group_message: %s", e)
