# ───────────────────────────────────────────────
#  database/people.py — люди: дела, персональные настройки, персонал
#  (02.09.2026).
#
#  Шаг 8 разреза history.py. Три связанные вещи об участниках:
#
#    user_dossier  — ВЕЧНОЕ личное дело: когда впервые увидели, сколько
#                    написал, сколько раз мутили, сколько ссылок удалили.
#                    Отсюда берётся статус «проверенный» (services/antispam.py)
#                    и блок «Служба» в /rank. Архив групп чистится через 10
#                    дней, дело — никогда
#    user_settings — ПЕРСОНАЛЬНЫЕ настройки участника: свои пороги антифлуда,
#                    иммунитет, разрешение ссылок, лимит картинок, звание
#    staff         — модераторы и их права
#
#  ⚠️ ВЛАДЕЛЬЦЕВ ЗДЕСЬ НЕТ. Они заданы в config.ADMIN_IDS и из панели не
#  назначаются и не снимаются — иначе управление ботом можно было бы потерять.
#
#  ⚠️ ДВА БЕЛЫХ СПИСКА КОЛОНОК (_USER_SETTING_FIELDS, _STAFF_PERM_FIELDS) —
#  ЭТО ЗАЩИТА, А НЕ УДОБСТВО. Значения в запросы подставляются параметрами, а
#  вот ИМЯ КОЛОНКИ приходится вклеивать в текст запроса — и без белого списка
#  туда можно было бы подставить чужое. Расширяешь список — понимай, что
#  открываешь колонку для правки снаружи.
#
#  ⚠️ ПУСТОЕ ЗНАЧЕНИЕ (None) В user_settings ЗНАЧИТ «работает общая настройка
#  бота», а не ноль. Персональный ноль — это значение (лимит картинок 0 =
#  полный запрет). Проверять только через «is None» — так и написано в
#  контракте services/user_settings.py.
#
#  ⚠️ КЭШ ЖИВЁТ НЕ ЗДЕСЬ. Персональные настройки читаются на КАЖДОЕ сообщение
#  группы, поэтому их держит в памяти services/user_settings.py, а права —
#  services/roles.py. Записал сюда мимо них — бот не заметит правку до
#  перезапуска. Единственный разрешённый путь правки описан в их шапках.
#
#  ⚠️ ПРОВЕРКАМИ ЭТА ТЕМА ПОЧТИ НЕ ПОКРЫТА. Группа «права доступа» в selftest
#  работает на кэше в памяти и в базу не ходит вовсе (так и написано в её
#  докстринге), а из всех функций файла вызовами покрыта одна —
#  set_user_settings, в группе фильтра ссылок. Правка здесь проверяется руками.
# ───────────────────────────────────────────────

from ._core import _lock, _get_connection

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
