# ───────────────────────────────────────────────
#  handlers/admin/common.py — общие помощники админ-панелей: гейт «только админ», отправка панельных сообщений, чтение и упаковка логов, формат времени.
#  Выделен из монолитного admin.py 2026-07-13 разрезом БЕЗ изменения логики.
# ───────────────────────────────────────────────
import html
import logging
import os

import logging_setup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import ADMIN_IDS
from utils import register_and_clean_bot_message, delete_user_message_safe


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Единый стиль кнопок-тумблеров (решение Максима 2026-07-20)
# ─────────────────────────────────────────────

def _onoff(is_on) -> str:
    """Метка состояния для КНОПКИ-тумблера: «🟢ВКЛ» / «🔴ВЫКЛ».

    ЕДИНСТВЕННЫЙ источник этих надписей — любой новый тумблер обязан брать
    метку отсюда, иначе кнопки снова разъедутся по стилю.

    Правило, которое эта функция закрепляет: кнопка показывает ТЕКУЩЕЕ
    СОСТОЯНИЕ, а не действие. Надписи вида «Выключить антиспам» (это значило
    «сейчас включён») больше не пишем: зелёный кружок обязан гореть тогда,
    когда настройка РАБОТАЕТ. Осторожно с настройками, которые хранятся
    наоборот: такая осталась ОДНА — admin_no_prompt_<id> держит ЕДИНИЦУ при
    ВЫКЛЮЧЕННОМ промпте, её значение нужно перевернуть ДО передачи сюда.
    (Второй перевёртыш, admin_no_rag_<id>, умер 2026-07-27 вместе с
    персональным тумблером базы знаний.) Новые тумблеры заводить только
    с ПРЯМОЙ семантикой — «1» значит «работает», как у rag_enabled,
    thoughts_enabled и proactive_enabled: на инверсии ошибиться проще всего.

    В ТЕКСТЕ панелей (не на кнопках) по-прежнему пишем полными словами —
    «🟢 ВКЛЮЧЁН» / «🔴 ВЫКЛЮЧЕН».
    """
    return "🟢ВКЛ" if is_on else "🔴ВЫКЛ"


async def _reject_non_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Отказ не-админу в админ-команде: бот НИЧЕГО не отвечает, а сообщение
    с командой удаляется. Удаление работает только в группах, где бот —
    админ; в личке Telegram не даёт боту удалять чужие сообщения, поэтому
    там команда просто молча игнорируется (сообщение пользователя остаётся).
    """
    if update and update.message:
        await delete_user_message_safe(update.message)


async def _require(update: Update, context: ContextTypes.DEFAULT_TYPE, perm: str) -> bool:
    """
    Гейт админ-КОМАНД: пропускает, только если у человека есть право `perm`.
    Возвращает True — можно работать дальше; False — доступ закрыт, команда
    уже удалена, вызывающий обязан просто выйти.

    Код права — из services/roles.PERMS, либо 'owner' (только владелец),
    либо 'any' (любой из персонала — для общей панели /adm).
    Отказ МОЛЧАЛИВЫЙ, как и раньше: посторонний не должен по ответу бота
    понимать, что такая команда вообще существует.
    """
    from services import roles
    user_id = update.effective_user.id if update.effective_user else 0
    allowed = roles.has_any_perm(user_id) if perm == "any" else roles.can(user_id, perm)
    if not allowed:
        await _reject_non_admin(update, context)
        return False
    return True


def _audit(actor_id: int, action: str, target_id: int = 0, details: str = "") -> None:
    """
    Записывает действие персонала в журнал (таблица staff_log, экран
    «📋 Журнал персонала»). Пишутся действия И владельца, и модераторов —
    журнал должен отвечать на вопрос «кто это сделал», без исключений.

    action — короткий код (см. _ACTION_TITLES в panel_users.py), details —
    человекочитаемая подробность («на 30 мин», «порог = 8»).
    Тихая: сбой журнала не должен ломать само действие.
    """
    try:
        from database.history import log_staff_action
        from services import roles
        name = _staff_name(actor_id)
        prefix = "👑" if roles.is_owner(actor_id) else "🛡"
        log_staff_action(actor_id, f"{prefix} {name}", action, target_id, details)
    except Exception as e:
        logger.debug("📋 Не удалось записать действие в журнал персонала: %s", e)


def _staff_name(user_id: int, known: dict | None = None) -> str:
    """
    Имя человека для журнала и уведомлений (сырой текст — экранировать перед
    вставкой в HTML). Ищет там же, где список пользователей: личное дело,
    архив групп, викторина. Личного дела может не быть вовсе (человек писал
    только в личке) — тогда останется id.

    known — заранее собранная карта {id: имя}, чтобы не сканировать таблицы
    на каждую строку журнала (см. _build_staff_log_panel).
    """
    if known is not None:
        return known.get(user_id) or str(user_id)
    try:
        from database.history import list_known_users
        for u in list_known_users(1000):
            if u["user_id"] == user_id:
                return (u.get("first_name")
                        or (f"@{u['username']}" if u.get("username") else "")
                        or u.get("quiz_name") or str(user_id))
    except Exception as e:
        logger.debug("📋 Не удалось определить имя %s: %s", user_id, e)
    return str(user_id)


def _known_names() -> dict:
    """Карта {user_id: имя} одним проходом — для экранов со списками."""
    try:
        from database.history import list_known_users
        return {u["user_id"]: ((u.get("first_name")
                                or (f"@{u['username']}" if u.get("username") else "")
                                or u.get("quiz_name") or str(u["user_id"])))
                for u in list_known_users(1000)}
    except Exception as e:
        logger.debug("📋 Не удалось собрать имена: %s", e)
        return {}


async def _notify_owners(bot, text: str) -> None:
    """
    Шлёт сообщение всем ВЛАДЕЛЬЦАМ бота в личку (уведомления о тяжёлых
    действиях модераторов). Намеренно БЕЗ register_and_clean_bot_message:
    такие сообщения — история, панели не должны их затирать.
    Тихая: недоступность одного владельца не мешает остальным.
    """
    from config import ADMIN_IDS
    for owner_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=owner_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.debug("📋 Не удалось уведомить владельца %s: %s", owner_id, e)


def _is_group_chat(update: Update) -> bool:
    """True, если команда пришла НЕ из лички (группа/супергруппа/канал).

    Используется панельными админ-командами: в группе они молча выходят
    (сообщение с командой уже удалено), а работают только в личке с ботом.
    """
    chat = update.effective_chat
    return bool(chat) and chat.type != "private"


def _adm_back_row():
    """Ряд с кнопкой возврата в главную админ-панель (единый для всех панелей)."""
    return [InlineKeyboardButton("⬅️ Админ-панель", callback_data="adm_back")]


def _filter_keyboard(rows: list, viewer_id: int) -> list:
    """
    Оставляет в клавиатуре только кнопки, которые этот человек ВПРАВЕ нажать
    (services/roles.py::may_press). Единственный механизм видимости кнопок в
    панелях персонала: «видит» и «может» — одно и то же, второго списка нет.
    Хочешь скрыть кнопку от модератора — поменяй ей право в `_CALLBACK_RULES`.

    Ряд, где после отбора остались одни заглушки (`…:noop` — средняя кнопка
    регулятора с живым значением), выбрасывается целиком: без ➖ и ➕ она
    превратилась бы в одинокую полосу с числом, которая никуда не ведёт.

    Кнопки БЕЗ callback_data (ссылки) проходят как есть — прав на них нет.
    """
    from services import roles

    def _is_stub(btn) -> bool:
        return bool(btn.callback_data) and btn.callback_data.endswith("noop")

    result = []
    for row in rows:
        kept = [b for b in row if not b.callback_data or roles.may_press(viewer_id, b.callback_data)]
        if not kept or all(_is_stub(b) for b in kept):
            continue
        result.append(kept)
    return result


async def _send_panel_message(bot, chat_id: int, text: str, markup: InlineKeyboardMarkup):
    """
    Общий помощник отправки панельных сообщений (статистика / промпты / модели):
    длинный текст режется по строкам на части ≤4000 символов (клавиатура — только
    у последней части), при ошибке разметки — запасной путь без HTML. Отправка
    идёт через register_and_clean_bot_message — предыдущее «панельное» сообщение
    бота в этом чате удаляется.
    """
    max_len = 4000
    try:
        if len(text) <= max_len:
            sent_msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            if sent_msg:
                await register_and_clean_bot_message(bot, chat_id, sent_msg.message_id)
        else:
            chunks = []
            current_chunk = ""
            for line in text.split("\n"):
                if len(current_chunk) + len(line) + 1 > max_len:
                    chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk += ("\n" + line) if current_chunk else line
            if current_chunk:
                chunks.append(current_chunk)
            for idx, chunk in enumerate(chunks):
                is_last = (idx == len(chunks) - 1)
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup if is_last else None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                if msg:
                    # keep_count=idx+1: куски ОДНОЙ панели не должны удалять
                    # друг друга (с keep_count=1 второй кусок стирал бы первый);
                    # следующая панель по-прежнему уберёт их все разом.
                    await register_and_clean_bot_message(bot, chat_id, msg.message_id, keep_count=idx + 1)
    except Exception as e:
        logger.warning("⚠️ Не удалось отправить панель с разметкой: %s — отправляю без разметки", e)
        import re as _re
        clean = _re.sub(r'<[^>]+>', '', text)[:max_len]
        sent_msg = await bot.send_message(
            chat_id=chat_id, text=clean, reply_markup=markup
        )
        if sent_msg:
            await register_and_clean_bot_message(bot, chat_id, sent_msg.message_id)


# ─────────────────────────────────────────────
#  Формат времени для журналов (модерация /mod и база знаний /rag)
# ─────────────────────────────────────────────

def _fmt_mod_time(ts: float) -> str:
    """Время действия модерации: 'ЧЧ:ММ' если сегодня, иначе 'ДД.ММ ЧЧ:ММ'."""
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ts)
    if dt.date() == _dt.datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%d.%m %H:%M")


# ─────────────────────────────────────────────
#  Чтение и упаковка логов для кнопки «📜 Логи бота»
#  (роутер: adm_logs / adm_logs_file / adm_logs_archive)
# ─────────────────────────────────────────────

_LOG_TEXT_LIMIT = 4096        # лимит Telegram на обычное текстовое сообщение

# Через сколько секунд присланный файл лога сам исчезает из переписки
# (решение Максима 2026-07-28: логи скачивают и сразу открывают, а в чате
# они копятся мусором). Гигиена панелей файлы не трогает — они уходят мимо
# неё, чтобы не стирать экран логов, — поэтому убирает их этот таймер.
# ⚠️ Удаление живёт в памяти процесса (utils.schedule_delete): перезапуск
# бота в эту минуту оставит файл в чате навсегда. Мелочь, но знать стоит.
_LOG_FILE_TTL = 60

# Потолок размера файла, который Telegram примет от бота, — 50 МБ. Берём с
# запасом: у записи разговора нет никакого ограничителя, кроме кнопки «Очистить
# РАЗГОВОРЫ», и однажды она может дорасти до отказа отправки.
_TG_FILE_MAX = 45 * 1024 * 1024


def _logs_menu_rows():
    """Две ветки экрана «📜 Логи бота» (2026-08-16, просьба Максима).

    Раньше кнопка «Логи бота» сразу присылала текст общего лога. Логов стало
    два вида — работа бота и дословный разговор в группе, — и один экран их
    не вмещает: у разговора свои цифры, свои файлы и свой счёт записей.
    Отсюда развилка, а сами экраны — ниже по роутеру.
    """
    # Обе ветки в ОДИН ряд, два столбца (решение Максима 2026-08-16): выбор из
    # двух равнозначных веток читается как выбор, а не как список.
    return [[
        InlineKeyboardButton("⚙️ Работа бота", callback_data="adm_logs_bot"),
        InlineKeyboardButton("💬 Разговор в группе", callback_data="adm_logs_chat"),
    ]]


def _logs_back_row():
    """Возврат с любого экрана логов на развилку. ОДИН на оба экрана: у каждой
    кнопки своя строка в проверке «кнопки ↔ роутер», и второй такой же ряд
    просто раздувал бы счётчик, ничего не добавляя."""
    return [InlineKeyboardButton("⬅️ К логам", callback_data="adm_logs")]


def _chat_log_files_row():
    """Ряд скачивания записей разговора: текущая и архив прошлых."""
    return [
        # 💾 — как у «Текущего лога» бота: значок скачивания одинаковый у всех
        # кнопок, которые присылают файл (решение Максима 2026-08-16).
        InlineKeyboardButton("💾 Текущая запись", callback_data="adm_logs_chat_file"),
        InlineKeyboardButton("🗄 Архив разговоров", callback_data="adm_logs_chat_archive"),
    ]


def _log_files_row():
    """Ряд из ДВУХ кнопок скачивания логов: текущий запуск и архив прошлых.

    Стоит ТОЛЬКО на экране «📜 Логи бота» и остаётся там после нажатия: сами
    файлы уходят в чат отдельными сообщениями, мимо гигиены панелей и БЕЗ
    клавиатуры (решение Максима 2026-07-28 — иначе получался лишний переход
    и дубль тех же двух кнопок под каждым файлом). Поэтому оба файла берутся
    с одного экрана подряд.
    """
    return [
        InlineKeyboardButton("💾 Текущий лог", callback_data="adm_logs_file"),
        InlineKeyboardButton("🗄 Архив логов", callback_data="adm_logs_archive"),
    ]


def _read_current_log():
    """Читает файл лога текущей сессии. Возвращает (путь, содержимое-байты).

    Если файла нет или он не читается — содержимое будет пустым (b"").
    """
    log_path = logging_setup.CURRENT_LOG_FILE
    raw = b""
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "rb") as f:
                raw = f.read()
        except Exception as e:
            logger.warning("⚠️ Не удалось прочитать файл лога %s: %s", log_path, e)
    return log_path, raw


def _read_archive_log():
    """Читает общий архив логов ПРОШЛЫХ запусков. Возвращает (путь, байты).

    Путь берётся у logging_setup — там он и складывается из папки и имени
    файла; вторую сборку пути заводить нельзя, иначе после переименования
    архива кнопка молча начнёт искать не тот файл.
    """
    log_path = logging_setup._archive_path()
    raw = b""
    if os.path.exists(log_path):
        try:
            with open(log_path, "rb") as f:
                raw = f.read()
        except Exception as e:
            logger.warning("⚠️ Не удалось прочитать архив логов %s: %s", log_path, e)
    return log_path, raw


def _count_archive_sessions(raw: bytes) -> int:
    """Сколько запусков лежит в архиве — считается по строкам-заголовкам,
    которыми logging_setup разделяет сессии («════ СЕССИЯ 27.07 22-44 ════»).

    Цифра честная: показываем реально склеенное, а не потолок
    ARCHIVE_SESSIONS_TO_KEEP (архив бывает и короче — сразу после чистки).
    """
    try:
        text = raw.decode("utf-8", errors="replace")
        return sum(1 for ln in text.splitlines()
                   if ln.startswith(logging_setup._SESSION_MARK_PREFIX))
    except Exception:
        return 0


def _read_file_bytes(path: str | None):
    """Читает файл целиком. Возвращает (путь, байты); нет файла — пустые байты."""
    raw = b""
    if path and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception as e:
            logger.warning("⚠️ Не удалось прочитать файл %s: %s", path, e)
    return path, raw


def _build_logs_menu_text() -> str:
    """
    Развилка «📜 Логи бота»: обе ветки с живыми цифрами (2026-08-16).

    Цифры стоят ДО нажатия намеренно: видно, есть ли вообще что качать, и не
    приходится ходить в обе ветки, чтобы это выяснить.
    """
    from services.backup import human_size
    from services import chat_log

    _, current = _read_current_log()
    _, archive = _read_archive_log()
    sessions = _count_archive_sessions(archive)
    st = chat_log.stats()

    if st["path"]:
        chat_now = (f"Текущая запись: с {st['started']} · "
                    f"проверок {st['checks']} · {human_size(st['size'])}")
    else:
        chat_now = "Текущая запись: пусто — после очистки в группе ещё не писали"
    chat_arc = (f"Архив: {st['archive_sessions']} из {chat_log.SESSIONS_TO_KEEP} "
                f"очисток · {human_size(st['archive_size'])}"
                if st["archive_sessions"] else "Архив: пуст")

    return (
        "📜 <b>ЛОГИ БОТА</b>\n"
        "───────────────────────────\n"
        "⚙️ <b>РАБОТА БОТА</b>\n"
        "<i>Запуски, ошибки, обращения к моделям.</i>\n"
        f"Текущая сессия: {human_size(len(current))}\n"
        f"Архив: {sessions} запусков · {human_size(len(archive))}\n"
        "\n"
        "💬 <b>РАЗГОВОР В ГРУППЕ</b>\n"
        "<i>Что уходит модели в режиме «Сам в разговор» и что она отвечает — "
        "дословно.</i>\n"
        f"{chat_now}\n"
        f"{chat_arc}\n"
        "───────────────────────────\n"
        "<i>Записи разговора считаются по очисткам разговоров, а не по дням "
        "и не по запускам бота.</i>"
    )


def _build_chat_log_header(stats: dict) -> str:
    """Шапка экрана записи разговора — она же уходит в _build_log_text."""
    from services.backup import human_size
    from services import chat_log

    arc = (f" · 🗄 архив: {stats['archive_sessions']} из {chat_log.SESSIONS_TO_KEEP} очисток"
           if stats["archive_sessions"] else "")
    return (
        "💬 <b>РАЗГОВОР В ГРУППЕ</b>\n"
        f"<code>{html.escape(stats['name'])}</code> · {human_size(stats['size'])}\n"
        f"🕐 с {stats['started']} · 🔍 проверок: {stats['checks']}{arc}\n\n"
        "Последние строки:\n"
    )


def _build_log_text(fname: str, raw: bytes, header: str = "") -> str:
    """Текст сообщения с логами: имя, размер и максимум последних строк в <pre>.

    Строки набираются с конца файла, пока сообщение целиком влезает в лимит
    Telegram (4096 символов). Если даже одна последняя строка длиннее лимита,
    от неё оставляется только конец.

    `header` — своя шапка вместо стандартной. Ею пользуется экран лога
    разговора (2026-08-16): текст там устроен так же — последние строки в
    рамке, — а отличается только заголовком, и второй такой набор строк с
    конца заводить незачем.
    """
    size_kb = max(1, round(len(raw) / 1024))
    header = header or (
        f"📜 <b>Логи текущей сессии</b>\n"
        f"<code>{html.escape(fname)}</code> · {size_kb} КБ\n\n"
        f"Последние строки:\n"
    )
    # Бюджет символов на сам хвост лога (с учётом заголовка и тегов <pre>)
    budget = _LOG_TEXT_LIMIT - len(header) - len("<pre></pre>")
    lines = [ln for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    tail, used = [], 0
    for line in reversed(lines):
        esc = html.escape(line)
        need = len(esc) + (1 if tail else 0)  # +1 символ на перевод строки
        if used + need > budget:
            if not tail:
                # Единственная строка сама длиннее лимита — оставляем её конец
                # (режем исходную строку, а экранируем заново, чтобы не порвать
                # HTML-сущности вроде &amp; посередине)
                while line:
                    esc = "…" + html.escape(line)
                    if len(esc) <= budget:
                        tail.append(esc)
                        break
                    line = line[100:]
            break
        tail.append(esc)
        used += need
    tail.reverse()
    return f"{header}<pre>" + "\n".join(tail) + "</pre>"
