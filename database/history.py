# ───────────────────────────────────────────────
#  history.py — хранилище истории диалогов и данных бота (SQLite)
#
#  Таблицы:
#    messages     — сообщения каждого пользователя
#    user_context — метаданные: реальный подсчёт токенов от Gemini API
# ─────────────────────────────────────────────

import os
import time
import json
import sqlite3
import threading
import logging

from config import DB_PATH, MAX_CONTEXT_MESSAGES, QUIZ_RANKS, PROVIDERS

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────
#  Замок базы (2026-07-27)
#
#  Один глобальный замок — защищает все операции с БД от гонки данных.
#  Раньше это был обычный threading.Lock; теперь — обёртка вокруг него,
#  которая ДОПОЛНИТЕЛЬНО откатывает незавершённую транзакцию, если функция
#  выпала с ошибкой на середине записи. Раньше это было не нужно: каждая
#  функция открывала СВОЁ соединение и бросала его при ошибке (транзакция
#  умирала вместе с соединением). Теперь соединение одно на весь процесс —
#  недописанная транзакция висела бы в нём и её содержимое уехало бы в чужой
#  commit следующей функции. Отсюда откат.
#
#  ⚠️ Замок НЕ реентерабельный — как и прежний threading.Lock. Значит,
#  get_setting/set_setting (они берут замок сами) звать только ВНЕ блока
#  `with _lock`, иначе бот повиснет намертво. Правило старое, не изменилось.
# ─────────────────────────────────────────────

class _DbLock:
    """Замок всех операций с БД + откат недописанной транзакции при ошибке."""

    def __init__(self):
        self._lock = threading.Lock()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            try:
                if _conn is not None and _conn.in_transaction:
                    _conn.rollback()
                    logger.debug("Откат незавершённой транзакции после ошибки: %s", exc)
            except Exception:
                pass  # откат — страховка, своих ошибок она порождать не должна
        self._lock.release()
        return False  # исключение наружу, как и раньше


_lock = _DbLock()


# ───────────────────────────────────────────────
#  Подключение и инициализация
# ─────────────────────────────────────────────

def _compact_sql(sql) -> str:
    """SQL в одну строку без переносов и лишних пробелов — для компактного лога."""
    return " ".join(str(sql).split())


def _short(text, limit: int = 500) -> str:
    """Обрезает длинные значения (напр. текст сообщения) в логе SQL."""
    text = str(text)
    return text if len(text) <= limit else f"{text[:limit]}...(+{len(text) - limit})"


class _LoggingConnection(sqlite3.Connection):
    """
    Подключение к SQLite, логирующее КАЖДЫЙ запрос: текст SQL, подставленные
    данные и длительность. Это «рентген БД» — одна точка, через которую видно
    все чтения и записи без правок в самих функциях (database/history.py).
    """

    def execute(self, sql, parameters=()):
        start = time.perf_counter()
        try:
            return super().execute(sql, parameters)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            logger.debug("SQL (%.1f ms) | %s | params=%s",
                         elapsed, _compact_sql(sql), _short(repr(parameters)))

    def executemany(self, sql, seq_of_parameters):
        start = time.perf_counter()
        try:
            return super().executemany(sql, seq_of_parameters)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            logger.debug("SQL many (%.1f ms) | %s", elapsed, _compact_sql(sql))

    def executescript(self, sql_script):
        start = time.perf_counter()
        try:
            return super().executescript(sql_script)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            logger.debug("SQL script (%.1f ms) | %d символов", elapsed, len(sql_script))

    def close(self):
        """
        Закрытие ИГНОРИРУЕТСЯ намеренно (2026-07-27).

        Соединение теперь ОДНО на весь процесс (см. _get_connection), и любой
        случайный `conn.close()` в новой функции убил бы базу для всего бота:
        каждый следующий запрос падал бы с «Cannot operate on a closed
        database». Все прежние вызовы close() из функций убраны, а этот заслон
        оставлен на случай, если кто-то напишет его по привычке.

        Настоящее закрытие — close_db() (зовётся один раз при остановке бота).
        """
        logger.debug("Вызов conn.close() проигнорирован: соединение общее на весь процесс")


# ───────────────────────────────────────────────
#  ЕДИНОЕ СОЕДИНЕНИЕ С БАЗОЙ (2026-07-27, ускорение)
#
#  Раньше каждая из 74 функций открывала своё соединение на один запрос и
#  закрывала его. Открывание файла базы стоит в СОТНИ раз дороже самого
#  запроса — замерено на рабочей базе:
#      чтение настройки   2.0 мс → 0.006 мс
#      запись в архив     6.8 мс → 0.86 мс
#  На каждое сообщение группы бот делал 3 записи и до 7 чтений: ~34 мс, и всё
#  это время не обрабатывал ничего другого. Теперь ~2.7 мс.
#
#  Безопасно потому, что ВСЕ 74 обращения идут внутри `with _lock` (проверено
#  сплошным просмотром файла) — то есть одновременных запросов к соединению
#  не бывает, хотя зовут его и из рабочих потоков (check_same_thread=False).
# ─────────────────────────────────────────────

_conn: sqlite3.Connection | None = None


def _get_connection() -> sqlite3.Connection:
    """
    Общее соединение с базой. Открывается при первом обращении, дальше
    возвращается готовое. Звать ТОЛЬКО внутри `with _lock`.
    """
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, factory=_LoggingConnection)
        _conn.row_factory = sqlite3.Row
        # WAL — быстрая запись и бесконфликтное чтение (режим запоминается
        # в самом файле базы, но ставим явно: на новой базе его ещё нет).
        _conn.execute("PRAGMA journal_mode=WAL")
        # synchronous=NORMAL (решение Максима 2026-07-27): запись 0.885 → 0.048 мс.
        # Штатная пара к WAL по рекомендации самой SQLite. Падение бота записи
        # НЕ теряет; потерять последние доли секунды можно только при сбое
        # Windows или обесточивании компьютера — для архива чата это принято.
        _conn.execute("PRAGMA synchronous=NORMAL")
        logger.info("База открыта: %s (WAL, synchronous=NORMAL)", DB_PATH)
    return _conn


def close_db() -> None:
    """
    Закрывает общее соединение — один раз, при остановке бота (main.py).
    Заодно сворачивает WAL-файл в саму базу. «Тихая»: сбой закрытия не должен
    мешать боту выключаться.
    """
    global _conn
    with _lock:
        if _conn is None:
            return
        try:
            sqlite3.Connection.close(_conn)   # свой close() у _LoggingConnection пустой
            logger.info("База закрыта")
        except Exception as e:
            logger.debug("Не удалось закрыть базу: %s", e)
        finally:
            _conn = None


def backup_to(path: str) -> int:
    """
    Делает копию базы в файл `path` средствами самой SQLite и возвращает
    размер получившейся копии в байтах. Зовётся из services/backup.py.

    ⚠️ ПОЧЕМУ НЕ ПРОСТЫМ КОПИРОВАНИЕМ ФАЙЛА. База работает в режиме WAL:
    часть свежих записей лежит НЕ в history.db, а в соседнем history.db-wal,
    и попадает в саму базу позже. Скопируешь один файл обычным copy — получишь
    базу без последних изменений, а если копирование попадёт на момент записи,
    то и вовсе битую. Штатный `Connection.backup` берёт согласованный снимок
    вместе с WAL — при РАБОТАЮЩЕМ боте это единственный правильный способ.

    Берёт общий замок, как все операции с базой: на нашем объёме снимок
    занимает миллисекунды, и на это время никто в базу не пишет.

    Ошибки НЕ глушит: копия, о которой доложили «готово», а её нет, хуже
    отсутствия копий — разбираться будут в тот день, когда она понадобится.
    """
    with _lock:
        conn = _get_connection()
        dest = sqlite3.connect(path)
        try:
            conn.backup(dest)
        finally:
            dest.close()   # обычное соединение, не наше — close() у него настоящий
    size = os.path.getsize(path)
    logger.info("💾 Снимок базы сделан: %s (%d байт)", path, size)
    return size


def _create_schema(conn):
    """
    СХЕМА: создание таблиц и индексов. Выполняется на каждом запуске, но
    делает что-то только для НОВОЙ базы — все CREATE идут с IF NOT EXISTS.

    ⚠️ Зовётся ТОЛЬКО из init_db, изнутри `with _lock`, и соединение получает
    параметром: своего `_get_connection()` здесь быть не должно — соединение
    одно на процесс, и брать его вне замка нельзя.
    ⚠️ Новая таблица — не забыть про `reset_db.py::USER_TABLES`.
    ⚠️ Новая ГРАФА в существующей таблице сюда добавляется, но этого мало:
    у работающего бота таблица уже создана, и CREATE её не тронет. Графу
    обязательно продублировать в `_COLUMN_MIGRATIONS` ниже.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            role       TEXT    NOT NULL,
            content    TEXT    NOT NULL,
            model_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_context (
            chat_id           INTEGER NOT NULL,
            user_id           INTEGER NOT NULL,
            last_prompt_tokens INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS sent_news (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            url        TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS news_subscriptions (
            chat_id    INTEGER PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS quiz_stats (
            user_id           INTEGER PRIMARY KEY,
            username          TEXT,
            correct_answers   INTEGER DEFAULT 0,
            total_attempts    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS bot_sent_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_calls (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            called_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Снимки счётчиков панели «📡 Настройки API» для суточного отчёта
        -- (services/daily_report.py, рассылка в полночь по Киеву).
        -- Счётчики расходов в settings — накопительные копилки, суточной
        -- цифры в них нет: отчёт показывает РАЗНИЦУ между соседними снимками.
        --   taken_at_utc — момент снимка в UTC «ГГГГ-ММ-ДД ЧЧ:ММ:СС»
        --                  (тот же формат, что called_at — по нему считаются
        --                  вызовы за период);
        --   kyiv_label   — тот же момент по Киеву «ГГГГ-ММ-ДД ЧЧ:ММ» (подпись);
        --   data         — JSON со значениями копилок и остатков квоты Qwen.
        CREATE TABLE IF NOT EXISTS stats_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            taken_at_utc TEXT NOT NULL,
            kyiv_label   TEXT NOT NULL,
            data         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_image_calls (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            called_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Журнал проактивных проверок (2026-07-31): по строке на КАЖДЫЙ
        -- запрос к модели в режиме «Сам в разговор» — вступил бот или
        -- промолчал. Нужен, чтобы порог настраивался по цифрам, а не по
        -- ощущению «тараторит / молчит»: одно число «сколько раз написал»
        -- не отличает «проверок мало» от «модель не умеет молчать»,
        -- а лечатся эти две беды противоположно (порогом и промптом).
        -- ⚠️ Пишутся ТОЛЬКО дошедшие до модели проверки — их единицы
        -- в минуту. Отсев дешёвыми фильтрами живёт в памяти
        -- (services/proactive.py): он идёт на КАЖДОЕ сообщение группы,
        -- и поход в базу на этом пути недопустим.
        CREATE TABLE IF NOT EXISTS proactive_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            chat_id      INTEGER,
            outcome      TEXT,      -- reply | reply_mute | silent | empty | error
            model        TEXT,      -- активная модель на момент проверки
            seconds      REAL,      -- сколько модель думала
            reply_len    INTEGER,   -- длина видимой реплики (0 у молчания)
            trigger_kind TEXT       -- text | photo | voice | video
        );

        -- 👋 Журнал вступлений в группы (2026-08-04): по строке на каждого
        -- новичка и на исход его проверки «я не бот» (services/greeter.py).
        -- Отдельная таблица, а НЕ moderation_log: вступления идут потоком и
        -- вытеснили бы муты со «Последних действий» панели /mod — журнал
        -- модерации показывает всего пять строк. Считается по этой таблице
        -- строка «За 7 дней: пришло … прошли …» в блоке приветствия.
        -- ⚠️ Кик не прошедшего пишется И СЮДА (outcome='kick'), И в
        -- moderation_log: там он законная запись — человека выгнали из группы.
        CREATE TABLE IF NOT EXISTS join_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL,       -- time.time(), как в moderation_log
            chat_id    INTEGER,
            user_id    INTEGER,
            name       TEXT,
            outcome    TEXT        -- join | ok | timeout | kick
        );

        CREATE TABLE IF NOT EXISTS group_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            username   TEXT,
            first_name TEXT,
            text       TEXT,
            has_photo  INTEGER DEFAULT 0,
            has_voice  INTEGER DEFAULT 0,
            has_video  INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Учёт токенов и запросов на ОДНОГО пользователя (объединённое окно)
        CREATE TABLE IF NOT EXISTS user_token_usage (
            user_id        INTEGER PRIMARY KEY,
            total_tokens   INTEGER DEFAULT 0,
            total_requests INTEGER DEFAULT 0
        );

        -- Журнал действий модерации (мут/размут) — статистика /mod за N дней.
        -- ts — unix-время (локальное, time.time()), как и счётчики антиспама.
        -- admin_name — кто выполнил размут (кнопка/команда); у автоматики пусто.
        CREATE TABLE IF NOT EXISTS moderation_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL    NOT NULL,
            action     TEXT    NOT NULL,
            chat_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            name       TEXT,
            admin_name TEXT
        );

        -- Тексты удалённых сообщений, из-за которых выдан мут (улики).
        -- Привязаны к строке moderation_log (log_id). Медиа НЕ хранятся —
        -- только пометка has_photo (см. контракт антиспама).
        CREATE TABLE IF NOT EXISTS mute_evidence (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id    INTEGER NOT NULL,
            ord       INTEGER DEFAULT 0,
            text      TEXT,
            has_photo INTEGER DEFAULT 0
        );

        -- Журнал действий с базой знаний RAG (панель /rag): одобрения,
        -- удаления, замены, добавления, пересборки. Хранится KB_LOG_DAYS
        -- дней (чистит cleanup_loop), показывается в блоке «Последние
        -- действия» панели /rag.
        CREATE TABLE IF NOT EXISTS knowledge_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      REAL    NOT NULL,
            action  TEXT    NOT NULL,
            article TEXT,
            user_id INTEGER
        );

        -- Личное дело участника групп: ВЕЧНАЯ запись (не чистится ни
        -- cleanup_loop, ни месячным сбросом). Стаж (first_seen — unix-время
        -- первого сообщения), счётчики сообщений/мутов/удалённых ссылок.
        -- Основа статуса «проверенный» (services/antispam.py::trust_info;
        -- статус информационный, бонус к порогу антифлуда удалён
        -- 2026-07-20) и блока «Служба» в /rank.
        CREATE TABLE IF NOT EXISTS user_dossier (
            user_id      INTEGER PRIMARY KEY,
            first_seen   REAL    NOT NULL,
            msg_count    INTEGER DEFAULT 0,
            mute_count   INTEGER DEFAULT 0,
            last_mute_ts REAL,
            link_count   INTEGER DEFAULT 0
        );

        -- Персональные настройки участника (карточка пользователя в
        -- админ-панели, /users). ПУСТОЕ значение (NULL) = «работает общая
        -- настройка бота»: пороги антифлуда из /mod, лимит картинок из
        -- config.IMAGE_DAILY_LIMIT, звание из викторины. Так карточка
        -- всегда честно показывает, где «своё», а где «общее».
        -- Читаются НЕ отсюда, а из кэша в памяти (services/user_settings.py):
        -- эти настройки нужны на каждое сообщение группы, поход в БД на
        -- горячем пути недопустим.
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id             INTEGER PRIMARY KEY,
            antispam_msg_count  INTEGER,
            antispam_window_sec INTEGER,
            antispam_mute_sec   INTEGER,
            antispam_immune     INTEGER DEFAULT 0,
            links_allowed       INTEGER DEFAULT 0,
            ai_ignored          INTEGER DEFAULT 0,
            image_limit         INTEGER,
            honorary_rank       TEXT,
            trusted_manual      INTEGER,
            note                TEXT,
            updated_at          REAL
        );

        -- Модераторы бота и их права (роли — services/roles.py).
        -- ВЛАДЕЛЬЦЕВ здесь НЕТ: они заданы в config.ADMIN_IDS и из панели
        -- не меняются. Каждая графа p_* — отдельная галочка права:
        --   p_mod        мут / размут / кик, журнал и улики модерации
        --   p_ban        бан и разбан
        --   p_cards      смотреть карточки пользователей
        --   p_cards_edit менять персональные настройки в карточках
        --   p_antispam   общие настройки антиспама (действуют на все группы)
        -- Читаются НЕ отсюда, а из кэша в памяти (services/roles.py):
        -- право проверяется на КАЖДОЕ нажатие кнопки.
        CREATE TABLE IF NOT EXISTS staff (
            user_id      INTEGER PRIMARY KEY,
            p_mod        INTEGER DEFAULT 0,
            p_ban        INTEGER DEFAULT 0,
            p_cards      INTEGER DEFAULT 0,
            p_cards_edit INTEGER DEFAULT 0,
            p_antispam   INTEGER DEFAULT 0,
            granted_by   INTEGER,
            granted_at   REAL
        );

        -- Журнал действий ПЕРСОНАЛА (владельца и модераторов): кто, что,
        -- над кем и когда. Видит его только владелец (кнопка «📋 Журнал
        -- персонала» в разделе «👥 Пользователи»). В отличие от
        -- moderation_log, который пишет и автоматика, сюда попадают ТОЛЬКО
        -- осознанные действия человека — включая смену настроек и выдачу
        -- прав, а не только муты. Хранится STAFF_LOG_DAYS дней (чистит
        -- cleanup_loop). ts — unix-время, как в moderation_log.
        CREATE TABLE IF NOT EXISTS staff_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL    NOT NULL,
            actor_id   INTEGER NOT NULL,
            actor_name TEXT,
            action     TEXT    NOT NULL,
            target_id  INTEGER,
            details    TEXT
        );

        -- Группы, где бот работает: нужны карточке пользователя, чтобы
        -- знать, ГДЕ выдавать мут/кик/бан и где человек состоит. Пополняется
        -- на каждое сообщение группы (collect_group_message), название
        -- обновляется вместе с ним — переименование группы подхватится само.
        CREATE TABLE IF NOT EXISTS known_chats (
            chat_id   INTEGER PRIMARY KEY,
            title     TEXT,
            last_seen REAL
        );

        -- Индекс для быстрой выборки/обрезки контекста по пользователю
        CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);
        CREATE INDEX IF NOT EXISTS idx_modlog_ts ON moderation_log(ts);
        CREATE INDEX IF NOT EXISTS idx_evidence_log ON mute_evidence(log_id);
        CREATE INDEX IF NOT EXISTS idx_kblog_ts ON knowledge_log(ts);
        CREATE INDEX IF NOT EXISTS idx_stafflog_ts ON staff_log(ts);
        CREATE INDEX IF NOT EXISTS idx_proactive_log_ts ON proactive_log(ts);
        CREATE INDEX IF NOT EXISTS idx_join_log_ts ON join_log(ts);
        -- Индекс стенограммы чата для проактивного участия в разговоре
        -- (get_recent_group_messages — выборка последних сообщений чата)
        CREATE INDEX IF NOT EXISTS idx_group_messages_chat ON group_messages(chat_id, id);
    """)


# Миграции граф: ALTER TABLE для баз, СОЗДАННЫХ РАНЬШЕ появления графы.
# CREATE TABLE выше выполняется только для новой базы — у работающего бота
# таблица уже есть, и новая графа из него не появится.
#
# ⚠️ «Колонка уже есть» — это НЕ поломка: SQLite отвечает OperationalError,
# мы её глушим, и поэтому весь список прогоняется на КАЖДОМ запуске.
# Порядок значения не имеет, новые строки дописывать в конец.
_COLUMN_MIGRATIONS = (
    # Какой моделью получен ответ.
    "ALTER TABLE messages ADD COLUMN model_name TEXT",
    # Кто снял мут (кнопка или /unmute); у автоматики остаётся пустым.
    "ALTER TABLE moderation_log ADD COLUMN admin_name TEXT",
    # Накопительный учёт токенов и запросов на пару (chat_id, user_id).
    "ALTER TABLE user_context ADD COLUMN total_tokens INTEGER DEFAULT 0",
    "ALTER TABLE user_context ADD COLUMN total_requests INTEGER DEFAULT 0",
    # Имя и ник в личном деле — для списка людей в админ-панели. Раньше дело
    # хранило только числовой ID, а имена лежали в архиве групп, который
    # чистится раз в 10 дней: молчащий участник превращался в безымянное число.
    "ALTER TABLE user_dossier ADD COLUMN username TEXT",
    "ALTER TABLE user_dossier ADD COLUMN first_name TEXT",
    # Голосовые (2026-07-21) и видео (2026-07-24) в архиве групп.
    "ALTER TABLE group_messages ADD COLUMN has_voice INTEGER DEFAULT 0",
    "ALTER TABLE group_messages ADD COLUMN has_video INTEGER DEFAULT 0",
)


def _run_column_migrations(conn):
    """
    Догоняет схему существующей базы: добавляет графы, появившиеся позже её
    создания. Зовётся только из init_db, изнутри `with _lock`.
    """
    for ddl in _COLUMN_MIGRATIONS:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass          # колонка уже есть — штатный случай, не ошибка
    conn.commit()


def _seed_once(conn):
    """
    РАЗОВЫЕ ЗАСЕВЫ: наполняют новую таблицу из уже накопленных данных, чтобы
    она не стояла пустой до первого события. Каждый срабатывает ОДИН раз —
    по условию «таблица пуста»; наполненную таблицу не трогают никогда.
    Зовётся только из init_db, изнутри `with _lock`.
    """
    # Список групп из архива сообщений: до первого сообщения после запуска
    # known_chats была бы пуста, и карточка пользователя не знала бы, где
    # выдавать мут. Названия групп подтянутся сами, как только в них напишут.
    try:
        if conn.execute("SELECT COUNT(*) FROM known_chats").fetchone()[0] == 0:
            conn.execute(
                "INSERT OR IGNORE INTO known_chats (chat_id, title, last_seen) "
                "SELECT DISTINCT chat_id, NULL, NULL FROM group_messages WHERE chat_id < 0"
            )
            conn.commit()
    except sqlite3.OperationalError:
        pass

    # Перенос статистики запросов из устаревшей user_context (сводим по
    # user_id). Токены раньше не писались (всегда были 0), поэтому
    # total_tokens стартует с нуля и дальше считается верно.
    # ⚠️ Условие «таблица пуста» — ещё и защита от `clear_user_token_usage`:
    # он обнуляет графы UPDATE-ом, а НЕ удаляет строки, именно чтобы этот
    # перенос не залил старые числа обратно после месячного сброса.
    try:
        if conn.execute("SELECT COUNT(*) FROM user_token_usage").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO user_token_usage (user_id, total_tokens, total_requests) "
                "SELECT user_id, COALESCE(SUM(total_tokens), 0), COALESCE(SUM(total_requests), 0) "
                "FROM user_context GROUP BY user_id"
            )
            conn.commit()
    except sqlite3.OperationalError:
        pass


def init_db():
    """
    Готовит базу к работе. Вызывается один раз из main.py.

    Разложена на три шага (2026-08-03; до этого была одной функцией на 345
    строк, где схема, миграции и засевы шли вперемешку):
      1. `_create_schema`         — таблицы и индексы (только для новой базы);
      2. `_run_column_migrations` — графы, появившиеся позже создания базы;
      3. `_seed_once`             — разовое наполнение новых таблиц.
    Порядок обязателен: миграция и засев работают по таблицам, которые
    создаёт первый шаг.
    """
    with _lock:
        conn = _get_connection()   # WAL и synchronous=NORMAL ставятся там же
        _create_schema(conn)
        _run_column_migrations(conn)
        _seed_once(conn)
        conn.commit()
    logger.info("🚀 База данных готова")


# ───────────────────────────────────────────────
#  Чтение истории
# ───────────────────────────────────────────────

def get_history(user_id: int, limit: int | None = MAX_CONTEXT_MESSAGES) -> list[dict]:
    """
    Возвращает объединённое контекстное окно пользователя (личка + все группы)
    в порядке от старых к новым.

    Контекст привязан к user_id, а не к конкретному чату, поэтому диалоги
    одного человека в личных сообщениях и в группах образуют единое окно.

    :param limit: вернуть только последние `limit` сообщений (скользящее окно).
                  По умолчанию MAX_CONTEXT_MESSAGES. None — вернуть всё.
    """
    with _lock:
        conn = _get_connection()
        if limit is not None:
            # Берём последние `limit` строк, затем разворачиваем в хронологию
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def get_history_length(user_id: int) -> int:
    """Возвращает размер объединённого контекстного окна пользователя."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return row["cnt"] if row else 0


def get_user_usage(user_id: int) -> dict:
    """
    Возвращает накопленный расход на пользователя (по всем чатам: личка + группы):
      - total_tokens:   суммарно токенов (вход+выход) за всё время
      - total_requests: сколько запросов сделано
    """
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT total_tokens, total_requests FROM user_token_usage WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"total_tokens": 0, "total_requests": 0}
    return {
        "total_tokens": row["total_tokens"] or 0,
        "total_requests": row["total_requests"] or 0,
    }


# ───────────────────────────────────────────────
#  Запись и обновление
# ───────────────────────────────────────────────

def add_messages(
    chat_id: int,
    user_id: int,
    user_text: str,
    answer: str,
    prompt_tokens: int = 0,
    model_name: str = None,
    total_tokens: int = 0,
):
    """
    Сохраняет пару (вопрос пользователя + ответ бота) и поддерживает
    единое скользящее окно контекста.

    Контекст объединён по user_id: после вставки в окне остаются только
    последние MAX_CONTEXT_MESSAGES сообщений этого пользователя (по всем чатам —
    личка и группы), всё что старше — физически удаляется. Реальный chat_id
    сохраняется в строках для архива и логов, но на размер окна не влияет.

    Накопленный расход токенов и число запросов учитываются на user_id.
    Вызывается только после успешного ответа модели.

    :param prompt_tokens: токены входа последнего запроса (для логов; на окно не влияет)
    :param total_tokens:  вход+выход за этот запрос — прибавляется к накопленной сумме
    """
    if model_name is None:
        try:
            from config import GEMINI_MODEL
            model_name = get_setting("active_model", GEMINI_MODEL)
        except Exception:
            model_name = "unknown"

    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, model_name) VALUES (?,?,?,?,?)",
            (chat_id, user_id, "user", user_text, None),
        )
        conn.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, model_name) VALUES (?,?,?,?,?)",
            (chat_id, user_id, "assistant", answer, model_name),
        )

        # Скользящее окно: оставляем только последние MAX_CONTEXT_MESSAGES строк
        # этого пользователя (по всем чатам), остальное удаляем.
        conn.execute(
            "DELETE FROM messages WHERE user_id=? AND id NOT IN ("
            "SELECT id FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_CONTEXT_MESSAGES),
        )

        # Накопительный учёт токенов и запросов на пользователя
        conn.execute(
            """INSERT INTO user_token_usage (user_id, total_tokens, total_requests)
               VALUES (?, ?, 1)
               ON CONFLICT(user_id)
               DO UPDATE SET
                   total_tokens   = total_tokens + excluded.total_tokens,
                   total_requests = total_requests + 1""",
            (user_id, total_tokens),
        )
        conn.commit()


def clear_history(user_id: int):
    """Полностью удаляет объединённое контекстное окно пользователя (команда /clear).

    Удаляет все сообщения этого user_id из всех чатов (личка + группы).
    Накопленную статистику токенов НЕ обнуляет.
    """
    with _lock:
        conn = _get_connection()
        conn.execute(
            "DELETE FROM messages WHERE user_id=?",
            (user_id,),
        )
        conn.commit()


# ───────────────────────────────────────────────
#  Подписки на новости и отправленные публикации
# ─────────────────────────────────────────────

def subscribe_chat(chat_id: int) -> bool:
    """Подписывает чат на новости. Возвращает True если подписка оформлена впервые."""
    with _lock:
        conn = _get_connection()
        try:
            conn.execute("INSERT INTO news_subscriptions (chat_id) VALUES (?)", (chat_id,))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Чат уже подписан (chat_id — первичный ключ). Транзакцию откатываем:
            # соединение общее, недописанная транзакция висела бы в нём.
            conn.rollback()
            return False


def unsubscribe_chat(chat_id: int) -> bool:
    """Отписывает чат. Возвращает True если подписка была активна."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM news_subscriptions WHERE chat_id=?", (chat_id,))
        rows = cur.rowcount
        conn.commit()
    return rows > 0


def get_subscribed_chats() -> list[int]:
    """Возвращает список ID всех подписанных чатов."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute("SELECT chat_id FROM news_subscriptions").fetchall()
    return [r["chat_id"] for r in rows]


def is_news_already_sent(url: str) -> bool:
    """Проверяет, была ли новость уже отправлена."""
    with _lock:
        conn = _get_connection()
        row = conn.execute("SELECT 1 FROM sent_news WHERE url=?", (url,)).fetchone()
    return row is not None


def mark_news_as_sent(url: str):
    """Помечает новость как отправленную."""
    with _lock:
        conn = _get_connection()
        conn.execute("INSERT OR IGNORE INTO sent_news (url) VALUES (?)", (url,))
        conn.commit()


def add_quiz_attempt(user_id: int, username: str, is_correct: bool):
    """Обновляет статистику ответов пользователя в викторине."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO quiz_stats (user_id, username, correct_answers, total_attempts)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(user_id)
               DO UPDATE SET 
                   username = excluded.username,
                   correct_answers = correct_answers + ?,
                   total_attempts = total_attempts + 1""",
            (user_id, username, 1 if is_correct else 0, 1 if is_correct else 0),
        )
        conn.commit()


def get_user_stats(user_id: int) -> dict:
    """Возвращает статистику пользователя и детали его текущего звания."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT username, correct_answers, total_attempts FROM quiz_stats WHERE user_id=?",
            (user_id,),
        ).fetchone()
    
    if not row:
        # Новичок без единой попытки: первое звание из общего списка
        # (config.QUIZ_RANKS) — не дублируем его текст здесь, иначе при правке
        # списка новичок и все остальные получат РАЗНЫЕ звания.
        first = QUIZ_RANKS[0]
        return {
            "username": "Боец",
            "correct_answers": 0,
            "total_attempts": 0,
            "success_rate": 0.0,
            "rank": first["name"],
            "rank_icon": first["icon"],
            "rank_desc": first["desc"],
            "next_rank_needed": QUIZ_RANKS[1]["min"] if len(QUIZ_RANKS) > 1 else -1,
            "rank_min": first["min"],
        }
    
    correct = row["correct_answers"]
    attempts = row["total_attempts"]
    rate = (correct / attempts * 100.0) if attempts > 0 else 0.0

    # Расчет звания. Список званий — ЕДИНЫЙ источник в config.QUIZ_RANKS
    # (его же читают кнопки выбора почётного звания в карточке /users);
    # раньше он был зашит прямо здесь.
    ranks = QUIZ_RANKS

    current_rank = ranks[0]
    next_rank_needed = ranks[1]["min"] if len(ranks) > 1 else -1
    for idx, r in enumerate(ranks):
        if r["min"] <= correct <= r["max"]:
            current_rank = r
            if idx + 1 < len(ranks):
                next_rank_needed = ranks[idx + 1]["min"]
            else:
                next_rank_needed = -1 # Максимальное звание
            break
            
    return {
        "username": row["username"] or "Боец",
        "correct_answers": correct,
        "total_attempts": attempts,
        "success_rate": round(rate, 1),
        "rank": current_rank["name"],
        "rank_icon": current_rank["icon"],
        "rank_desc": current_rank["desc"],
        "next_rank_needed": next_rank_needed,
        "rank_min": current_rank["min"]
    }


def register_bot_message(chat_id: int, message_id: int):
    """Регистрирует отправленное ботом сообщение в БД."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO bot_sent_messages (chat_id, message_id) VALUES (?, ?)",
            (chat_id, message_id),
        )
        conn.commit()


def get_old_bot_messages(chat_id: int, keep_count: int = 3) -> list[int]:
    """Возвращает список ID сообщений бота, которые нужно удалить."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT message_id FROM bot_sent_messages "
            "WHERE chat_id=? ORDER BY id DESC",
            (chat_id,),
        ).fetchall()
    
    msg_ids = [r["message_id"] for r in rows]
    if len(msg_ids) <= keep_count:
        return []
    return msg_ids[keep_count:]


def remove_bot_message(chat_id: int, message_id: int):
    """Удаляет запись об отправленном сообщении из БД."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "DELETE FROM bot_sent_messages WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    """Возвращает текстовое значение настройки из БД."""
    with _lock:
        conn = _get_connection()
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    val = row["value"] if row else default
    if key == "active_model":
        from config import AVAILABLE_MODELS
        if val not in AVAILABLE_MODELS:
            # Если выбранной модели больше нет в списке доступных, сбрасываем на дефолтную
            set_setting("active_model", default)
            return default
    if key == "active_image_model":
        from config import AVAILABLE_IMAGE_MODELS
        # Старый выбор Imagen (отключён 17.08.2026) больше не в списке — сбрасываем
        # на дефолт (Nano Banana), иначе бот слал бы запросы на мёртвую модель.
        if val not in AVAILABLE_IMAGE_MODELS:
            set_setting("active_image_model", default)
            return default
    return val


def set_setting(key: str, value: str):
    """Сохраняет текстовое значение настройки в БД."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def delete_setting(key: str) -> None:
    """Удаляет настройку ЦЕЛИКОМ (строку из settings), а не обнуляет её.

    Нужна там, где «значение не задано» и «значение равно нулю» — РАЗНЫЕ вещи:
    остаток счёта и остаток квоты Qwen. Пока ключа нет, вычитающие UPDATE
    в add_*_cost и spend_qwen_tokens молча не находят строку и ничего не портят;
    если же вместо удаления записать пустоту или ноль, CAST('' AS REAL) даст 0
    и остаток начнёт уходить в минус с первого же запроса.
    Зовётся из кнопки «убрать значение» на экране «💰 Счета и квоты».
    """
    with _lock:
        conn = _get_connection()
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
        conn.commit()


def add_provider_cost(provider: str, delta_usd: float):
    """
    Прибавляет стоимость одного запроса к копилке расхода провайдера и на ту же
    сумму уменьшает остаток на его счету. ОДНА функция на всех (2026-08-03):
    раньше их было пять — add_deepseek_cost / add_xiaomi_cost /
    add_openrouter_cost / add_qwen_cost / add_image_cost, — и различались они
    только именами ключей.

    Имена ключей берутся из реестра `config.PROVIDERS`: `cost_key` (копилка,
    есть у всех, кто тратит деньги) и `balance_key` (остаток, есть не у всех —
    у Qwen вместо денег бесплатная квота в токенах, см. spend_qwen_tokens).
    Провайдер без копилки — молча ничего не делаем: у Gemini бесплатный ключ,
    и считать там нечего.

    Оба действия выполняются прямо в SQL — атомарно, без гонки
    «прочитал-прибавил-записал» между worker-потоками; settings хранит строки,
    CAST приводит к числу. Остаток не заведён — уменьшать нечего, UPDATE молча
    не найдёт строку, а копилка расхода при этом работает как обычно.
    ⚠️ На бесплатных вариантах моделей сюда приходят НУЛИ, и это правильно:
    копилка заведётся на нуле и будет видна в панели — значит, счёт ведётся.
    """
    meta = PROVIDERS.get(provider) or {}
    cost_key = meta.get("cost_key")
    if not cost_key:
        return
    balance_key = meta.get("balance_key")
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS REAL) + CAST(excluded.value AS REAL)",
            (cost_key, str(delta_usd)),
        )
        if balance_key:
            conn.execute(
                "UPDATE settings SET value = CAST(value AS REAL) - CAST(? AS REAL) "
                "WHERE key = ?",
                (str(delta_usd), balance_key),
            )
        conn.commit()


def spend_qwen_tokens(model_name: str, tokens: int):
    """Вычитает израсходованные токены из ОСТАТКА бесплатной квоты Alibaba
    по КАЖДОЙ модели Qwen отдельно (ключ settings qwen_tokens_<имя модели>) —
    в панели «📡 Настройки API» это число «осталось».
    Устроено как остаток баланса DeepSeek: квота заводится в settings РУКАМИ
    (при смене ключа или рабочего пространства), дальше тает сама. Ключа
    в settings нет — вычитать нечего, UPDATE молча не найдёт строку.
    Остаток ВЕЧНЫЙ: месячный сброс (_monthly_stats_reset) его не трогает —
    квота Alibaba даётся не помесячно, а объёмом токенов на модель.
    Число берётся из usage.total_tokens — это «всего» из строки лога
    (вход + ответ + размышления), сверено с консолью Model Studio 2026-07-05.
    Уходит в МИНУС при исчерпанной квоте — честный сигнал, что расход пошёл
    за деньги; не «чинить» ограничением снизу.
    Вычитание атомарно в SQL — без гонки между потоками."""
    if not model_name or not tokens or tokens <= 0:
        return
    with _lock:
        conn = _get_connection()
        conn.execute(
            "UPDATE settings SET value = CAST(value AS INTEGER) - ? WHERE key = ?",
            (int(tokens), f"qwen_tokens_{model_name}"),
        )
        conn.commit()


def get_qwen_tokens() -> dict:
    """ОСТАТОК бесплатной квоты по каждой модели Qwen: {имя модели: токенов}.
    Читает все ключи settings вида qwen_tokens_<модель> (их уменьшает
    spend_qwen_tokens). Показывается в панели «📡 Настройки API».
    Значение может быть отрицательным — квота исчерпана, пошёл платный расход."""
    prefix = "qwen_tokens_"
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",)
        ).fetchall()
    out = {}
    for row in rows:
        try:
            out[row["key"][len(prefix):]] = int(float(row["value"] or 0))
        except (TypeError, ValueError):
            out[row["key"][len(prefix):]] = 0
    return out


def register_api_call(model_name: str):
    """Регистрирует успешный вызов API модели."""
    with _lock:
        conn = _get_connection()
        conn.execute("INSERT INTO api_calls (model_name) VALUES (?)", (model_name,))
        conn.commit()
    # Лог убран: дублировал строку «✅ Ответила …» (модель видна там). Запись в БД остаётся.


def clear_api_calls() -> int:
    """Полная очистка счётчиков вызовов API — месячный сброс статистики
    (jobs/cleanup.py::_monthly_stats_reset, первое число месяца). Возвращает число
    удалённых записей. Обмены «вопрос-ответ» (user_token_usage) не трогает."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM api_calls")
        deleted = cur.rowcount
        conn.commit()
    return deleted


def clear_user_token_usage() -> int:
    """Месячный сброс обменов «вопрос-ответ»: обнуляет счётчики токенов и
    запросов всех пользователей (jobs/cleanup.py::_monthly_stats_reset).
    ВАЖНО: именно UPDATE до нулей, а НЕ DELETE — init_db при полностью пустой
    таблице заново заполнил бы её старыми числами из устаревшей user_context
    (одноразовый перенос в init_db выше). Возвращает число затронутых строк."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute(
            "UPDATE user_token_usage SET total_tokens = 0, total_requests = 0"
        )
        affected = cur.rowcount
        conn.commit()
    return affected


def register_image_call(user_id: int):
    """Списывает одну генерацию картинки из суточного лимита пользователя.

    С 2026-07-13 вызывается ДО генерации (аванс): обработчик /imagine не
    блокирует очередь апдейтов (block=False), и списание после генерации
    позволило бы залпом команд обойти лимит. Если генерация не удалась,
    попытка возвращается через unregister_image_call."""
    with _lock:
        conn = _get_connection()
        conn.execute("INSERT INTO user_image_calls (user_id) VALUES (?)", (user_id,))
        conn.commit()
    logger.info("🎨 Генерация картинки учтена в лимите (пользователь %s)", user_id)


def unregister_image_call(user_id: int):
    """Возвращает пользователю попытку генерации, списанную авансом
    (register_image_call), если картинка не получилась: удаляет самую
    свежую запись этого пользователя из user_image_calls."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "DELETE FROM user_image_calls WHERE id = ("
            "SELECT id FROM user_image_calls WHERE user_id=? ORDER BY id DESC LIMIT 1)",
            (user_id,),
        )
        conn.commit()
    logger.info("🎨 Попытка генерации возвращена в лимит (пользователь %s)", user_id)


def get_remaining_image_calls(user_id: int, default_limit: int) -> int:
    """Возвращает количество оставшихся генераций картинок для пользователя
    на текущие сутки.

    Сутки отсчитываются по Киеву (Europe/Kyiv): лимит сбрасывается в 00:00 по
    киевскому времени, перевод часов зима/лето учитывается автоматически (через
    базу часовых поясов из пакета tzdata). called_at в БД хранится в UTC, поэтому
    мы считаем момент киевской полуночи и переводим его в UTC для сравнения.
    """
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        kyiv = ZoneInfo("Europe/Kyiv")
    except Exception:
        kyiv = None

    cutoff = None
    if kyiv is not None:
        now_kyiv = datetime.now(kyiv)
        midnight_kyiv = now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = midnight_kyiv.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with _lock:
        conn = _get_connection()
        if cutoff is not None:
            # called_at и cutoff — оба в формате UTC "ГГГГ-ММ-ДД ЧЧ:ММ:СС",
            # строковое сравнение для него совпадает с хронологическим.
            cnt_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM user_image_calls WHERE user_id=? AND called_at >= ?",
                (user_id, cutoff),
            ).fetchone()
        else:
            # Подстраховка, если база часовых поясов недоступна (нет tzdata):
            # фиксированное смещение Киева летом (UTC+3), без учёта перевода часов.
            cnt_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM user_image_calls WHERE user_id=? AND date(called_at, '+3 hours') = date('now', '+3 hours')",
                (user_id,),
            ).fetchone()
        calls_today = cnt_row["cnt"] if cnt_row else 0
        remaining = default_limit - calls_today
    return max(0, remaining)


def get_active_system_prompt() -> tuple:
    """
    Возвращает текущий активный системный промпт.
    
    Логика:
      1. Если в БД есть custom_system_prompt → используем его как базу
      2. Иначе → используем SYSTEM_PROMPT из config.py
      3. Если в БД есть prompt_additions → дописываем их в конец базы
    
    Возвращает кортеж (prompt: str, is_custom: bool, additions: str)
    """
    from config import SYSTEM_PROMPT
    
    custom = get_setting("custom_system_prompt", "")
    additions = get_setting("prompt_additions", "")
    
    if custom:
        base = custom
        is_custom = True
    else:
        base = SYSTEM_PROMPT
        is_custom = False
    
    if additions:
        base += "\n\n" + additions
    
    return (base, is_custom, additions)


def append_prompt_addition(text: str):
    """
    Дописывает дополнительные правила/инструкции к промпту.
    Несколько вызовов накапливаются через перевод строки.
    """
    current = get_setting("prompt_additions", "")
    if current:
        new_value = current + "\n" + text
    else:
        new_value = text
    set_setting("prompt_additions", new_value)


def get_news_system_prompt() -> str:
    """
    Возвращает системный промпт для форматирования новостей (format_news_as_colonel).
    Хранится в settings под ключом 'news_system_prompt'.
    Если не задан — возвращает пустую строку (бот работает без системного промпта).
    """
    return get_setting("news_system_prompt", "")


def get_rag_instruction() -> str:
    """
    Возвращает «шапку»-инструкцию, которая уходит модели перед найденными
    статьями RAG (services/gemini.py дописывает под неё сами статьи).

    Живой текст хранится в settings под ключом 'rag_instruction'. В отличие от
    промпта новостей, ПУСТОЕ значение НЕ означает «без инструкции»: если админ
    ничего не задал или сбросил — возвращаем заводской текст RAG_INSTRUCTION
    (совсем без обёртки статьи не отправляем — так безопаснее).
    """
    from config import RAG_INSTRUCTION
    return get_setting("rag_instruction", "").strip() or RAG_INSTRUCTION


def get_proactive_instruction() -> str:
    """
    Возвращает инструкцию участия в разговоре групп (проактивный режим,
    services/proactive.py): по ней модель решает, вступить ли в беседу.

    Живой текст хранится в settings под ключом 'proactive_instruction'.
    Как у RAG-инструкции: пустое/сброшенное значение → заводской текст
    PROACTIVE_INSTRUCTION (без инструкции модель не поймёт правила игры).
    """
    from config import PROACTIVE_INSTRUCTION
    return get_setting("proactive_instruction", "").strip() or PROACTIVE_INSTRUCTION


# ───────────────────────────────────────────────
#  Сбор сообщений группы (Stage 3 — контекстный бот)
# ─────────────────────────────────────────────

def save_group_message(
    chat_id: int,
    user_id: int,
    username: str,
    first_name: str,
    text: str,
    has_photo: bool = False,
    has_voice: bool = False,
    has_video: bool = False,
):
    """Сохраняет сообщение группы в архив для будущего анализа контекста."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO group_messages (chat_id, user_id, username, first_name, text, has_photo, has_voice, has_video)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, user_id, username or "", first_name or "", text or "",
             1 if has_photo else 0, 1 if has_voice else 0, 1 if has_video else 0),
        )
        conn.commit()


def set_proactive_reset_mark() -> str:
    """
    Подводит «черту» под стенограммой групп для режима «Сам в разговор»
    (кнопка «🧹Очистить РАЗГОВОРЫ» в панели промптов): бот перестаёт видеть
    всё, что было сказано ДО этого момента, во ВСЕХ чатах и у всех людей
    сразу — включая владельцев и модераторов.

    НИЧЕГО НЕ УДАЛЯЕТ. Архив group_messages остаётся цел, поэтому не страдают
    ни счётчик «архив группы за 10 дней» в /stats, ни запасной источник имён
    для списка /users. Старое уйдёт само обычной суточной чисткой (10 дней).
    Решение обратимо: сотри ключ proactive_reset_mark — стенограмма вернётся.

    Время берём у самой SQLite (CURRENT_TIMESTAMP, UTC) — тем же способом,
    каким заполняется created_at: иначе форматы могли бы разъехаться и
    сравнение строк перестало бы совпадать с хронологическим.
    Возвращает поставленную метку.
    """
    with _lock:
        conn = _get_connection()
        mark = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
    # set_setting берёт _lock сам — только ПОСЛЕ выхода из блока выше
    # (обычный threading.Lock не реентерабельный, вложение = взаимоблокировка).
    set_setting("proactive_reset_mark", mark)
    return mark


def get_recent_group_messages(chat_id: int, limit: int = 25) -> list[dict]:
    """
    Последние `limit` сообщений конкретного чата из архива групп,
    в порядке от старых к новым — стенограмма беседы для проактивного
    участия в разговоре (services/proactive.py, gemini.ask_group_proactive).

    Уважает «черту» proactive_reset_mark (см. set_proactive_reset_mark):
    сказанное до неё в стенограмму не попадает, хотя физически остаётся
    в архиве.
    """
    # get_setting берёт _lock сам — читаем ДО входа в блок ниже.
    mark = get_setting("proactive_reset_mark", "")

    sql = ("SELECT user_id, username, first_name, text, has_photo, has_voice, has_video "
           "FROM group_messages WHERE chat_id=?")
    params: list = [chat_id]
    if mark:
        sql += " AND created_at > ?"
        params.append(mark)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with _lock:
        conn = _get_connection()
        rows = conn.execute(sql, params).fetchall()
    rows = list(reversed(rows))
    return [
        {
            "user_id": r["user_id"],
            "username": r["username"],
            "first_name": r["first_name"],
            "text": r["text"],
            "has_photo": bool(r["has_photo"]),
            "has_voice": bool(r["has_voice"]),
            "has_video": bool(r["has_video"]),
        }
        for r in rows
    ]


# ───────────────────────────────────────────────
#  Статистика бота (для admin-панели)
# ─────────────────────────────────────────────

def _kyiv_today_start_utc() -> str | None:
    """Начало текущих суток по Киеву в формате UTC «ГГГГ-ММ-ДД ЧЧ:ММ:СС» —
    для сравнения с called_at/created_at (они хранятся в UTC). None — если
    база часовых поясов недоступна (нет tzdata)."""
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        kyiv = ZoneInfo("Europe/Kyiv")
    except Exception:
        return None
    now_kyiv = datetime.now(kyiv)
    midnight_kyiv = now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_kyiv.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_bot_stats() -> dict:
    """Возвращает сводную статистику бота для /stats команды администратора."""
    # Сутки для счётчика «Сегодня» — по Киеву (как лимит картинок и месячный
    # сброс), а не по UTC: иначе вечером счётчик уже относился бы к «завтра».
    today_cutoff = _kyiv_today_start_utc()
    with _lock:
        conn = _get_connection()

        # Обмены «вопрос-ответ» за текущий месяц: user_token_usage копится
        # с последнего месячного сброса (jobs/cleanup.py::_monthly_stats_reset;
        # каждый успешный ответ модели = +1 к total_requests).
        try:
            lifetime_requests = conn.execute(
                "SELECT COALESCE(SUM(total_requests), 0) FROM user_token_usage"
            ).fetchone()[0]
        except Exception:
            lifetime_requests = 0

        api_calls_total = conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        if today_cutoff is not None:
            # called_at и cutoff — оба в UTC «ГГГГ-ММ-ДД ЧЧ:ММ:СС», строковое
            # сравнение совпадает с хронологическим (как в get_remaining_image_calls).
            api_calls_today = conn.execute(
                "SELECT COUNT(*) FROM api_calls WHERE called_at >= ?",
                (today_cutoff,),
            ).fetchone()[0]
        else:
            # Подстраховка без tzdata: фиксированное летнее смещение Киева (UTC+3)
            api_calls_today = conn.execute(
                "SELECT COUNT(*) FROM api_calls WHERE date(called_at, '+3 hours') = date('now', '+3 hours')"
            ).fetchone()[0]

        api_calls_by_model = conn.execute(
            "SELECT model_name, COUNT(*) as cnt FROM api_calls "
            "GROUP BY model_name ORDER BY cnt DESC"
        ).fetchall()

        # Архив групп чистится через 10 дней (jobs/cleanup.py::cleanup_loop),
        # поэтому COUNT — это «за последние 10 дней», а не за всё время.
        try:
            group_msg_count = conn.execute(
                "SELECT COUNT(*) FROM group_messages"
            ).fetchone()[0]
        except Exception:
            group_msg_count = 0

        subscriptions = conn.execute(
            "SELECT COUNT(*) FROM news_subscriptions"
        ).fetchone()[0]

        active_model_row = conn.execute(
            "SELECT value FROM settings WHERE key='active_model'"
        ).fetchone()


    return {
        "lifetime_requests": lifetime_requests,
        "api_calls_total": api_calls_total,
        "api_calls_today": api_calls_today,
        "api_calls_by_model": [(r["model_name"], r["cnt"]) for r in api_calls_by_model],
        "group_msg_count": group_msg_count,
        "subscriptions": subscriptions,
        "active_model": active_model_row["value"] if active_model_row else "unknown",
    }


# ───────────────────────────────────────────────
#  Снимки счётчиков для суточного отчёта о расходах
#  (таблица stats_snapshots; сборка отчёта — services/daily_report.py)
# ─────────────────────────────────────────────

def save_stats_snapshot(taken_at_utc: str, kyiv_label: str, data: dict) -> None:
    """Сохраняет «фотографию» счётчиков расходов на момент taken_at_utc.
    Снимок делается раз в сутки в полночь по Киеву (jobs/reports.py::daily_report_loop)
    и один раз при первом запуске бота — с него начинается отсчёт."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO stats_snapshots (taken_at_utc, kyiv_label, data) VALUES (?, ?, ?)",
            (taken_at_utc, kyiv_label, json.dumps(data, ensure_ascii=False)),
        )
        conn.commit()


def get_last_stats_snapshot() -> dict | None:
    """Последний снимок счётчиков: {taken_at_utc, kyiv_label, data} или None,
    если снимков ещё нет (первый запуск после внедрения отчёта).
    Испорченный JSON = снимка нет: отчёт лучше пропустить, чем соврать."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT taken_at_utc, kyiv_label, data FROM stats_snapshots "
            "ORDER BY taken_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        return None
    return {
        "taken_at_utc": row["taken_at_utc"],
        "kyiv_label": row["kyiv_label"],
        "data": data if isinstance(data, dict) else {},
    }


def get_prev_stats_snapshot() -> dict | None:
    """ПРЕДпоследний снимок — начало последних закрытых суток. Нужен кнопке
    «📊 Отчёт за вчера»: пара «предпоследний → последний» и есть вчерашний
    период. None, если снимков меньше двух."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT taken_at_utc, kyiv_label, data FROM stats_snapshots "
            "ORDER BY taken_at_utc DESC, id DESC LIMIT 2"
        ).fetchall()
    if len(rows) < 2:
        return None
    row = rows[1]
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        return None
    return {
        "taken_at_utc": row["taken_at_utc"],
        "kyiv_label": row["kyiv_label"],
        "data": data if isinstance(data, dict) else {},
    }


def count_api_calls_between(start_utc: str, end_utc: str | None = None) -> list:
    """Вызовы API по моделям за период [start_utc, end_utc): [(модель, число)].
    Время хранится в UTC строкой «ГГГГ-ММ-ДД ЧЧ:ММ:СС» — строковое сравнение
    совпадает с хронологическим (как в get_bot_stats). end_utc не задан —
    считаем до текущего момента."""
    sql = ("SELECT model_name, COUNT(*) AS cnt FROM api_calls "
           "WHERE called_at >= ?")
    params = [start_utc]
    if end_utc:
        sql += " AND called_at < ?"
        params.append(end_utc)
    sql += " GROUP BY model_name ORDER BY cnt DESC"
    with _lock:
        conn = _get_connection()
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [(r["model_name"], r["cnt"]) for r in rows]


def delete_old_stats_snapshots(days: int = 400) -> int:
    """Чистка снимков старше N суток (суточный цикл jobs/cleanup.py::cleanup_loop).
    Таблица крошечная (один снимок в сутки), но расти вечно ей незачем.
    ⚠️ Последний снимок не удаляется НИКОГДА — от него отсчитывается текущий
    период; без него отчёт остался бы без точки отсчёта."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute(
            "DELETE FROM stats_snapshots WHERE taken_at_utc < datetime('now', ?) "
            "AND id <> (SELECT id FROM stats_snapshots ORDER BY taken_at_utc DESC, id DESC LIMIT 1)",
            (f"-{int(days)} days",),
        )
        deleted = cur.rowcount
        conn.commit()
    return deleted


# ───────────────────────────────────────────────
#  Очистка архива групповых сообщений (retention)
# ───────────────────────────────────────────────

def delete_old_group_messages(days: int = 10) -> int:
    """
    Удаляет из group_messages записи старше `days` дней.

    created_at хранится как CURRENT_TIMESTAMP (UTC), и datetime('now') тоже UTC —
    сравнение корректное. Возвращает число удалённых строк.
    """
    with _lock:
        conn = _get_connection()
        cur = conn.execute(
            "DELETE FROM group_messages WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        deleted = cur.rowcount
        conn.commit()
    if deleted:
        logger.info("🚀 Очистка архива групп: удалено %d записей старше %d дней", deleted, days)
    return deleted


# ───────────────────────────────────────────────
#  Модерация: журнал действий + улики (для панели /mod)
#  Персистентная замена прежним in-memory счётчикам антиспама.
#  Статистика считается за скользящее окно в N дней; улики — тексты
#  удалённых при муте сообщений. Медиа не хранятся (только has_photo).
# ───────────────────────────────────────────────

def log_moderation_action(action: str, chat_id: int, user_id: int, name: str | None = None,
                          admin_name: str | None = None) -> int:
    """
    Записывает действие модерации ('mute'/'unmute'/'linkdel') в журнал. Возвращает id строки.
    admin_name — кто выполнил (передаётся при размуте кнопкой или /unmute; у автоматики None).
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        cur = conn.execute(
            "INSERT INTO moderation_log (ts, action, chat_id, user_id, name, admin_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_time.time(), action, chat_id, user_id, name or str(user_id), admin_name),
        )
        log_id = cur.lastrowid
        conn.commit()
    return log_id


def save_mute_evidence(log_id: int, messages: list) -> None:
    """
    Сохраняет тексты удалённых сообщений для мута (log_id).
    messages — список dict с ключами text (str) и has_photo (bool).
    Текст обрезается до 1000 символов на сообщение (защита от гигантских вставок).
    """
    if not log_id or not messages:
        return
    with _lock:
        conn = _get_connection()
        for i, m in enumerate(messages):
            conn.execute(
                "INSERT INTO mute_evidence (log_id, ord, text, has_photo) VALUES (?, ?, ?, ?)",
                (log_id, i, (m.get("text") or "")[:1000], 1 if m.get("has_photo") else 0),
            )
        conn.commit()


def get_moderation_counts(days: int = 7) -> dict:
    """Сколько мутов/размутов было за последние `days` дней (по журналу)."""
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT action, COUNT(*) AS n FROM moderation_log WHERE ts >= ? GROUP BY action",
            (cutoff,),
        ).fetchall()
    counts = {"mutes": 0, "unmutes": 0, "linkdels": 0, "kicks": 0, "bans": 0}
    for r in rows:
        if r["action"] in ("mute", "mute_adm", "mute_ai"):
            # mute — автоматика (антифлуд/ссылки), mute_adm — ручной мут админа
            # из карточки пользователя, mute_ai — мут, который бот выдал сам
            # в режиме «Сам в разговор». В сводке /mod считаем вместе.
            # ⚠️ НОВЫЙ ТИП МУТА ДОБАВЛЯТЬ И СЮДА: иначе он будет виден в списке
            # последних действий, но пропадёт из счётчика «За 7 дней» —
            # панель начнёт занижать цифру (наступили на этом 2026-07-20).
            counts["mutes"] += r["n"]
        elif r["action"] in ("unmute", "unban"):
            counts["unmutes"] += r["n"]
        elif r["action"] == "linkdel":
            # Удаления ссылок фильтром (services/antispam.py::check_and_delete_links)
            counts["linkdels"] = r["n"]
        elif r["action"] == "kick":
            counts["kicks"] = r["n"]
        elif r["action"] == "ban":
            counts["bans"] = r["n"]
    return counts


def get_recent_moderation_actions(limit: int = 5) -> list:
    """Последние действия модерации (новые первыми) из журнала."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT id, ts, action, chat_id, user_id, name, admin_name FROM moderation_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_moderation_entry(log_id: int) -> dict | None:
    """Одна строка журнала по id (для экрана улик)."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT id, ts, action, chat_id, user_id, name, admin_name FROM moderation_log WHERE id = ?",
            (log_id,),
        ).fetchone()
    return dict(row) if row else None


def get_mute_evidence(log_id: int) -> list:
    """Тексты удалённых сообщений конкретного мута (по порядку)."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT ord, text, has_photo FROM mute_evidence WHERE log_id = ? ORDER BY ord ASC",
            (log_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
#  Личное дело участника групп (вечное) — стаж и счётчики для статуса
#  «проверенный» (services/antispam.py) и блока «Служба» в /rank
# ─────────────────────────────────────────────

def dossier_add_message(user_id: int, username: str = "", first_name: str = "") -> None:
    """+1 сообщение в личное дело. Первое сообщение заводит запись —
    с него начинается стаж (first_seen).

    Заодно освежает имя и ник: они нужны списку пользователей в админ-панели, а
    архив групп, откуда их можно было взять раньше, чистится каждые 10 дней.
    Пустое имя НЕ затирает уже сохранённое (COALESCE + NULLIF): у пользователя
    без ника в деле останется прошлое значение, а не пустая строка.
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO user_dossier (user_id, first_seen, msg_count, username, first_name)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   msg_count  = msg_count + 1,
                   username   = COALESCE(NULLIF(excluded.username, ''), user_dossier.username),
                   first_name = COALESCE(NULLIF(excluded.first_name, ''), user_dossier.first_name)""",
            (user_id, _time.time(), username or "", first_name or ""),
        )
        conn.commit()


def dossier_add_mute(user_id: int) -> None:
    """+1 мут в личное дело + метка времени (на TRUST_FORGIVE_DAYS лишает
    статуса «проверенный»)."""
    import time as _time
    now = _time.time()
    with _lock:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO user_dossier (user_id, first_seen, msg_count, mute_count, last_mute_ts)
               VALUES (?, ?, 0, 1, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   mute_count   = mute_count + 1,
                   last_mute_ts = excluded.last_mute_ts""",
            (user_id, now, now),
        )
        conn.commit()


def dossier_add_linkdel(user_id: int) -> None:
    """+1 удалённая фильтром ссылка в личное дело."""
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO user_dossier (user_id, first_seen, msg_count, link_count)
               VALUES (?, ?, 0, 1)
               ON CONFLICT(user_id) DO UPDATE SET link_count = link_count + 1""",
            (user_id, _time.time()),
        )
        conn.commit()


def dossier_reset_violations(user_id: int) -> None:
    """
    Обнуляет нарушения в личном деле: муты, удалённые ссылки и метку последнего
    мута (кнопка «♻️ Обнулить нарушения» в карточке пользователя). Стаж и счётчик
    сообщений НЕ трогаются — это заслуженное, а не наказание. Снимает «взыскание»
    досрочно: человек сразу может стать «проверенным» по общему правилу.
    """
    with _lock:
        conn = _get_connection()
        conn.execute(
            "UPDATE user_dossier SET mute_count = 0, link_count = 0, last_mute_ts = NULL "
            "WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()


def get_dossier(user_id: int) -> dict | None:
    """Личное дело пользователя или None, если он ещё не писал в группах."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT user_id, first_seen, msg_count, mute_count, last_mute_ts, link_count, "
            "username, first_name "
            "FROM user_dossier WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


# ─────────────────────────────────────────────
#  Известные группы (карточка пользователя: где выдавать мут/кик/бан)
# ─────────────────────────────────────────────

def remember_chat(chat_id: int, title: str = "") -> None:
    """
    Запоминает группу, где работает бот (зовётся из collect_group_message).
    Название обновляется на каждое сообщение — переименование группы
    подхватывается само. Пустое название не затирает сохранённое.
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO known_chats (chat_id, title, last_seen)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   title     = COALESCE(NULLIF(excluded.title, ''), known_chats.title),
                   last_seen = excluded.last_seen""",
            (chat_id, title or "", _time.time()),
        )
        conn.commit()


def get_known_chats() -> list[dict]:
    """Группы, где работает бот (новые по активности сверху)."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT chat_id, title, last_seen FROM known_chats "
            "ORDER BY last_seen DESC NULLS LAST, chat_id"
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
#  Пользователи и их персональные настройки (панель «👥 Пользователи»)
# ─────────────────────────────────────────────

def list_known_users(limit: int = 100) -> list[dict]:
    """
    Все, кого знает бот, — для списка в панели «👥 Пользователи».

    Собирает из четырёх источников: личное дело (кто писал в группах),
    архив групп (самые свежие имена), статистика викторины и учёт токенов
    (кто общался с ботом только в личке — в личном деле его нет).
    Сортировка: сначала активные в группах, затем остальные.
    """
    users: dict[int, dict] = {}

    def _slot(uid: int) -> dict:
        return users.setdefault(uid, {
            "user_id": uid, "username": "", "first_name": "", "quiz_name": "",
            "msg_count": 0, "mute_count": 0, "link_count": 0, "in_dossier": False,
        })

    with _lock:
        conn = _get_connection()

        for r in conn.execute(
            "SELECT user_id, msg_count, mute_count, link_count, username, first_name "
            "FROM user_dossier"
        ).fetchall():
            u = _slot(r["user_id"])
            u["msg_count"] = r["msg_count"] or 0
            u["mute_count"] = r["mute_count"] or 0
            u["link_count"] = r["link_count"] or 0
            u["username"] = r["username"] or ""
            u["first_name"] = r["first_name"] or ""
            u["in_dossier"] = True

        # Архив групп: имена свежее, чем в деле, если человек сменил ник.
        # Берём последнюю запись каждого (MAX(id)).
        for r in conn.execute(
            "SELECT g.user_id, g.username, g.first_name FROM group_messages g "
            "JOIN (SELECT user_id, MAX(id) AS mid FROM group_messages GROUP BY user_id) t "
            "ON g.id = t.mid"
        ).fetchall():
            u = _slot(r["user_id"])
            u["username"] = r["username"] or u["username"]
            u["first_name"] = r["first_name"] or u["first_name"]

        for r in conn.execute("SELECT user_id, username FROM quiz_stats").fetchall():
            _slot(r["user_id"])["quiz_name"] = r["username"] or ""

        for r in conn.execute("SELECT user_id FROM user_token_usage").fetchall():
            _slot(r["user_id"])


    out = sorted(users.values(), key=lambda u: (-u["msg_count"], u["user_id"]))
    return out[:limit]


# Колонки user_settings, которые разрешено менять из панели. Белый список —
# защита от подстановки чужого имени колонки в SQL (значения-то подставляются
# параметрами, а имя колонки приходится вклеивать в текст запроса).
_USER_SETTING_FIELDS = (
    "antispam_msg_count", "antispam_window_sec", "antispam_mute_sec",
    "antispam_immune", "links_allowed", "ai_ignored", "image_limit",
    "honorary_rank", "note",
)


def get_user_settings(user_id: int) -> dict | None:
    """Персональные настройки участника или None, если их никогда не задавали."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def get_all_user_settings() -> list[dict]:
    """Все персональные настройки разом — для загрузки кэша при старте бота."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute("SELECT * FROM user_settings").fetchall()
    return [dict(r) for r in rows]


def set_user_settings(user_id: int, **fields) -> None:
    """
    Записывает персональные настройки участника (только поля из
    _USER_SETTING_FIELDS). Значение None означает «вернуть на общую настройку
    бота» — так и пишется в БД, пустотой.
    """
    import time as _time
    cols = [k for k in fields if k in _USER_SETTING_FIELDS]
    if not cols:
        return
    with _lock:
        conn = _get_connection()
        conn.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
        assign = ", ".join(f"{c} = ?" for c in cols)
        conn.execute(
            f"UPDATE user_settings SET {assign}, updated_at = ? WHERE user_id = ?",
            [fields[c] for c in cols] + [_time.time(), user_id],
        )
        conn.commit()


def clear_user_settings(user_id: int) -> None:
    """Убирает ВСЕ персональные настройки участника — он возвращается на общие."""
    with _lock:
        conn = _get_connection()
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        conn.commit()


# ─────────────────────────────────────────────
#  Модераторы и их права (роли — services/roles.py)
#  ВЛАДЕЛЬЦЫ здесь не хранятся: они в config.ADMIN_IDS.
# ─────────────────────────────────────────────

# Графы прав в таблице staff. Белый список — защита от подстановки чужого
# имени колонки в SQL (значения подставляются параметрами, имя колонки — нет).
_STAFF_PERM_FIELDS = ("p_mod", "p_ban", "p_cards", "p_cards_edit", "p_antispam")


def get_all_staff() -> list[dict]:
    """Все модераторы с правами — для загрузки кэша при старте бота."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute("SELECT * FROM staff ORDER BY granted_at").fetchall()
    return [dict(r) for r in rows]


def get_staff(user_id: int) -> dict | None:
    """Права одного модератора или None, если он не модератор."""
    with _lock:
        conn = _get_connection()
        row = conn.execute("SELECT * FROM staff WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def add_staff(user_id: int, granted_by: int) -> None:
    """
    Делает человека модератором БЕЗ прав (все галочки сняты) — права выдаются
    отдельно. Повторный вызов не сбрасывает уже выданные права.
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO staff (user_id, granted_by, granted_at) VALUES (?, ?, ?)",
            (user_id, granted_by, _time.time()),
        )
        conn.commit()


def remove_staff(user_id: int) -> None:
    """Снимает звание модератора вместе со всеми правами."""
    with _lock:
        conn = _get_connection()
        conn.execute("DELETE FROM staff WHERE user_id = ?", (user_id,))
        conn.commit()


def set_staff_perm(user_id: int, field: str, value: int) -> None:
    """Ставит или снимает одну галочку права (только из _STAFF_PERM_FIELDS)."""
    if field not in _STAFF_PERM_FIELDS:
        return
    with _lock:
        conn = _get_connection()
        conn.execute(f"UPDATE staff SET {field} = ? WHERE user_id = ?", (int(value), user_id))
        conn.commit()


# ─────────────────────────────────────────────
#  Журнал действий персонала (экран «📋 Журнал персонала», только владельцу)
# ─────────────────────────────────────────────

def log_staff_action(actor_id: int, actor_name: str, action: str,
                     target_id: int = 0, details: str = "") -> None:
    """
    Записывает осознанное действие персонала: модерацию, смену настроек,
    выдачу прав. В отличие от moderation_log сюда НЕ пишет автоматика —
    только то, что сделал человек своими руками.
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO staff_log (ts, actor_id, actor_name, action, target_id, details) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_time.time(), actor_id, actor_name or str(actor_id), action, target_id, details or ""),
        )
        conn.commit()


def get_recent_staff_actions(limit: int = 20) -> list:
    """Последние действия персонала (новые первыми)."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT id, ts, actor_id, actor_name, action, target_id, details "
            "FROM staff_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_staff_actions(days: int = 7) -> int:
    """Сколько действий персонала за последние `days` дней."""
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        row = conn.execute("SELECT COUNT(*) AS n FROM staff_log WHERE ts >= ?", (cutoff,)).fetchone()
    return row["n"] if row else 0


def delete_old_staff_log(days: int = 30) -> int:
    """Удаляет записи журнала персонала старше `days` дней (чистит cleanup_loop)."""
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM staff_log WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount or 0
        conn.commit()
    if deleted:
        logger.info("🧹 Журнал персонала: удалено %d записей старше %d дней", deleted, days)
    return deleted


def clear_staff_log() -> int:
    """Полная очистка журнала персонала (кнопка «🧹 Очистить журнал»)."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM staff_log")
        deleted = cur.rowcount or 0
        conn.commit()
    return deleted


# ─────────────────────────────────────────────
#  Журнал действий с базой знаний RAG (панель /rag)
# ─────────────────────────────────────────────

def add_kb_action(action: str, article: str, user_id: int = 0) -> None:
    """
    Записывает действие с базой знаний: «одобрена», «удалена из базы»,
    «удалена из ожидания», «заменена», «добавлена», «пересборка».
    article — человекочитаемое название статьи (не имя файла).
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO knowledge_log (ts, action, article, user_id) VALUES (?, ?, ?, ?)",
            (_time.time(), action, (article or "")[:200], user_id),
        )
        conn.commit()


def get_recent_kb_actions(limit: int = 5) -> list:
    """Последние действия с базой знаний (новые первыми)."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT id, ts, action, article, user_id FROM knowledge_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_old_kb_log(days: int = 30) -> int:
    """Удаляет записи журнала базы знаний старше `days` дней. Возвращает число удалённых."""
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM knowledge_log WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
    if deleted:
        logger.info("🚀 Очистка журнала базы знаний: удалено %d записей старше %d дней", deleted, days)
    return deleted


def clear_kb_log() -> int:
    """Полная очистка журнала базы знаний (кнопка «🧹 Очистить журнал»). Возвращает число удалённых."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM knowledge_log")
        deleted = cur.rowcount
        conn.commit()
    return deleted


def delete_old_moderation_log(days: int = 7) -> int:
    """
    Удаляет из журнала модерации записи старше `days` дней вместе с их уликами.
    Возвращает число удалённых строк журнала.
    """
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        old_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM moderation_log WHERE ts < ?", (cutoff,)
        ).fetchall()]
        if old_ids:
            qmarks = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM mute_evidence WHERE log_id IN ({qmarks})", old_ids)
            conn.execute(f"DELETE FROM moderation_log WHERE id IN ({qmarks})", old_ids)
        conn.commit()
    deleted = len(old_ids)
    if deleted:
        logger.info("🛡 Очистка журнала модерации: удалено %d записей старше %d дней", deleted, days)
    return deleted


# ───────────────────────────────────────────────
#  Журнал проактивных проверок (2026-07-31)
# ─────────────────────────────────────────────

def log_proactive_check(chat_id: int, outcome: str, model: str = "",
                        seconds: float = 0.0, reply_len: int = 0,
                        trigger_kind: str = "text") -> None:
    """
    Записывает ОДНУ проактивную проверку — то есть один запрос к модели
    в режиме «Сам в разговор» и его исход.

    Исходы (`outcome`), их ровно пять и они соответствуют веткам
    services/proactive.py::_run_proactive — новый исход добавлять И ТАМ, И
    в подписи `_OUTCOME_TITLES` панели, иначе он покажется голым кодом:
      reply      — бот вступил в разговор;
      reply_mute — вступил и заодно сам выдал мут («руки»);
      silent     — модель решила промолчать (вернула «ПРОПУСК»);
      empty      — ответ состоял из одной пометки мута, говорить было нечего;
      error      — запрос сорвался (сеть, отказ всех моделей цепочки).

    ⚠️ Зовётся ТОЛЬКО из фоновой задачи проверки — не с горячего пути
    архиватора групп: там на каждое сообщение и походов в базу быть не должно.
    """
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO proactive_log (chat_id, outcome, model, seconds, reply_len, trigger_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, outcome, model, round(float(seconds), 2), int(reply_len), trigger_kind),
        )
        conn.commit()


def proactive_stats(start_utc: str, end_utc: str | None = None) -> dict:
    """
    Итоги проактивных проверок за промежуток. Границы — строки UTC
    «ГГГГ-ММ-ДД ЧЧ:ММ:СС», как у остальных счётчиков по времени; правая
    граница строго меньше (как в суточном отчёте), пустая — «по сейчас».

    Возвращает словарь: сколько проверок всего, сколько каких исходов,
    среднее время ответа модели и разбивку по виду триггера.
    """
    where = "ts >= ?"
    params = [start_utc]
    if end_utc:
        where += " AND ts < ?"
        params.append(end_utc)

    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            f"SELECT outcome, COUNT(*) AS n, AVG(seconds) AS avg_sec "
            f"FROM proactive_log WHERE {where} GROUP BY outcome", params,
        ).fetchall()
        kinds = conn.execute(
            f"SELECT trigger_kind, COUNT(*) AS n FROM proactive_log "
            f"WHERE {where} GROUP BY trigger_kind", params,
        ).fetchall()

    out = {"checks": 0, "reply": 0, "reply_mute": 0, "silent": 0, "empty": 0,
           "error": 0, "avg_sec": 0.0, "by_trigger": {}}
    total_sec = 0.0
    for r in rows:
        n = r["n"] or 0
        out["checks"] += n
        out[r["outcome"]] = out.get(r["outcome"], 0) + n
        total_sec += (r["avg_sec"] or 0.0) * n
    if out["checks"]:
        out["avg_sec"] = total_sec / out["checks"]
    out["by_trigger"] = {r["trigger_kind"] or "text": r["n"] for r in kinds}
    return out


def proactive_by_chat(start_utc: str, limit: int = 10) -> list:
    """Проверки и вступления по чатам за промежуток, самые бойкие сверху."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT chat_id, COUNT(*) AS checks, "
            "       SUM(CASE WHEN outcome IN ('reply', 'reply_mute') THEN 1 ELSE 0 END) AS replies "
            "FROM proactive_log WHERE ts >= ? "
            "GROUP BY chat_id ORDER BY checks DESC LIMIT ?",
            (start_utc, limit),
        ).fetchall()
    return [(r["chat_id"], r["checks"] or 0, r["replies"] or 0) for r in rows]


def proactive_by_day(days: int = 7) -> list:
    """
    Проверки и вступления по дням — чтобы видеть, что изменилось после правки
    порога или промпта. Дни считаются ПО МЕСТНОМУ времени сервера (`localtime`),
    а он живёт в Europe/Kyiv — то есть по тем же суткам, что и отчёты.
    ⚠️ На машине с другим часовым поясом границы дней уедут; для показа это
    терпимо, но считать по ним деньги нельзя.
    """
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT date(ts, 'localtime') AS day, COUNT(*) AS checks, "
            "       SUM(CASE WHEN outcome IN ('reply', 'reply_mute') THEN 1 ELSE 0 END) AS replies "
            "FROM proactive_log WHERE ts >= datetime('now', ?) "
            "GROUP BY day ORDER BY day DESC",
            (f"-{int(days)} days",),
        ).fetchall()
    return [(r["day"], r["checks"] or 0, r["replies"] or 0) for r in rows]


def delete_old_proactive_log(days: int = 30) -> int:
    """Чистка журнала проверок (суточный цикл). Возвращает число удалённых."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute(
            "DELETE FROM proactive_log WHERE ts < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        deleted = cur.rowcount or 0
        conn.commit()
    if deleted:
        logger.info("🤖 Очистка журнала проактивных проверок: удалено %d записей старше %d дней",
                    deleted, days)
    return deleted


# ─── 👋 Журнал вступлений в группы (приветствие новичков) ───────────

def log_join(chat_id: int, user_id: int, name: str, outcome: str) -> None:
    """
    Пишет строку в журнал вступлений (services/greeter.py).

    outcome: 'join' — человек вошёл в группу, 'ok' — прошёл проверку
    «я не бот», 'timeout' — не нажал за отведённый срок, 'kick' — не нажал
    и был выгнан (кикать не прошедших — отдельный тумблер панели /mod).
    На одного новичка приходится ДВЕ строки: приход и исход, — иначе
    «пришло» и «прошли» пришлось бы считать по разным источникам.

    Время — time.time(), как в moderation_log: обе таблицы читает одна и та
    же панель, и разнобой форматов пришлось бы разбирать в ней.
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO join_log (ts, chat_id, user_id, name, outcome) VALUES (?, ?, ?, ?, ?)",
            (_time.time(), chat_id, user_id, name or str(user_id), outcome),
        )
        conn.commit()


def get_join_counts(days: int = 7) -> dict:
    """
    Сколько человек пришло и чем кончилась их проверка за последние `days`
    дней. Ключи: joins, ok, timeouts, kicks (неизвестные исходы игнорируются).
    """
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM join_log WHERE ts >= ? GROUP BY outcome",
            (cutoff,),
        ).fetchall()
    counts = {"joins": 0, "ok": 0, "timeouts": 0, "kicks": 0}
    keys = {"join": "joins", "ok": "ok", "timeout": "timeouts", "kick": "kicks"}
    for r in rows:
        key = keys.get(r["outcome"])
        if key:
            counts[key] = r["n"]
    return counts


def delete_old_join_log(days: int = 30) -> int:
    """Чистка журнала вступлений (суточный цикл). Возвращает число удалённых."""
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM join_log WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount or 0
        conn.commit()
    if deleted:
        logger.info("👋 Очистка журнала вступлений: удалено %d записей старше %d дней",
                    deleted, days)
    return deleted


def clear_moderation_log() -> int:
    """
    Полная очистка журнала модерации (кнопка «🧹 Очистить журнал» в /mod).
    Улики стираются вместе с журналом: они привязаны к его записям, и без
    журнала превратились бы в мусор, до которого уже ничем не добраться.
    Возвращает число удалённых записей журнала.
    """
    with _lock:
        conn = _get_connection()
        conn.execute("DELETE FROM mute_evidence")
        cur = conn.execute("DELETE FROM moderation_log")
        deleted = cur.rowcount or 0
        conn.commit()
    return deleted
