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
    уже говорил. Заодно обновляет паузу проактивного режима, чтобы бот не
    влезал сам сразу после прямого ответа. Мысли <thought> вырезаются.
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

    try:
        # Статус «печатает…» висит всё время анализа фото
        # (keep_chat_action обновляет его, пока модель думает).
        async with keep_chat_action(context.bot, chat_id, "typing"):
            photo_file = await message.photo[-1].get_file()
            file_bytes = await photo_file.download_as_bytearray()
            image_base64 = base64.b64encode(file_bytes).decode('utf-8')

            loop = asyncio.get_running_loop()
            answer = await loop.run_in_executor(None, ask_gemini, chat_id, user_id, user_text, image_base64)

        # Единая отправка: форматирование + безопасная нарезка длинных ответов.
        # Текст ответа в лог не пишем: модель, время и токены уже логирует
        # services/gemini.py строкой «♊️/🐪/🐋 Ответ от …» (значок провайдера).
        await send_formatted(context.bot, chat_id, answer, reply_to=message.message_id)

        # Свой ответ — в архив групп (стенограмма проактивного режима)
        if is_group:
            await _archive_bot_group_reply(context.bot, chat_id, answer)

    except Exception as e:
        logger.error("⚠️ Не удалось обработать фото: %s", e)
        await message.reply_text("❌ Произошла ошибка при анализе фотографии.")


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

    status_msg = await message.reply_text("🎧 _Слушаю голосовое..._", parse_mode=ParseMode.MARKDOWN)

    try:
        # Статус «печатает…» висит всю обработку голосового —
        # в дополнение к сообщению-статусу «Слушаю голосовое…» выше.
        async with keep_chat_action(context.bot, chat_id, "typing"):
            voice_file = await voice.get_file()
            file_bytes = await voice_file.download_as_bytearray()
            audio_base64 = base64.b64encode(file_bytes).decode('utf-8')

            loop = asyncio.get_running_loop()
            answer = await loop.run_in_executor(None, ask_gemini_audio, chat_id, user_id, audio_base64)

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except Exception:
            pass

        # Единая отправка: форматирование + безопасная нарезка длинных ответов.
        # Текст ответа в лог не пишем — см. handle_photo.
        await send_formatted(context.bot, chat_id, answer, reply_to=message.message_id)

        # Свой ответ — в архив групп (стенограмма проактивного режима).
        # Само голосовое в архив не попадает (фильтр TEXT|PHOTO), но текстовый
        # ответ бота — видимая часть беседы, его сохраняем.
        if is_group:
            await _archive_bot_group_reply(context.bot, chat_id, answer)

    except Exception as e:
        logger.error("⚠️ Не удалось обработать голосовое: %s", e)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except Exception:
            pass
        await message.reply_text("❌ Произошла ошибка при обработке голосового сообщения.")


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

    status_msg = await message.reply_text("🎬 _Смотрю видео..._", parse_mode=ParseMode.MARKDOWN)

    try:
        # Статус «печатает…» висит весь разбор — в дополнение к сообщению-статусу.
        async with keep_chat_action(context.bot, chat_id, "typing"):
            video_file = await video.get_file()
            file_bytes = await video_file.download_as_bytearray()
            video_base64 = base64.b64encode(file_bytes).decode('utf-8')
            mime = video.mime_type or "video/mp4"

            loop = asyncio.get_running_loop()
            answer = await loop.run_in_executor(
                None, ask_gemini_video, chat_id, user_id, video_base64, user_text, mime
            )

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except Exception:
            pass

        await send_formatted(context.bot, chat_id, answer, reply_to=message.message_id)

        # Свой ответ — в архив групп (стенограмма проактивного режима).
        if is_group:
            await _archive_bot_group_reply(context.bot, chat_id, answer)

    except Exception as e:
        logger.error("⚠️ Не удалось обработать видео: %s", e)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except Exception:
            pass
        await message.reply_text("❌ Произошла ошибка при разборе видео.")


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

    # Игнор проверяем ПОСЛЕ режима «Проверить поиск» (он для админа и к ИИ
    # отношения не имеет) и до всего остального.
    if _ai_ignored(user.id):
        logger.info("👤 Сообщение пропущено: бот игнорирует пользователя %s (карточка /users)", user.id)
        return

    if _ai_replies_muted_for_admin(user.id, is_group):
        logger.info("👤 Сообщение пропущено (пользователь %s): ответы ИИ выключены", user.id)
        return

    # Статус «печатает…» висит всё время, пока модель думает
    # (общая поддерживалка utils.keep_chat_action).
    async with keep_chat_action(context.bot, chat_id, "typing"):
        # Запрос к модели в отдельном потоке, чтобы не блокировать event loop
        loop   = asyncio.get_running_loop()
        answer = await loop.run_in_executor(
            None, ask_gemini, chat_id, user.id, user_text
        )

    # Единая отправка: форматирование (telegramify) + сворачиваемые мысли +
    # безопасная нарезка длинных ответов по границам entity.
    # Текст ответа в лог не пишем — см. handle_photo.
    await send_formatted(context.bot, chat_id, answer, reply_to=message.message_id)

    # Свой ответ — в архив групп (стенограмма проактивного режима)
    if is_group:
        await _archive_bot_group_reply(context.bot, chat_id, answer)


# ───────────────────────────────────────────────
#  Сборщик сообщений группы (Stage 3 — контекстный бот)
# ───────────────────────────────────────────────

async def collect_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Passthrough-хендлер: тихо сохраняет ВСЕ входящие сообщения групп в БД.
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

    # Список групп бота (карточка пользователя: где выдавать мут/кик/бан).
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
