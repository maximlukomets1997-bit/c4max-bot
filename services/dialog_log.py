# ─────────────────────────────────────────────
#  services/dialog_log.py — 👤 дословный лог ПРЯМЫХ обращений к боту
#  (2026-09-21, просьба Максима)
#
#  Брат services/chat_log.py. Тот пишет режим «Сам в разговор» — как бот сам
#  влезает в беседу группы. Этот пишет второй путь: когда к боту обращаются
#  НАПРЯМУЮ — личка, упоминание в группе, ответ на его сообщение, фото,
#  голосовое, видео.
#
#  ⚠️ ЗАПИСЬ ВЕДЁТСЯ НА ЧЕЛОВЕКА, А НЕ НА ЧАТ, и это не прихоть: память бота
#  устроена ровно так же. database/chat.py::get_history собирает контекст по
#  user_id ПО ВСЕМ чатам сразу — человек продолжает в личке разговор, начатый
#  в группе. Разложи запись по чатам — и в файле лички не будет половины того,
#  что реально ушло модели.
#
#  Файл на человека: logs/dialog/dialog <id>.log. Архива, как у chat_log, тут
#  нет: у каждого одна непрерывная запись, а от бесконечного роста её держит
#  потолок MAX_BYTES — переросла, и самое старое срезается сверху.
#
#  ⚠️ ЧЕРТЫ ПО «ОЧИСТИТЬ РАЗГОВОРЫ» ЗДЕСЬ НЕТ НАМЕРЕННО (решение Максима
#  2026-09-21). Та кнопка стирает память ПРОАКТИВНОГО режима и личного
#  контекста не трогает — связав с ней запись, мы получили бы файл, который
#  начинается с чистого листа, пока бот всё помнит. Запись стирается своими
#  кнопками на экране «👤 Разговор с ботом».
#
#  Модуль ТИХИЙ: любая ошибка записи глушится и наружу не выходит. Потерять
#  ответ человеку из-за лога нельзя.
# ─────────────────────────────────────────────

import logging
import os
import re
import threading
from datetime import datetime

from logging_setup import LOG_DIR

logger = logging.getLogger(__name__)

_ICON = "👤"

# Папка внутри logs — как у chat_log: все логи в одном месте, и .gitignore,
# который целиком закрывает logs/, накрывает её без единой правки.
DIR = os.path.join(LOG_DIR, "dialog")

_NAME_PREFIX = "dialog "
_NAME_SUFFIX = ".log"

# Потолок записи ОДНОГО человека. Дорос — самое старое срезается сверху
# (см. _trim), запись продолжается. Потолок нужен не ради места на диске, а
# ради кнопки «💾 Скачать»: файл больше 50 МБ Telegram не примет вовсе.
MAX_BYTES = 2 * 1024 * 1024

# Сколько остаётся после обрезки: последняя половина потолка. Резать «впритык»
# нельзя — иначе обрезка запускалась бы почти на каждое обращение.
_KEEP_BYTES = MAX_BYTES // 2

# Начало строки-шапки ОДНОГО обращения. По нему обращения СЧИТАЮТСЯ для экрана,
# поэтому оно обязано быть уникальным: подзаголовки внутри записи начинаются
# с «── », разделитель обращений — сплошная линия из «═».
_ASK_MARK = "🕐 "

# Запись идёт из рабочих потоков (модель зовут через run_in_executor), поэтому
# без замка два обращения могли бы перемешать строки в одном файле.
_lock = threading.Lock()

_KIND_RU = {"text": "текст", "photo": "фото", "voice": "голосовое", "video": "видео"}


# ─── пути ───────────────────────────────────────────────────────────

def path_for(user_id: int) -> str:
    """Файл записи одного человека. Имя — по id: имена и ники меняются."""
    return os.path.join(DIR, f"{_NAME_PREFIX}{int(user_id)}{_NAME_SUFFIX}")


def _user_id_of(name: str) -> int | None:
    """id из имени файла или None, если имя не наше."""
    m = re.fullmatch(re.escape(_NAME_PREFIX) + r"(-?\d+)" + re.escape(_NAME_SUFFIX), name)
    return int(m.group(1)) if m else None


# ─── запись ─────────────────────────────────────────────────────────

def _write(user_id: int, text: str) -> None:
    """Дописать кусок в запись человека, заведя её при необходимости. Тихая."""
    try:
        path = path_for(user_id)
        with _lock:
            os.makedirs(DIR, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)
                size = f.tell()
            if size > MAX_BYTES:
                _trim(path)
    except Exception as e:
        logger.debug("%s Не удалось записать лог обращения: %s", _ICON, e)


def _trim(path: str) -> None:
    """
    Срезает начало записи, оставляя последние _KEEP_BYTES.

    Файл читается КУСКОМ С КОНЦА, а не целиком: он может весить мегабайты.
    Срез делается по границе обращения (строка-шапка), чтобы запись не
    начиналась с середины чужого запроса; не нашлось шапки — по границе
    строки. Зовётся под общим замком из _write, своего не берёт.
    """
    tmp = path + ".tmp"
    try:
        with open(path, "rb") as src:
            src.seek(-_KEEP_BYTES, os.SEEK_END)
            tail = src.read()
        text = tail.decode("utf-8", errors="ignore")
        cut = text.find("\n" + "═" * 60 + "\n")
        text = text[cut + 1:] if cut >= 0 else text[text.find("\n") + 1:]
        with open(tmp, "w", encoding="utf-8") as dst:
            dst.write("…начало записи срезано: она переросла потолок "
                      f"{MAX_BYTES // (1024 * 1024)} МБ…\n")
            dst.write(text)
        os.replace(tmp, path)
        logger.info("%s Запись обращений %s подрезана до потолка",
                    _ICON, os.path.basename(path))
    except OSError as e:
        logger.debug("%s Не удалось подрезать запись обращений: %s", _ICON, e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _where(chat_id: int, user_id: int) -> str:
    """«личка» или «группа «Название»» — где человек написал."""
    if chat_id == user_id:
        return "личка"
    try:
        from database.history import get_known_chats
        for c in get_known_chats():
            if c["chat_id"] == chat_id and (c.get("title") or "").strip():
                return f"группа «{c['title'].strip()}»"
    except Exception as e:
        logger.debug("%s Не удалось узнать название группы: %s", _ICON, e)
    return f"группа {chat_id}"


def note_ask(user_id: int, chat_id: int, kind: str, text: str) -> None:
    """Шапка нового обращения — всё остальное пишется под ней."""
    # Повод в одну строку и коротко: полный текст всё равно уйдёт ниже,
    # в стенограмме запроса.
    snippet = " ".join((text or "").split())[:120] or "—"
    _write(user_id,
           "\n" + "═" * 60 + "\n"
           + f"{_ASK_MARK}{datetime.now().strftime('%d.%m %H:%M:%S')} · "
             f"{_where(chat_id, user_id)} · {_KIND_RU.get(kind, kind)}: {snippet}\n")


def note_request(user_id: int, model: str, prompt: str) -> None:
    """Всё, что уходит модели: характер, база знаний, справки и вся история."""
    _write(user_id, f"── УХОДИТ МОДЕЛИ ({model}, {len(prompt)} симв.) ──\n{prompt}\n")


def note_answer(user_id: int, model: str, seconds: float, answer: str,
                total: float = 0.0) -> None:
    """
    Ответ модели человеку.

    seconds — сколько работала САМА ответившая модель; total — весь перебор
    очереди подстраховки, если по дороге были отказы (0 — отказов не было).
    ⚠️ Слова пометки — те же, что у секундомера в общем логе
    (services/gemini.py::_took) и в журнале разговоров
    (services/chat_log.py::note_answer): меняешь здесь — поменяй и там.

    ⚠️ РАЗМЫШЛЕНИЯ СРЕЗАЮТСЯ — как в chat_log и по той же причине: блок
    <thought> бывает длиннее самой реплики в разы, а читают запись ради того,
    ЧТО бот ответил. Срез — общим strip_thoughts, своего выражения не заводить.
    """
    from utils_format import strip_thoughts
    took = f", {seconds:.1f} с" if seconds else ""
    if total:
        took += f", всего с отказами {total:.1f} с"
    _write(user_id, f"── ВЕРНУЛА МОДЕЛЬ ({model}{took}) ──\n"
                    f"{strip_thoughts(answer) or '(пусто — весь ответ был размышлением)'}\n")


def note_outcome(user_id: int, text: str) -> None:
    """Чем кончилось обращение, когда ответа модели не было (отказ цепочки)."""
    _write(user_id, f"── ИТОГ: {text} ──\n")


# ─── цифры и список для экранов ─────────────────────────────────────

def _count_asks(path: str) -> int:
    """Сколько в записи обращений. Файл читается построчно — он большой."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.startswith(_ASK_MARK))
    except OSError:
        return 0


def _first_ask_time(path: str) -> str:
    """«14.09 в 10:02» — время первого обращения в записи ("" — не нашлось)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(_ASK_MARK):
                    stamp = line[len(_ASK_MARK):].split(" · ")[0].strip()
                    day, _, clock = stamp.partition(" ")
                    return f"{day} в {clock[:5]}" if clock else day
    except OSError:
        pass
    return ""


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def stats(user_id: int) -> dict:
    """Сводка по записи одного человека для экрана."""
    path = path_for(user_id)
    if not os.path.exists(path):
        return {"user_id": user_id, "path": path, "name": os.path.basename(path),
                "exists": False, "size": 0, "asks": 0, "first": "", "last": 0.0}
    return {
        "user_id": user_id,
        "path": path,
        "name": os.path.basename(path),
        "exists": True,
        "size": _size(path),
        "asks": _count_asks(path),
        "first": _first_ask_time(path),
        "last": _mtime(path),
    }


def list_records(limit: int | None = None) -> list[dict]:
    """
    Все записи, самые свежие сверху: [{user_id, path, size, asks, last}].

    Число обращений считается чтением файла — поэтому `limit` режет список
    ДО подсчёта: считать сотню файлов ради двадцати кнопок незачем.
    """
    try:
        names = os.listdir(DIR)
    except OSError:
        return []

    found = []
    for name in names:
        uid = _user_id_of(name)
        if uid is None:
            continue
        path = os.path.join(DIR, name)
        found.append((_mtime(path), uid, path))

    found.sort(reverse=True)
    if limit is not None:
        found = found[:limit]
    return [{"user_id": uid, "path": path, "size": _size(path),
             "asks": _count_asks(path), "last": last}
            for last, uid, path in found]


def total_size() -> int:
    """Сколько весят все записи вместе — строка для экрана списка."""
    return sum(r["size"] for r in list_records())


def count_records() -> int:
    """Сколько людей имеют запись."""
    try:
        return sum(1 for name in os.listdir(DIR) if _user_id_of(name) is not None)
    except OSError:
        return 0


# ─── очистка ────────────────────────────────────────────────────────

def clear(user_id: int) -> bool:
    """Стирает запись одного человека. True — было что стирать."""
    path = path_for(user_id)
    try:
        with _lock:
            os.remove(path)
    except OSError:
        return False
    logger.info("%s Запись обращений человека %s стёрта", _ICON, user_id)
    return True


def clear_all() -> int:
    """Стирает записи всех. Возвращает, сколько файлов убрано."""
    removed = 0
    for record in list_records():
        try:
            with _lock:
                os.remove(record["path"])
            removed += 1
        except OSError:
            continue
    logger.info("%s Записи обращений стёрты целиком: файлов %d", _ICON, removed)
    return removed
