# ───────────────────────────────────────────────
#  database/stats.py — счётчики бота и ночные снимки (02.09.2026).
#
#  Шаг 3 разреза history.py. Две связанные вещи:
#    get_bot_stats           — «сколько всего» для панели /stats и сайта
#    снимки stats_snapshots  — «сколько было в полночь», от них считается
#                              расход за сутки и за неделю
#
#  ⚠️ ЗДЕСЬ НЕ СЧИТАЮТ ДЕНЬГИ, здесь их только достают. Вся арифметика
#  расхода — в services/daily_report.py, и её проверяет selftest
#  (check_daily_report, check_report_render). Не переносить сюда «заодно»:
#  разъедется с проверками.
#
#  Кто это читает:
#    handlers/admin/panel_main.py — панели /stats и «📡 Настройки API»
#    services/daily_report.py     — суточный и недельный отчёты
#    jobs/cleanup.py              — итоги месяца и чистка старых снимков
#    web/pages.py                 — сводка на сайте
#
#  ⚠️ ВРЕМЯ ХРАНИТСЯ В UTC строкой «ГГГГ-ММ-ДД ЧЧ:ММ:СС», а сутки считаются
#  ПО КИЕВУ. Отсюда _kyiv_today_start_utc: он берёт киевскую полночь и
#  переводит её в UTC для сравнения. Строковое сравнение таких дат совпадает
#  с хронологическим — на этом держатся все запросы «за период» ниже.
# ───────────────────────────────────────────────

import json

from ._core import _lock, _get_connection

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


# ⚠️ ПРЕДПОСЛЕДНЕГО СНИМКА ЗДЕСЬ БОЛЬШЕ НЕТ (удалён 28.08.2026).
# Была функция get_prev_stats_snapshot, и её докстринг уверял, что пара
# «предпоследний → последний» нужна кнопке «📊 Отчёт за вчера». Читателей у неё
# не было НИ ОДНОГО, а описание сбивало с толку — карта рисков полгода носила
# подозрение «а верные ли цифры в отчёте за вчера».
#
# Как на самом деле (проверено на боевых данных 28.08.2026, сошлось до знака):
#   • суточный период считается «ПОСЛЕДНИЙ снимок → сейчас», а новый снимок
#     ставится сразу после сборки текста (services/daily_report.midnight_report);
#   • кнопка «Отчёт за вчера» ничего не пересчитывает — показывает СОХРАНЁННЫЙ
#     ночью текст (settings 'daily_report_last_text'). Пересборка была бы
#     враньём: первого числа месяца вызовы обнуляются, и исходных данных за
#     прошлые сутки в базе уже нет.
# Значит второй снимок не нужен ни для чего.


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
