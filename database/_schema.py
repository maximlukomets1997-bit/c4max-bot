# ───────────────────────────────────────────────
#  database/_schema.py — УСТРОЙСТВО базы: таблицы, графы, запуск (02.09.2026).
#
#  Шаг 1 разреза history.py (шаг 0 — фундамент, database/_core.py). Здесь
#  лежит всё, что отвечает на вопрос «из чего состоит база», и ничего из
#  того, что отвечает «что в ней лежит»:
#    _create_schema         — таблицы и индексы (только для НОВОЙ базы)
#    _COLUMN_MIGRATIONS     — графы, появившиеся позже создания базы
#    _run_column_migrations — их догоняющая установка
#    _seed_once             — разовое наполнение новых таблиц
#    init_db                — три шага выше по порядку; зовётся из main.py
#
#  ⚠️ СХЕМУ НЕ ДРОБИТЬ ПО ТЕМАМ. Разрез history.py идёт по темам (новости,
#  викторина, модерация …), и напрашивается разложить CREATE TABLE по тем же
#  файлам. Делать этого нельзя: тогда ответ на вопрос «какие вообще таблицы
#  есть» пришлось бы собирать по дюжине файлов, а правило «новая таблица —
#  дописать её в reset_db.py::USER_TABLES» стало бы невыполнимым на глаз.
#  Схема остаётся одним списком в одном месте.
#
#  ⚠️ Что проверяет предполётная проверка: она зовёт init_db на пустой
#  временной базе и сверяет получившиеся таблицы со списком в reset_db.py.
#  Забыть там новую таблицу нельзя — проверка покраснеет.
# ───────────────────────────────────────────────

import sqlite3
import logging

# Замок и соединение — из фундамента; своих здесь нет и быть не должно.
from ._core import _lock, _get_connection

logger = logging.getLogger(__name__)

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

        -- Банк вопросов викторины (2026-08-05, решение Максима). Раньше
        -- вопросы лежали СПИСКОМ В КОДЕ (data/quiz_questions.py, 12 штук на
        -- весь бот) — их выучивали за пару вечеров. Теперь их собирает по
        -- статьям базы знаний кнопка «🧠 Собрать вопросы» панели /quizadm
        -- (services/quiz_bank.py), а хранятся они здесь.
        --   approved = 0  черновик: собран, но в игру НЕ идёт;
        --   approved = 1  одобрен владельцем, викторина берёт только такие.
        -- ⚠️ В БАЗЕ, А НЕ В ФАЙЛЕ: файл, который бот переписывает сам, ломает
        -- обновление кода с GitHub (на этом уже наступили с
        -- knowledge_base_vectors.json — см. карту проекта).
        -- article — имя файла статьи-источника: по нему видно, какие статьи
        -- вопросами уже покрыты, и по нему же кнопка догоняет только новые.
        -- options — JSON-список вариантов ответа, correct_idx — номер верного.
        -- asked_count — сколько раз вопрос уже задавали (выбор идёт среди
        -- наименее заданных, поэтому база не крутит одно и то же).
        -- Статьи, по которым сборка вопросов НЕ ДАЛАСЬ (2026-08-05, просьба
        -- Максима после первой сборки: 11 статей из 40 остались без
        -- результата, и повторить именно их было нечем). Строка живёт ровно
        -- до первого удачного захода: `clear_quiz_failure` снимает её, как
        -- только по статье собрался хоть один вопрос.
        --   reason   — человеческая причина («модель не ответила», «не
        --              разобрать ответ», «все вопросы отбракованы»): по ней
        --              видно, ждать ли толку от повтора вообще;
        --   attempts — сколько раз уже пробовали: статья, не дающаяся третий
        --              раз, почти наверняка не даётся из-за себя самой, а не
        --              из-за молчащей модели.
        CREATE TABLE IF NOT EXISTS quiz_failed (
            article  TEXT PRIMARY KEY,
            ts       REAL    NOT NULL,
            reason   TEXT,
            attempts INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS quiz_bank (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            article     TEXT    NOT NULL,
            question    TEXT    NOT NULL,
            options     TEXT    NOT NULL,
            correct_idx INTEGER NOT NULL,
            explanation TEXT,
            approved    INTEGER DEFAULT 0,
            created_at  REAL    NOT NULL,
            asked_count INTEGER DEFAULT 0
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
        -- Вызовы моделей по времени: таблица чистится только раз в месяц и к
        -- концу месяца набирает десятки тысяч строк, а по времени её перебирают
        -- суточный и недельный отчёты (count_api_calls_between) и панель /stats.
        CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(called_at);
        -- Выбор вопроса викторины: только одобренные, реже всего заданные
        CREATE INDEX IF NOT EXISTS idx_quiz_bank_pick ON quiz_bank(approved, asked_count);
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
