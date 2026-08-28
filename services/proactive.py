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
    PROACTIVE_ALBUM_MAX_PHOTOS,
    PROACTIVE_ALBUM_MAX_WAIT_SEC,
    PROACTIVE_ALBUM_WAIT_SEC,
    PROACTIVE_ENABLED_DEFAULT,
    PROACTIVE_MIN_MSGS,
    PROACTIVE_HANDS_DEFAULT,
    PROACTIVE_MUTE_MAX_SEC,
    PROACTIVE_MUTE_PATTERN,
    PROACTIVE_MUTE_CLEANUP_PATTERN,
    PROACTIVE_VIDEO_MAX_BYTES,
)
from database.history import get_setting, log_proactive_check, save_group_message
from services.gemini import (ask_group_proactive, ask_group_proactive_media,
                             _describe_image,
                             _transcribe_audio, _describe_video)
from utils import should_respond_in_group, keep_chat_action
from utils_format import send_formatted, strip_thoughts

logger = logging.getLogger(__name__)

# ─── состояние в памяти (обнуляется при рестарте — как у антиспама) ──
_msgs_since_check: dict[int, int] = {}    # chat_id → новых сообщений с последней проверки
_last_judge_ts: dict[int, float] = {}     # chat_id → monotonic последней проверки моделью
_last_reply_ts: dict[int, float] = {}     # chat_id → monotonic последней реплики бота
_in_flight: set[int] = set()              # чаты с проверкой «в полёте» (защёлка от гонки)

# ─── копилка альбома (2026-08-27, просьба Максима) ───────────────────
#
#  Telegram шлёт альбом НЕ одной посылкой, а несколькими сообщениями подряд.
#  До этой правки первый кадр запускал проверку и взводил защёлку `_in_flight`,
#  а остальные упирались в неё и отсеивались с причиной «в полёте» — модель
#  видела один кадр из шести и совершенно логично молчала (живой случай
#  27.08.2026, чат -1002682757322: шесть фото в 14:41:53, одна проверка).
#
#  Теперь первый кадр открывает копилку, следующие кадры того же альбома в неё
#  складываются, а проверка ждёт (см. _collect_album) и забирает всё разом.
#
#  ⚠️ КОПИЛКА НА ЧАТ ОДНА, и это не упрощение: защёлка `_in_flight` и так не
#  даёт начаться второй проверке в том же чате, значит и второго альбома в
#  сборе быть не может. Ключ альбома всё равно храним — чтобы не подмешать в
#  проверку кадры СЛЕДУЮЩЕГО альбома, если человек шлёт их один за другим.
#
#  Как и всё остальное здесь, живёт в памяти и обнуляется перезапуском.
_albums: dict[int, dict] = {}             # chat_id → {"id", "file_ids", "message_ids", "last_add"}

# Исходы проверки человеческими словами — для строки «ИТОГ» в логе разговора.
# Коды те же, что уходят в журнал proactive_log.
_OUTCOME_RU = {
    "reply": "реплика ушла в чат",
    "reply_mute": "реплика ушла в чат, автору выдан мут",
    "silent": "бот решил промолчать",
    "empty": "реплики нет — ответ состоял из одной пометки мута",
    "error": "проверка сорвалась ошибкой",
}

# Исходы, которые в лог разговора НЕ пишутся (2026-08-20, решение Максима):
# они дублируют то, что видно строкой выше — сама реплика или слово «ПРОПУСК»
# в ответе модели. Остальные четыре пишутся: про мут, пустой ответ и сорвавшуюся
# проверку узнать больше неоткуда.
# ⚠️ Из _OUTCOME_RU их НЕ удаляли намеренно: журнал proactive_log хранит те же
# коды, и таблица остаётся единственным местом, где они переведены на
# человеческий. Удалишь — потеряешь перевод, а не только строку в логе.
_OUTCOME_NOT_LOGGED = ("reply", "silent")

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

    ⚠️ ИЩЕМ И ВЫРЕЗАЕМ РАЗНЫМИ ШАБЛОНАМИ (28.08.2026, нашли проверки
    поведения). Распознавание строгое — мут это наказание живого человека,
    по мусору его выдавать нельзя. Вырезание широкое — служебную пометку
    участники не должны видеть НИКОГДА, даже когда разобрать срок не удалось.
    Раньше шаблон был один, и всё, что он не разобрал, уезжало в чат целиком.
    """
    visible = strip_thoughts(answer)
    m = re.search(PROACTIVE_MUTE_PATTERN, visible, re.IGNORECASE)
    cleaned = re.sub(PROACTIVE_MUTE_CLEANUP_PATTERN, "", answer, flags=re.IGNORECASE).strip()
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
                      trigger_text: str, message_id: int | None = None,
                      album_message_ids: list[int] | None = None) -> bool:
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

    ⚠️ АЛЬБОМ УДАЛЯЕТСЯ ЦЕЛИКОМ (2026-08-27, решение Максима при утверждении
    правки про альбомы). album_message_ids — все кадры отправления; без них
    сносился бы один кадр из шести, а остальные пять висели бы в чате при
    замолчавшем авторе — то же самое, что чинили 11.08, только в профиль.

    Возвращает True, если сообщение-триггер удалено, — вызывающий по этому
    признаку отправляет реплику БЕЗ Reply: ссылаться на удалённое незачем.
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

    # Сносим все кадры отправления. Порядок важен только для возвращаемого
    # признака: он про сообщение-триггер, на которое иначе пошёл бы Reply.
    to_delete = list(album_message_ids or [])
    if message_id and message_id not in to_delete:
        to_delete.insert(0, message_id)

    deleted = False
    done = 0
    for _mid in to_delete:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=_mid)
            done += 1
            if _mid == message_id:
                deleted = True
        except Exception as e:
            logger.warning("🤖 Чат %s: мут выдан, но сообщение %s удалить не вышло: %s",
                           chat_id, _mid, e)
    if done:
        logger.info("🤖 Чат %s: за мут удалено сообщений: %d из %d",
                    chat_id, done, len(to_delete))
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


def _album_open(chat_id: int, album_id: str, file_id: str | None, message_id: int) -> None:
    """
    Открыть копилку под альбом, кадр-триггер кладём в неё первым.

    Зовётся из consider_message РЯДОМ с защёлкой `_in_flight` и по тем же
    правилам: между открытием копилки и запуском задачи не должно быть await.
    """
    _albums[chat_id] = {
        "id": album_id,
        "file_ids": [file_id] if file_id else [],
        "message_ids": [message_id],
        "last_add": time.monotonic(),
    }


def _album_add(chat_id: int, message) -> bool:
    """
    Положить кадр в копилку, если он из ТОГО ЖЕ альбома, что собирается сейчас.

    Возвращает True, если кадр принят, — тогда consider_message не отсеивает
    его как «в полёте»: кадр не потерян, он уедет модели вместе с остальными.

    ⚠️ ВИДЕО ИЗ СМЕШАННОГО АЛЬБОМА В КОПИЛКУ НЕ ИДЁТ (решение при утверждении
    плана 27.08.2026): у роликов свой потолок размера и своя цена разбора,
    городить оба правила внутри копилки Максим не просил. Само сообщение при
    этом всё равно считается принятым — заводить по нему отдельную проверку
    нельзя, чат уже занят.
    """
    album = _albums.get(chat_id)
    if not album or album["id"] != (message.media_group_id or ""):
        return False
    if message.photo and len(album["file_ids"]) < PROACTIVE_ALBUM_MAX_PHOTOS:
        album["file_ids"].append(message.photo[-1].file_id)
    album["message_ids"].append(message.message_id)
    album["last_add"] = time.monotonic()
    return True


async def _collect_album(chat_id: int, album_id: str) -> tuple[list[str], list[int]]:
    """
    Дождаться остальных кадров альбома и забрать копилку целиком.

    Ждём PROACTIVE_ALBUM_WAIT_SEC ТИШИНЫ — то есть отсчёт идёт от ПОСЛЕДНЕГО
    приехавшего кадра, а не от первого: Telegram растягивает доставку альбома,
    и отсчёт от первого обрезал бы хвост. Сверху всё ограничено
    PROACTIVE_ALBUM_MAX_WAIT_SEC: защёлка `_in_flight` взведена, и подвисшая
    доставка иначе держала бы чат «занятым» сколько угодно долго.

    Возвращает (file_id кадров, id сообщений альбома). Пустые списки —
    значит копилки нет, и проверка работает по одному кадру, как раньше.
    """
    started = time.monotonic()
    while True:
        album = _albums.get(chat_id)
        if not album or album["id"] != album_id:
            return [], []          # копилки нет — работаем по одному кадру
        if time.monotonic() - album["last_add"] >= PROACTIVE_ALBUM_WAIT_SEC:
            break
        if time.monotonic() - started >= PROACTIVE_ALBUM_MAX_WAIT_SEC:
            logger.info("🤖 Чат %s: альбом собирается дольше %.0f с — "
                        "берём то, что успело приехать",
                        chat_id, PROACTIVE_ALBUM_MAX_WAIT_SEC)
            break
        await asyncio.sleep(0.25)
    album = _albums.pop(chat_id, None) or {}
    return list(album.get("file_ids") or []), list(album.get("message_ids") or [])


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

        # ── Кадр альбома, который уже собирается? (2026-08-27) ──
        # Стоит ВЫШЕ счётчика и всех фильтров намеренно. Выше фильтров —
        # потому что кадр не отсеивается, а едет модели вместе с первым.
        # Выше счётчика — потому что альбом это ОДНО отправление: считать
        # шесть кадров за шесть сообщений значило бы приближать следующую
        # проверку вшестеро быстрее (так же на альбом смотрит и антиспам,
        # см. services/antispam.py::check_and_mute).
        if message.media_group_id and _album_add(chat_id, message):
            logger.info("🤖 Чат %s: кадр альбома добавлен в идущую проверку", chat_id)
            return

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

        # Первый кадр альбома открывает копилку — остальные лягут в неё веткой
        # в начале этой же функции, а проверка их дождётся (_collect_album).
        # Одиночному сообщению копилка не заводится вовсе: ждать нечего.
        album_id = message.media_group_id or ""
        if album_id:
            _album_open(chat_id, album_id, photo_file_id, message.message_id)

        try:
            context.application.create_task(
                _run_proactive(context.bot, chat_id, message.message_id, text,
                               user.id, has_photo, photo_file_id, has_voice, voice_file_id,
                               has_video, video_file_id, video_mime, album_id)
            )
        except Exception:
            _in_flight.discard(chat_id)
            _albums.pop(chat_id, None)
            raise
    except Exception as e:
        logger.debug("🤖 Ошибка проактивной проверки: %s", e)


def _media_args(message, chat_id: int) -> tuple:
    """
    Что за вложение пришло и какие file_id отдавать проверке.

    Вынесено из consider_message 2026-08-11 ради очереди пропущенных сообщений
    (_pending). Очередь в тот же день убрали (см. предупреждение в
    consider_message), и с тех пор зовущий здесь ОДИН — сама consider_message.
    Строку про второго читателя правил 27.08.2026: она пережила удаление
    очереди и с тех пор врала.
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
                         video_mime: str = "video/mp4", album_id: str = ""):
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

    album_id непустой — триггер оказался первым кадром альбома: проверка ждёт
    остальные кадры (_collect_album) и дальше идёт по ПАЧКЕ картинок. Разбор
    для стенограммы при этом всё равно ОДИН на весь альбом, и запрос за
    репликой тоже один — просто в обоих лежит несколько изображений.

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

    # Дословный лог разговора (2026-08-16, просьба Максима): шапка проверки, под
    # неё лягут разбор вложения, запрос к модели, её ответ и исход.
    # Файл — logs/chat, подробности в services/chat_log.py.
    from services import chat_log
    chat_log.note_check(chat_id, trigger_kind, trigger_user_id, trigger_text)

    def _note(outcome: str, reply_len: int = 0) -> None:
        """Записать исход проверки. «Тихая»: журнал не должен ломать режим."""
        try:
            log_proactive_check(chat_id, outcome, model_at_check,
                                time.monotonic() - started, reply_len, trigger_kind)
        except Exception as e:
            logger.debug("🤖 Не удалось записать проверку в журнал: %s", e)
        # Строка «ИТОГ» в логе разговора стоит ЗДЕСЬ, а не в каждой ветке:
        # это единственное место, через которое проходят все исходы сразу.
        # Неизвестный код пишем как есть — иначе новый исход, забытый в
        # таблице, пропал бы из лога молча.
        if outcome not in _OUTCOME_NOT_LOGGED:
            chat_log.note_outcome(_OUTCOME_RU.get(outcome, outcome))

    try:
        loop = asyncio.get_running_loop()

        # ── Сборка альбома (2026-08-27) ──
        # Триггер оказался первым кадром альбома — ждём остальные. Одиночному
        # сообщению эта ветка не стоит ничего: album_id пуст, ожидания нет.
        album_photo_ids: list[str] = []
        album_message_ids: list[int] = []
        if album_id:
            album_photo_ids, album_message_ids = await _collect_album(chat_id, album_id)
            if len(album_photo_ids) > 1:
                logger.info("🤖 Чат %s: альбом собран — %d фото в одной проверке",
                            chat_id, len(album_photo_ids))
            # ⚠️ ОТСЧЁТ ВРЕМЕНИ ПРОВЕРКИ НАЧИНАЕМ ЗАНОВО: в журнал (proactive_log)
            # уходит «сколько думала модель», и секунды ожидания альбома там были
            # бы прямым враньём — панель промптов показывает это число средним.
            # `_note` объявлен выше, но читает переменную, а не её старое
            # значение, поэтому присваивание здесь до него доходит.
            started = time.monotonic()

        # ── Анализ медиа-триггера (фото / голосовое) ──
        enriched_text = trigger_text
        # Файл держим под рукой: с 2026-08-11 на медиа отвечает та же
        # модель, что его смотрит (ask_group_proactive_media), и ей нужен
        # сам файл, а не пересказ. Пусто — значит скачать не вышло.
        media_b64 = media_mime = media_kind = ""
        # Второй и дальше кадры альбома. Заводится ЗДЕСЬ, а не в ветке фото:
        # ниже список читается безусловно, а ветка могла и не сработать.
        album_images_extra: list[str] = []
        if has_photo and photo_file_id:
            try:
                import base64 as _b64
                # ⚠️ Строка правлена 16.08.2026: раньше писала «активная модель
                # не видит картинки → описание через Gemini». Условие выше
                # активную модель НЕ проверяет — фото описывается ВСЕГДА, какая
                # бы модель ни стояла. Читать лог по этой строке было нельзя.
                logger.info("🤖 Чат %s: триггер — фото → описание через Gemini", chat_id)
                # Кадры альбома, если он был; иначе одна картинка триггера.
                # Потолок стоит уже в копилке (PROACTIVE_ALBUM_MAX_PHOTOS) —
                # второй раз здесь не режем, чтобы два правила не разъехались.
                photo_ids = album_photo_ids or [photo_file_id]
                images_b64: list[str] = []
                total_bytes = 0
                for _fid in photo_ids:
                    # Каждый кадр — со своей страховкой: сорвавшаяся закачка
                    # ОДНОГО кадра не должна отменять разбор всего альбома.
                    # Без этого один сетевой сбой на пятой картинке возвращал
                    # бы нас ровно туда, откуда уходили: модель не видит ничего.
                    try:
                        _file = await bot.get_file(_fid)
                        _bytes = await _file.download_as_bytearray()
                    except Exception as e:
                        logger.warning("🤖 Чат %s: кадр альбома не скачался, идём дальше: %s",
                                       chat_id, e)
                        continue
                    total_bytes += len(_bytes)
                    images_b64.append(_b64.b64encode(_bytes).decode('utf-8'))
                if not images_b64:
                    raise RuntimeError("ни один кадр не скачался")
                # ⚠️ media_kind ОБЯЗАН остаться словом «фото»: по нему
                # ask_group_proactive_media проверяет, умеет ли модель цепочки
                # смотреть картинки (_supports_vision), и выбирает таймаут.
                # Напишешь сюда «альбом» — проверка перестанет срабатывать, и
                # запрос уйдёт модели, которая картинок не видит.
                media_b64, media_mime, media_kind = images_b64[0], "image/jpeg", "фото"
                album_images_extra = images_b64[1:]
                # Разбор для стенограммы ОДИН на весь альбом (а не по разбору
                # на кадр): в стенограмме одно отправление — одна строка, да и
                # платить за шесть отдельных запросов не за что.
                description = await loop.run_in_executor(
                    None, _describe_image, images_b64[0], 0, images_b64[1:],
                )
                if description:
                    # ⚠️ ТЕКСТА РАЗБОРА В ЛОГЕ НЕТ — решение Максима 2026-08-11
                    # («убери из логов полный текст ответа моделей»). Остаётся
                    # только след «сработало» и длина: молчаливый сбой разбора
                    # выглядит как «бот проигнорировал сообщение», это уже
                    # ловили 10.08. Сам разбор не пропадает — он оседает в
                    # стенограмме группы (save_group_message ниже), там его и
                    # смотреть. Не возвращать текст в лог без просьбы Максима.
                    logger.info("🤖 Чат %s: фото проанализировано (%d шт, %d символов)",
                                chat_id, len(images_b64), len(description))
                    # Сам текст разбора — в лог разговора (в общий по-прежнему
                    # не пишем, см. предупреждение выше). В подписи говорим,
                    # сколько кадров ушло: иначе по логу не отличить альбом,
                    # собранный целиком, от альбома, у которого взяли первый кадр.
                    chat_log.note_media(
                        f"альбом из {len(images_b64)} фото" if len(images_b64) > 1 else "фото",
                        total_bytes, description)
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
                # Та же правка, что у фото выше: голосовое расшифровывается
                # всегда, активная модель здесь ни при чём.
                logger.info("🤖 Чат %s: триггер — голосовое → расшифровка через Gemini", chat_id)
                voice_file = await bot.get_file(voice_file_id)
                file_bytes = await voice_file.download_as_bytearray()
                audio_base64 = _b64.b64encode(file_bytes).decode('utf-8')
                media_b64, media_mime, media_kind = audio_base64, "audio/ogg", "голосовое"
                transcription = await loop.run_in_executor(
                    None, _transcribe_audio, audio_base64,
                )
                if transcription:
                    logger.info("🤖 Чат %s: голосовое расшифровано (%d символов)",
                                chat_id, len(transcription))   # текста в логе нет, см. фото выше
                    chat_log.note_media("голосовое", len(file_bytes), transcription)
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
                    None, _describe_video, video_base64, video_mime,
                )
                if description:
                    logger.info("🤖 Чат %s: видео проанализировано (%d символов)",
                                chat_id, len(description))   # текста в логе нет, см. фото выше
                    chat_log.note_media("видео", len(file_bytes), description)
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
            # Единственный выход, который не проходит через _note (в журнал
            # проверок он тоже не пишется — модели тут не было вовсе).
            chat_log.note_outcome("до модели не дошло — триггер пуст после разбора вложения")
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
            # Остальные кадры альбома (если он был) идут тем же запросом —
            # модель смотрит все картинки разом, а не первую из шести.
            extra_media = [(b64, "image/jpeg") for b64 in album_images_extra]
            answer = await loop.run_in_executor(
                None, ask_group_proactive_media, chat_id, bot.id, enriched_text,
                trigger_user_id, media_b64, media_mime, media_kind, extra_media,
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
                                  trigger_text, trigger_message_id, album_message_ids)
            _note("empty")
            return

        # Мут — ДО реплики: между решением и словами человек не должен успеть
        # написать ещё пару сообщений.
        deleted = False
        if mute_sec:
            deleted = await _apply_mute(bot, chat_id, trigger_user_id, mute_sec,
                                        trigger_text, trigger_message_id, album_message_ids)

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
        # Копилку закрываем в любом случае: сорвалась проверка на полпути —
        # брошенные кадры не должны подмешаться в следующую.
        _albums.pop(chat_id, None)
