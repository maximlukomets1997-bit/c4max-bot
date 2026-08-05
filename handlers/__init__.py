from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, ChatMemberHandler, InlineQueryHandler, PollAnswerHandler, filters
from .commands import cmd_start, cmd_help, cmd_clear, cmd_subscribe, cmd_unsubscribe, handle_unknown_command, log_incoming_command
from .admin import cmd_prompt_set, cmd_prompt_add, cmd_prompt_reset, cmd_stats, cmd_mod, cmd_adm, cmd_rag, cmd_unmute, cmd_users, handle_callback_query, cmd_news_prompt_set, cmd_news_prompt_reset, cmd_rag_prompt_set, cmd_rag_prompt_reset, cmd_proactive_prompt_set, cmd_proactive_prompt_reset, cmd_media_prompt_set, cmd_media_prompt_reset, cmd_quiz_admin, handle_kb_document
from .media import cmd_imagine
from .messages import handle_message, handle_photo, handle_voice, handle_video, collect_group_message
from .quiz import cmd_rank, handle_poll_answer
from .tech import cmd_ttx, inline_ttx
from services.greeter import on_chat_member


async def _note_chat_activity(update, context):
    """
    Отмечает, что в чатах только что кто-то написал. Один из самых частых
    обработчиков бота (срабатывает на КАЖДОЕ сообщение), поэтому внутри —
    одно присваивание и ничего больше: ни базы, ни сети, ни логов.
    Читает эту отметку самообновление (jobs.auto_update_loop), чтобы не
    перезапускать бота посреди разговора.
    Ошибки глушим: отметка активности не тот повод, чтобы ронять обработку.
    """
    try:
        from services import deploy
        deploy.note_activity()
    except Exception:
        pass


def setup_handlers(application):
    application.add_handler(CommandHandler('start', cmd_start))
    application.add_handler(CommandHandler('help', cmd_help))
    application.add_handler(CommandHandler('clear', cmd_clear))
    application.add_handler(CommandHandler('subscribe', cmd_subscribe))
    application.add_handler(CommandHandler('unsubscribe', cmd_unsubscribe))
    application.add_handler(CommandHandler('prompt_set', cmd_prompt_set))
    application.add_handler(CommandHandler('prompt_add', cmd_prompt_add))
    application.add_handler(CommandHandler('prompt_reset', cmd_prompt_reset))
    application.add_handler(CommandHandler('news_prompt_set', cmd_news_prompt_set))
    application.add_handler(CommandHandler('news_prompt_reset', cmd_news_prompt_reset))
    application.add_handler(CommandHandler('rag_prompt_set', cmd_rag_prompt_set))
    application.add_handler(CommandHandler('rag_prompt_reset', cmd_rag_prompt_reset))
    application.add_handler(CommandHandler('proactive_prompt_set', cmd_proactive_prompt_set))
    application.add_handler(CommandHandler('proactive_prompt_reset', cmd_proactive_prompt_reset))
    # 🖼 Промпт разбора медиа (2026-08-05): что вспомогательная модель делает
    # с фото/голосовым/видео перед проактивной проверкой. Один на все три вида.
    application.add_handler(CommandHandler('media_prompt_set', cmd_media_prompt_set))
    application.add_handler(CommandHandler('media_prompt_reset', cmd_media_prompt_reset))
    application.add_handler(CommandHandler('adm', cmd_adm))
    application.add_handler(CommandHandler('stats', cmd_stats))
    application.add_handler(CommandHandler('mod', cmd_mod))
    application.add_handler(CommandHandler('rag', cmd_rag))
    application.add_handler(CommandHandler('unmute', cmd_unmute))
    application.add_handler(CommandHandler('users', cmd_users))
    # 🎮 Панель викторины (2026-08-05): сборка вопросов по статьям базы знаний
    # и их одобрение. ⚠️ НЕ путать с публичной кнопкой запуска игры
    # (callback quiz_start) — здесь только управление банком вопросов.
    application.add_handler(CommandHandler('quizadm', cmd_quiz_admin))
    # block=False: генерация картинки — долгая операция (до ~90 сек), обработчик
    # не должен блокировать общую очередь апдейтов. Лимит картинок из-за этого
    # списывается ДО генерации (см. cmd_imagine) — иначе залп команд /imagine
    # прошёл бы проверку лимита весь разом.
    application.add_handler(CommandHandler('imagine', cmd_imagine, block=False))
    application.add_handler(CommandHandler('rank', cmd_rank))
    # 📊 Справочник техники (2026-08-04). block=False: первая ступень поиска —
    # чтение файлов, но вторая (семантическая) ходит в сеть за эмбеддингом
    # запроса, и держать на ней всю очередь апдейтов нельзя.
    application.add_handler(CommandHandler('ttx', cmd_ttx, block=False))
    # Инлайн-режим того же справочника: «@бот ариете» в ЛЮБОМ чате.
    # ⚠️ Пока инлайн не включён у @BotFather (/setinline), Telegram таких
    # запросов просто не присылает — обработчик молчит, и это не поломка.
    application.add_handler(InlineQueryHandler(inline_ttx, block=False))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(PollAnswerHandler(handle_poll_answer))
    # Документы в личке: используется ТОЛЬКО заменой статей базы знаний
    # (кнопка «Заменить» в панели /rag); во всех остальных случаях документ
    # молча игнорируется — как и раньше, когда обработчика документов не было.
    application.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_kb_document))
    # block=False у трёх обработчиков ниже: ответ нейросети занимает от секунд
    # до минут (цепочка фолбэка) — пока один пользователь ждёт ответ, остальные
    # апдейты (другие пользователи, кнопки панелей, антиспам) не должны стоять
    # в очереди. Всё «чувствительное к порядку» (панели, коллектор групп,
    # антиспам) остаётся блокирующим — обрабатывается строго по одному.
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice, block=False))
    # Видео (2026-07-24): ТОЛЬКО filters.VIDEO — кружочки (VIDEO_NOTE) и гифки
    # (ANIMATION) обрабатывать Максим не захотел, поэтому их здесь намеренно нет.
    application.add_handler(MessageHandler(filters.VIDEO, handle_video, block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message, block=False))
    application.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))

    # 👋 Приветствие новичков (2026-08-04): событие «участник группы сменил
    # статус». Обработчик разбирает ТОЛЬКО вступления и уходы (services/greeter.py).
    # ⚠️ Событий chat_member Telegram по умолчанию НЕ ПРИСЫЛАЕТ — они запрошены
    # явным списком allowed_updates в main.py. Уберёшь его — этот обработчик
    # молча перестанет срабатывать, без единой ошибки в логе.
    application.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Handler group=1: runs independently from group=0, captures ALL group messages for context archiving
    group_filter = (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.VIDEO) & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP)
    application.add_handler(MessageHandler(group_filter, collect_group_message), group=1)

    # Handler group=-1: центральный регистратор команд — пишет в лог КАЖДУЮ
    # входящую команду (публичные, админские, неизвестные) ДО основных
    # обработчиков. Отдельная группа: на обработку команды в group=0 не влияет.
    # Выключить лог команд = закомментировать одну эту строку.
    application.add_handler(MessageHandler(filters.COMMAND, log_incoming_command), group=-1)

    # Handler group=-2: отметка «в чатах кто-то пишет». Нужна САМООБНОВЛЕНИЮ
    # (jobs.auto_update_loop): перезапуск обрывает разговор, поэтому бот
    # обновляется только в тишину. Обработчик предельно дешёвый — одно
    # присваивание, никаких походов в базу.
    # ⚠️ ОТДЕЛЬНАЯ ГРУППА, а не -1: внутри одной группы срабатывает только
    # ПЕРВЫЙ подходящий обработчик, и filters.ALL рядом с регистратором команд
    # отобрал бы у него все команды — лог команд молча опустел бы.
    application.add_handler(MessageHandler(filters.ALL, _note_chat_activity), group=-2)
