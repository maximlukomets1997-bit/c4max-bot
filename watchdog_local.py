# ───────────────────────────────────────────────
#  watchdog_local.py — МЕСТНЫЙ сторож бота (2026-07-27)
#
#  Запускается ЗАДАНИЕМ ПЛАНИРОВЩИКА Windows каждые 2 минуты, отдельным
#  процессом. Смотрит, жив ли бот, и если нет — пишет владельцу в Telegram.
#
#  ЧТО ЛОВИТ: «бот упал, завис или потерял Telegram, а компьютер и интернет
#  живы» — за 2–4 минуты.
#  ЧЕГО НЕ ЛОВИТ: выключенный компьютер и пропавший интернет. Он живёт на том
#  же компьютере и умирает вместе с ним — для этих случаев есть ВНЕШНИЙ сторож
#  (healthchecks.io, см. jobs.py::watchdog_loop). Один другого не заменяет.
#
#  ⚠️ НАМЕРЕННО НЕ ЗАВИСИТ ОТ КОДА БОТА: не импортирует ни handlers, ни
#  services, ни database, не открывает базу (её держит живой бот). Сторож,
#  который ломается вместе с тем, за кем следит, бесполезен. Единственное
#  исключение — config.py: из него берутся ТОКЕН и ID владельца, чтобы не
#  заводить их вторую копию, которая разъедется с первой. config.py — файл
#  голых констант, ломаться в нём нечему; а если он всё-таки сломан, бот не
#  запустится вовсе, и это видно без сторожа.
#
#  ЖИВ ли бот — проверяется ДВУМЯ признаками сразу:
#    1) процесс из bot.pid существует (и это python.exe);
#    2) метка живости bot.alive свежее WATCHDOG_STALE_SEC — её обновляет сам
#       бот после каждой удачной проверки связи с Telegram.
#  Второй признак и отличает ЗАВИСШЕГО бота от работающего: процесс висит
#  в списке задач, а делать ничего не делает.
#
#  ЛОЖНЫЕ ТРЕВОГИ. Тревога поднимается только если плохо ДВА РАЗА ПОДРЯД
#  (то есть ~4 минуты). Иначе кнопка «🔄 ПЕРЕЗАПУСТИТЬ БОТА» будила бы её
#  каждый раз: между смертью старой копии и запуском новой есть доли секунды,
#  когда процесса нет. Память между запусками — файл watchdog.state.
#
#  ЗАПУСК ВРУЧНУЮ (проверить, что всё настроено):
#      python watchdog_local.py --test
#  пришлёт пробное сообщение в Telegram и ничего больше не сделает.
# ─────────────────────────────────────────────

import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(BASE_DIR, "bot.pid")
STATE_FILE = os.path.join(BASE_DIR, "watchdog.state")

# ⚠️ .env ЧИТАЕМ ПО ПОЛНОМУ ПУТИ, И ДО import config — иначе сторож немой.
# config.py зовёт load_dotenv() без пути, а тот ищет .env ОТ РАБОЧЕЙ ПАПКИ.
# Задание планировщика запускается из своей папки (обычно C:\Windows\System32),
# .env там нет — и TELEGRAM_TOKEN оказывался ПУСТЫМ. Сторож при этом не падал,
# а молча ничего не отправлял: худший вид поломки — тот, которого не видно.
# Проверено запуском из C:\ (2026-07-27): без этих двух строк токен пустой.
# Рабочую папку в задании тоже задаём, но полагаться на неё нельзя.
sys.path.insert(0, BASE_DIR)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
except Exception:
    pass  # dotenv нет — попробуем работать на том, что уже есть в окружении

# Сколько раз подряд должно быть плохо, прежде чем поднимать тревогу.
# ДВА, а не одна: между смертью старой копии бота и запуском новой (кнопка
# «🔄 ПЕРЕЗАПУСТИТЬ БОТА») есть несколько секунд без файла bot.pid. Две плохие
# проверки подряд разнесены на CHECK_STEP_SEC, поэтому короткий перезапуск
# физически не может дать две подряд — а настоящая смерть даёт.
BAD_CHECKS_BEFORE_ALARM = 2

# Шаг между проверками ВНУТРИ одного запуска задания и общая длина запуска
# (2026-07-27, решение Максима «быстро»). Окно чуть короче минуты — задание
# запускается раз в минуту, и предыдущий запуск обязан успеть завершиться,
# иначе планировщик пропустит следующий (MultipleInstances = IgnoreNew).
CHECK_STEP_SEC = 20
CHECK_WINDOW_SEC = 50


def _log(message: str) -> None:
    """
    Пишет строку в собственный файл сторожа. В логи бота НЕ пишем: у них своя
    жизнь (папка logs всегда содержит ровно два файла — текущую сессию и архив),
    и посторонний файл сломал бы этот порядок.
    Печать в консоль тоже есть — для запуска руками.
    """
    line = "%s | %s" % (time.strftime("%d.%m %H:%M:%S"), message)
    try:
        print(line)
    except Exception:
        pass  # у задания планировщика консоли нет — не беда
    try:
        with open(os.path.join(BASE_DIR, "watchdog.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── состояние между запусками ──────────────────────────────────────

def _read_state() -> dict:
    """Память сторожа. Файла нет или он испорчен — начинаем с чистого листа."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {"bad_count": 0, "alarm_sent": False}


def _write_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        _log("не удалось сохранить состояние: %s" % e)


# ─── проверка «жив ли бот» ──────────────────────────────────────────

def _process_alive(pid: int) -> bool:
    """
    Есть ли в системе процесс python с таким номером.

    ⚠️ ЗДЕСЬ НЕЛЬЗЯ ЗАПУСКАТЬ ВНЕШНИЕ ПРОГРАММЫ (2026-07-27, жалоба Максима
    «постоянно выскакивает окно терминала»). Сначала тут был вызов `tasklist`
    через subprocess — и он МИГАЛ ОКНОМ: задание планировщика работает под
    `pythonw.exe`, у которого своей консоли нет, поэтому Windows заводила новое
    окно под каждый запуск консольной программы. Пока проверка была раз в две
    минуты, это мозолило глаза 30 раз в час; после ускорения (3 проверки
    в минуту) стало 180 раз в час. Теперь спрашиваем систему напрямую через
    ctypes — окну взяться неоткуда, и заодно не плодятся процессы.
    Захочешь вернуть subprocess — обязателен `creationflags=CREATE_NO_WINDOW`,
    иначе окно вернётся вместе с ним.

    ⚠️ os.kill(pid, 0) использовать НЕЛЬЗЯ: в Windows os.kill не умеет
    «сигнал 0» и вместо проверки ЗАВЕРШАЕТ процесс — сторож убил бы бота,
    за которым следит.

    Имя программы сверяем (должно начинаться на «python») на случай, когда
    номер процесса уже переиспользован чем-то посторонним.
    """
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False          # процесса с таким номером нет
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            if not ok:
                # Процесс есть, но имя не отдали (редко, права). Раз он
                # существует — считаем живым: не паниковать без оснований.
                return True
            name = os.path.basename(buf.value).lower()
            return name.startswith("python")
        finally:
            kernel32.CloseHandle(handle)
    except Exception as e:
        # Не смогли проверить — считаем, что бот ЖИВ. Безопасный ответ:
        # ложная тишина лучше ложной паники (тот же принцип, что у
        # _is_protected в антиспаме: не уверен — не наказывай).
        _log("не удалось проверить процесс %d (%s) — считаю, что бот жив" % (pid, e))
        return True


def _check_bot() -> tuple[bool, str]:
    """
    Жив ли бот. Возвращает (жив, причина) — причина уходит в текст тревоги,
    чтобы Максиму было понятно, что именно случилось.
    """
    from config import WATCHDOG_ALIVE_FILE, WATCHDOG_STALE_SEC

    # 1) Процесс
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except Exception:
        return False, "процесс бота не запущен (нет файла bot.pid)"

    if not _process_alive(pid):
        return False, "процесс бота (%d) исчез из списка задач" % pid

    # 2) Метка живости
    try:
        age = time.time() - os.path.getmtime(WATCHDOG_ALIVE_FILE)
    except Exception:
        # Файла нет: бот только что запустился и ещё не успел его написать,
        # либо запущена старая версия кода. Процесс при этом жив — не паникуем.
        return True, ""

    if age > WATCHDOG_STALE_SEC:
        return False, ("процесс бота жив, но он завис или потерял Telegram: "
                       "последний признак жизни %d мин назад" % int(age // 60))

    return True, ""


# ─── отправка в Telegram ────────────────────────────────────────────

def _notify(text: str) -> bool:
    """
    Пишет владельцу в личку напрямую через Telegram Bot API — от имени бота,
    но НЕ через его код: бот в этот момент, скорее всего, мёртв.
    Возвращает True, если хотя бы одному владельцу доставлено.
    """
    import requests
    from config import TELEGRAM_TOKEN, ADMIN_IDS

    if not TELEGRAM_TOKEN:
        _log("нет TELEGRAM_TOKEN — некому и нечем отправлять")
        return False

    url = "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_TOKEN
    delivered = False
    for chat_id in ADMIN_IDS:
        try:
            response = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=20)
            response.raise_for_status()
            delivered = True
        except Exception as e:
            _log("не удалось отправить владельцу %s: %s" % (chat_id, e))
    return delivered


# ─── главный проход ─────────────────────────────────────────────────

def main() -> None:
    if "--test" in sys.argv:
        alive, reason = _check_bot()
        status = "бот жив" if alive else ("бот НЕ отвечает: " + reason)
        ok = _notify("🐕 Проверка сторожа.\n\nСторож настроен и умеет тебе писать.\n"
                     "Сейчас: %s" % status)
        _log("проверка вручную: %s, сообщение %s" % (status, "доставлено" if ok else "НЕ доставлено"))
        return

    # ⚠️ ПОЧЕМУ НЕСКОЛЬКО ПРОВЕРОК ЗА ОДИН ЗАПУСК (2026-07-27, решение
    # Максима «быстро»): планировщик Windows не умеет запускать задание чаще
    # РАЗА В МИНУТУ — это его предел, и он же был бы пределом скорости реакции.
    # Поэтому одно задание, запускаясь раз в минуту, само проверяет несколько
    # раз с шагом CHECK_STEP_SEC и завершается до следующего запуска.
    # Постоянно висящий процесс не заводим намеренно: он сам может умереть,
    # и сторожа пришлось бы сторожить. Здесь же за живучесть отвечает
    # планировщик — он поднимет задание заново в любом случае.
    deadline = time.monotonic() + CHECK_WINDOW_SEC
    while True:
        _one_check()
        if time.monotonic() + CHECK_STEP_SEC > deadline:
            return
        time.sleep(CHECK_STEP_SEC)


def _one_check() -> None:
    """Одна проверка «жив ли бот» со всеми последствиями (тревога/отбой)."""
    state = _read_state()
    alive, reason = _check_bot()

    if alive:
        if state.get("alarm_sent"):
            _notify("✅ Бот снова работает.\n\nСвязь восстановлена, отвечать он может.")
            _log("бот восстановился — отправлено уведомление")
        _write_state({"bad_count": 0, "alarm_sent": False})
        return

    # Плохо. Считаем, сколько раз подряд.
    bad = int(state.get("bad_count", 0)) + 1
    if state.get("alarm_sent"):
        # Уже жаловались об этом происшествии — молчим до восстановления.
        _write_state({"bad_count": bad, "alarm_sent": True})
        return

    if bad < BAD_CHECKS_BEFORE_ALARM:
        # Первый плохой заход: возможно, идёт перезапуск кнопкой. Ждём следующего.
        _log("плохая проверка (%d из %d): %s" % (bad, BAD_CHECKS_BEFORE_ALARM, reason))
        _write_state({"bad_count": bad, "alarm_sent": False})
        return

    sent = _notify("🔴 БОТ НЕ ОТВЕЧАЕТ\n\n%s\n\nКомпьютер при этом работает и интернет есть — "
                   "иначе это сообщение бы не пришло.\nПроверь окно бота и запусти его заново." % reason)
    _log("ТРЕВОГА: %s (сообщение %s)" % (reason, "доставлено" if sent else "НЕ доставлено"))
    # alarm_sent ставим только при удачной доставке — иначе при первом же
    # восстановлении связи попробуем ещё раз, а не промолчим навсегда.
    _write_state({"bad_count": bad, "alarm_sent": bool(sent)})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Сторож обязан быть тихим: его падение не должно ничего ломать,
        # но след в собственном логе оставить надо.
        _log("сторож упал: %s" % e)
