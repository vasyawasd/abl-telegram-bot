import os
import sys
import re
import json
import time
import socket
import requests
import threading
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Настраиваем UTF-8 для вывода эмодзи на любых платформах (Windows cp1251 / Termux)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def safe_print(*args, **kwargs):
    # Метка времени в каждой строке лога — чтобы сопоставлять сбои с реальными событиями
    args = (datetime.now().strftime("[%Y-%m-%d %H:%M:%S]"),) + args
    kwargs.setdefault("flush", True)
    try:
        print(*args, **kwargs)
    except Exception:
        try:
            enc = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
            cleaned = [str(a).encode(enc, errors="replace").decode(enc, errors="replace") for a in args]
            print(*cleaned, **kwargs)
        except Exception:
            pass

MOSCOW_TZ = timezone(timedelta(hours=3))

# Таблица нормализации визуально одинаковых букв (кириллица <-> латиница)
HOMOGLYPHS = str.maketrans({
    'a': 'а', 'c': 'с', 'e': 'е', 'k': 'к', 'o': 'о', 'p': 'р', 'x': 'х', 'y': 'у',
    'A': 'а', 'B': 'в', 'C': 'с', 'E': 'е', 'H': 'н', 'K': 'к', 'M': 'м', 'O': 'о',
    'P': 'р', 'T': 'т', 'X': 'х', 'Y': 'у',
    'А': 'а', 'В': 'в', 'С': 'с', 'Е': 'е', 'Н': 'н', 'К': 'к', 'М': 'м', 'О': 'о',
    'Р': 'р', 'Т': 'т', 'Х': 'х', 'У': 'у'
})

TRANSLIT_RU_TO_EN = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
}

def translit_ru(s: str) -> str:
    return "".join(TRANSLIT_RU_TO_EN.get(c, c) for c in (s or "").lower())

def clean_alnum(s: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", (s or "").lower().replace("ё", "е"))

def clean_homo(s: str) -> str:
    return clean_alnum(s).translate(HOMOGLYPHS)

def match_text(query: str, target: str) -> bool:
    """Умное сопоставление названий: нечувствительно к пробелам, регистру, тире,
    визуально одинаковым буквам (кириллица/латиница: X/Х, C/С) и транслитерации."""
    if not query or not target:
        return False
    q_raw, t_raw = query.strip(), target.strip()
    if not q_raw or not t_raw:
        return False

    q_low, t_low = q_raw.lower(), t_raw.lower()
    if q_low == t_low:
        return True

    cq, ct = clean_alnum(q_raw), clean_alnum(t_raw)
    if not cq or not ct:
        return False
    if cq == ct:
        return True

    # Для очень коротких запросов (1-2 символа) требуем точного совпадения слова
    if len(cq) < 3:
        return q_low in t_low.split() or cq in [clean_alnum(w) for w in t_low.split()]

    if q_low in t_low:
        return True

    if cq in ct or (len(ct) >= 4 and ct in cq):
        return True

    hq, ht = clean_homo(q_raw), clean_homo(t_raw)
    if hq in ht or (len(ht) >= 4 and ht in hq):
        return True

    tq, tt = clean_alnum(translit_ru(q_raw)), clean_alnum(translit_ru(t_raw))
    if tq in tt or (len(tt) >= 4 and tt in tq):
        return True

    # Пословная проверка: все значимые слова запроса есть в названии
    words_q = [w for w in re.split(r"[^a-zа-я0-9]+", q_low) if len(w) >= 2 and w not in ("бк", "bc", "team", "команда")]
    if words_q:
        all_in = True
        for w in words_q:
            hw = clean_homo(w)
            tw = clean_alnum(translit_ru(w))
            if hw not in ht and tw not in tt:
                all_in = False
                break
        if all_in:
            return True

    return False

def parse_team_and_division(text: str) -> tuple:
    """Разбирает аргумент на (команда, дивизион). Поддерживает '|' и формат в скобках '(Дивизион)'."""
    text = (text or "").strip()
    if not text:
        return "", ""
    if "|" in text:
        t, d = text.split("|", 1)
        return t.strip(), d.strip()
    m = re.match(r"^(.*?)\s*[\(\[](.*?)[\)\]]$", text)
    if m:
        t, d = m.group(1).strip(), m.group(2).strip()
        if t:
            return t, d
    return text, ""

# Принудительно используем IPv4 для Android / Termux (избегаем Errno 101 Network is unreachable)
try:
    import urllib3.util.connection as urllib3_cn
    urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
except Exception as e:
    safe_print(f"⚠️ Не удалось включить принудительный IPv4: {e}")

# --- Устойчивый HTTP-слой ---
# Отдельная Session на поток: переиспользуем TCP/TLS-соединения (без нового рукопожатия
# и DNS-запроса на каждый вызов) и автоматически повторяем транзиентные сбои.
# Ошибки соединения (запрос не ушёл) повторяются для любых методов — это безопасно.
# Повтор по 5xx / обрыву чтения — только для GET, чтобы не задублировать sendMessage.
_HTTP_LOCAL = threading.local()

def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4, connect=4, read=2, status=3,
        backoff_factor=1.0,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=False,  # 429 обрабатываем сами
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def _session() -> requests.Session:
    s = getattr(_HTTP_LOCAL, "session", None)
    if s is None:
        s = _HTTP_LOCAL.session = _make_session()
    return s

def http_get(url: str, **kwargs) -> requests.Response:
    return _session().get(url, **kwargs)

def http_post(url: str, **kwargs) -> requests.Response:
    return _session().post(url, **kwargs)

# (connect, read): быстрый отказ при недоступности хоста, но терпимость к медленной сети
TG_TIMEOUT = (10, 20)

def load_env_file():
    script_dir = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else "."
    for path in [".env", os.path.join(script_dir, ".env")]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k and k not in os.environ:
                                os.environ[k] = v
                break
            except Exception:
                pass

load_env_file()

def get_bot_token():
    t = os.getenv("BOT_TOKEN", "").strip()
    if not t and sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
        try:
            t = input("Введите токен Telegram-бота (BOT_TOKEN): ").strip()
            if t:
                os.environ["BOT_TOKEN"] = t
                with open(".env", "a", encoding="utf-8") as f:
                    f.write(f"\nBOT_TOKEN={t}\n")
        except Exception:
            pass
    return t or ""

TOKEN = get_bot_token()
LEAGUE_ID = int(os.getenv("LEAGUE_ID", "2"))
DEFAULT_TEAM = (os.getenv("DEFAULT_TEAM") or os.getenv("TEAM_NAME") or "БК Путилково").strip() or "БК Путилково"
CHECK_INTERVAL_SEC = 300
CHATS_FILE = "chats.json"

CONFIG_LOCK = threading.RLock()

def load_chats_config() -> dict:
    with CONFIG_LOCK:
        if os.path.exists(CHATS_FILE):
            try:
                with open(CHATS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                safe_print(f"⚠️ Ошибка чтения {CHATS_FILE}: {e}")
                bak = CHATS_FILE + ".bak"
                if os.path.exists(bak):
                    try:
                        with open(bak, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            safe_print(f"✅ Конфигурация восстановлена из {bak}")
                            return data
                    except Exception as e_bak:
                        safe_print(f"❌ Ошибка резервной копии {bak}: {e_bak}")
                # Сохраняем битый файл для ручного восстановления, чтобы следующее
                # сохранение не затёрло его пустым конфигом без следа
                try:
                    os.replace(CHATS_FILE, f"{CHATS_FILE}.corrupt-{int(time.time())}")
                except Exception:
                    pass
                return {}
        bak = CHATS_FILE + ".bak"
        if os.path.exists(bak):
            try:
                with open(bak, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

def save_chats_config(data: dict):
    with CONFIG_LOCK:
        tmp_path = CHATS_FILE + ".tmp"
        bak_path = CHATS_FILE + ".bak"
        try:
            # 1. Запись во временный файл со сбросом буфера на диск (fsync)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass

            # 2. Резервная копия перед заменой
            if os.path.exists(CHATS_FILE):
                try:
                    with open(CHATS_FILE, "r", encoding="utf-8") as src, open(bak_path, "w", encoding="utf-8") as dst:
                        dst.write(src.read())
                        dst.flush()
                        try:
                            os.fsync(dst.fileno())
                        except Exception:
                            pass
                except Exception:
                    pass

            # 3. Атомарная замена файла
            os.replace(tmp_path, CHATS_FILE)

            # 4. Fsync директории на POSIX системах (Termux/Linux)
            if hasattr(os, "O_DIRECTORY") and hasattr(os, "open") and os.name != "nt":
                try:
                    dir_path = os.path.dirname(os.path.abspath(CHATS_FILE)) or "."
                    dir_fd = os.open(dir_path, os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except Exception:
                    pass
        except Exception as e:
            safe_print(f"⚠️ Ошибка атомарного сохранения {CHATS_FILE}: {e}")

def update_chat(chat_id: int | str, mutator) -> bool:
    """Атомарный read-modify-write одной записи чата под блокировкой.
    Перечитывает свежий конфиг, поэтому не затирает параллельные /set, /unset и disable_chat.
    mutator(info) возвращает True, если запись изменена. Если чата уже нет — ничего не делает."""
    with CONFIG_LOCK:
        cfg = load_chats_config()
        info = cfg.get(str(chat_id))
        if not isinstance(info, dict):
            return False
        if mutator(info):
            save_chats_config(cfg)
            return True
        return False

def disable_chat(chat_id: int | str, reason: str = ""):
    """Безопасно отключает рассылку в чат, если бота удалили или чат деактивирован."""
    def _mut(info):
        if info.get("enabled", True):
            info["enabled"] = False
            return True
        return False
    if update_chat(chat_id, _mut):
        safe_print(f"ℹ️ Чат {chat_id} отключен от рассылки ({reason})")

def is_group_admin(chat_id: str | int, user_id: int) -> bool:
    """Проверяет права пользователя в конкретной группе Telegram."""
    try:
        r = http_get(
            f"https://api.telegram.org/bot{TOKEN}/getChatMember",
            params={"chat_id": chat_id, "user_id": user_id},
            timeout=TG_TIMEOUT
        ).json()
        if r.get("ok"):
            status = r.get("result", {}).get("status")
            return status in ["creator", "administrator"]
    except Exception as e:
        safe_print(f"⚠️ Не удалось проверить права администратора в {chat_id}: {e}")
    return False

# Кэш расписания ABL (Circuit Breaker / Network resilience)
ABL_GAMES_CACHE = []
ABL_GAMES_CACHE_TIME = 0.0
ABL_CACHE_LOCK = threading.Lock()
ABL_FRESH_TTL_SEC = 60          # свежий кэш отдаём без запроса (снимает нагрузку от частых /game)
ABL_STALE_TTL_SEC = 6 * 3600    # устаревший кэш годится как фолбэк при сбое API
ABL_ATTEMPTS = 3
ABL_TIMEOUT = (10, 30)

# API отдаёт игры страницами по 500 (новые первыми), заголовок X-Page-Next-Link.
# Будущие игры всегда на первых страницах; берём несколько, чтобы не потерять
# ближайшие матчи, если в лиге много игр на ближайшие недели.
ABL_MAX_PAGES = 3

def _fetch_abl_page(url: str, headers: dict) -> tuple:
    """Один запрос страницы с повторами. Бросает исключение, если все попытки неудачны."""
    last_err = None
    for attempt in range(1, ABL_ATTEMPTS + 1):
        if threading.current_thread().name == "monitor":
            touch_heartbeat(MONITOR_HEARTBEAT_FILE)
        try:
            r = http_get(url, headers=headers, timeout=ABL_TIMEOUT)
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
            data = r.json()
            if not isinstance(data, list):
                raise ValueError(f"неожиданный формат ответа: {type(data).__name__}")
            return data, r.headers
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < ABL_ATTEMPTS:
                time.sleep(2.0 * attempt)
    raise last_err

def fetch_raw_abl_games(fallback_to_cache: bool = False) -> list | None:
    """Возвращает список игр лиги.
    - Свежий кэш (< ABL_FRESH_TTL_SEC) отдаётся сразу.
    - Сетевые сбои повторяются несколько раз с паузой.
    - При полном отказе API отдаётся последний удачный ответ (если не слишком старый)."""
    global ABL_GAMES_CACHE, ABL_GAMES_CACHE_TIME
    with ABL_CACHE_LOCK:
        if ABL_GAMES_CACHE and time.time() - ABL_GAMES_CACHE_TIME < ABL_FRESH_TTL_SEC:
            return ABL_GAMES_CACHE

    url = f"https://mtgame.ru/api/v1/league/{LEAGUE_ID}/games/"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://ablforpeople.com/"}
    try:
        games = []
        for page in range(1, ABL_MAX_PAGES + 1):
            data, resp_headers = _fetch_abl_page(url if page == 1 else f"{url}?page={page}", headers)
            games.extend(data)
            # Дальше только прошедшие игры — останавливаемся
            if not resp_headers.get("X-Page-Next-Link") or not _page_has_future(data):
                break
        with ABL_CACHE_LOCK:
            ABL_GAMES_CACHE = games
            ABL_GAMES_CACHE_TIME = time.time()
        return games
    except Exception as e:
        safe_print(f"⚠️ Сбой при запросе к ABL API после {ABL_ATTEMPTS} попыток: {e}")

    with ABL_CACHE_LOCK:
        age = time.time() - ABL_GAMES_CACHE_TIME
        if ABL_GAMES_CACHE and (fallback_to_cache or age < ABL_STALE_TTL_SEC):
            safe_print(f"ℹ️ Использован кэш ABL API ({int(age // 60)} мин.) из-за сбоя сети")
            return ABL_GAMES_CACHE
    return None

def _page_has_future(games: list) -> bool:
    now = datetime.now(timezone.utc)
    for g in games:
        dt = parse_game_dt(g) if isinstance(g, dict) else None
        if dt and dt >= now:
            return True
    return False

def parse_game_dt(g: dict) -> datetime | None:
    dt_str = g.get("datetime")
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def format_game_card(g: dict, is_left: bool, title: str = "Расписание игры") -> str:
    """Форматирует карточку игры с залом, формой и московским временем."""
    t1 = (g.get("team") or {}).get("name") or ""
    t2 = (g.get("competitor_team") or {}).get("name") or ""
    tour = g.get("tournament_tour")
    tour_str = f" ({tour} тур)" if tour else ""
    division = (g.get("tournament") or {}).get("name") or "ABL"
    court = (g.get("tournament_court") or {}).get("name") or ""
    loc = g.get("location") or ""
    place = f"{court} ({loc})" if court and court != loc else (court or loc or "Уточняется")
    form = "⚪️ СВЕТЛАЯ" if is_left else "⚫️ ЧЁРНАЯ"

    dt_str = g.get("datetime")
    msk_str = "Уточняется"
    if dt_str:
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            msk_str = dt.astimezone(MOSCOW_TZ).strftime('%d.%m.%Y в %H:%M')
        except Exception:
            pass

    return (
        f"🏀 *{title}{tour_str}*\n\n"
        f"⚔️ *Матч:* {t1} — {t2}\n"
        f"🏆 *Дивизион:* {division}\n"
        f"📅 *Когда:* {msk_str} (МСК)\n"
        f"📍 *Зал:* {place}\n"
        f"👕 *Форма:* {form}"
    )

def fetch_next_game(team_name: str, division_name: str = ""):
    # ponytail: старые аргументы принимаем, но следим за командой между этапами сезона.
    parsed_team, _ = parse_team_and_division(team_name)
    team_name = parsed_team or team_name

    if not team_name:
        return None, "⚠️ Пожалуйста, укажите название команды: `/game НазваниеКоманды`"

    resp = fetch_raw_abl_games(fallback_to_cache=False)
    if resp is None:
        return None, "⚠️ Не удалось получить данные о расписании ABL (ошибка сети/сервера). Попробуйте позже."

    now = datetime.now(timezone.utc)
    upcoming = []

    for g in resp:
        if not isinstance(g, dict):
            continue
        if g.get("status") != "open":
            continue
        t1 = (g.get("team") or {}).get("name") or ""
        t2 = (g.get("competitor_team") or {}).get("name") or ""
        is_left = match_text(team_name, t1)
        is_right = match_text(team_name, t2)

        if is_left or is_right:
            dt_str = g.get("datetime")
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt >= now:
                        upcoming.append((dt, g, is_left, t1, t2))
                except Exception:
                    pass

    if not upcoming:
        return None, f"Пока нет открытых назначенных игр для команды *{team_name}*."

    upcoming.sort(key=lambda x: x[0])
    dt, g, is_left, t1, t2 = upcoming[0]
    msk_dt = dt.astimezone(MOSCOW_TZ)

    tour = g.get("tournament_tour")
    tour_str = f" ({tour} тур)" if tour else ""
    division = (g.get("tournament") or {}).get("name") or "ABL"
    court = (g.get("tournament_court") or {}).get("name") or ""
    loc = g.get("location") or ""
    place = f"{court} ({loc})" if court and court != loc else (court or loc or "Уточняется")

    # Слева = светлая, справа = черная
    form = "⚪️ СВЕТЛАЯ" if is_left else "⚫️ ЧЁРНАЯ"

    text = (
        f"🏀 *Расписание игры{tour_str}*\n\n"
        f"⚔️ *Матч:* {t1} — {t2}\n"
        f"🏆 *Дивизион:* {division}\n"
        f"📅 *Когда:* {msk_dt.strftime('%d.%m.%Y в %H:%M')} (МСК)\n"
        f"📍 *Зал:* {place}\n"
        f"👕 *Форма:* {form}"
    )
    return g.get("id"), text

def fetch_schedule(team_name: str, division_name: str = "") -> str:
    parsed_team, _ = parse_team_and_division(team_name)
    team_name = parsed_team or team_name

    if not team_name:
        return "⚠️ Пожалуйста, укажите название команды: `/schedule НазваниеКоманды`"

    resp = fetch_raw_abl_games(fallback_to_cache=False)
    if resp is None:
        return "⚠️ Не удалось получить данные о расписании ABL (ошибка сети/сервера). Попробуйте позже."

    now = datetime.now(timezone.utc)
    min_dt = datetime.min.replace(tzinfo=timezone.utc)
    team_games = []

    for g in resp:
        if not isinstance(g, dict):
            continue
        t1 = (g.get("team") or {}).get("name") or ""
        t2 = (g.get("competitor_team") or {}).get("name") or ""
        is_left = match_text(team_name, t1)
        is_right = match_text(team_name, t2)

        if is_left or is_right:
            dt_str = g.get("datetime")
            dt = min_dt
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            team_games.append((dt, g, is_left, t1, t2))

    if not team_games:
        return f"Игр для команды *{team_name}* не найдено."

    team_games.sort(key=lambda x: (x[0] == min_dt, x[0]))
    upcoming = [x for x in team_games if x[1].get("status") == "open" and (x[0] >= now or x[0] == min_dt)]
    finished = [x for x in team_games if x[1].get("status") == "closed"]

    lines = [f"📅 *Календарь матчей ABL для {team_name}:*\n"]
    if upcoming:
        lines.append("*⏳ Ближайшие запланированные матчи:*")
        for dt, g, is_left, t1, t2 in upcoming[:5]:
            msk_dt = dt.astimezone(MOSCOW_TZ)
            opp = t2 if is_left else t1
            form = "⚪️ СВЕТЛАЯ" if is_left else "⚫️ ЧЁРНАЯ"
            court = (g.get("tournament_court") or {}).get("name") or g.get("location") or "Зал уточняется"
            division = (g.get("tournament") or {}).get("name") or "ABL"
            time_display = msk_dt.strftime('%d.%m в %H:%M') if dt > datetime(2000, 1, 1, tzinfo=timezone.utc) else "Дата уточняется"
            lines.append(f"• *{time_display}* — vs {opp} ({form}) | {division} | {court}")

    if finished:
        lines.append("\n*🏁 Завершённые матчи:*")
        for dt, g, is_left, t1, t2 in finished[-5:]:
            s1 = g.get("team_score", 0)
            s2 = g.get("competitor_team_score", 0)
            lines.append(f"• {t1} {s1}:{s2} {t2}")

    return "\n".join(lines)

def _scout_players(team: dict, season: dict, rows: list) -> list:
    """Объединяет турнирные записи одного игрока и считает взвешенные средние за сезон."""
    fields = ("points", "rebounds", "assists", "steals", "blocks",
              "three_points_made", "three_point_attempts", "player_efficiency")
    active_ids = set(season.get("active_player_ids") or [])
    active_ids.update(row["tournament_team_user"]["league_player"]["id"] for row in rows
                      if row["tournament_team_user"].get("active") is not False)
    grouped = {}
    for row in rows:
        if row["team_id"] != team["id"]:
            raise ValueError("Статистика другой команды")
        member = row["tournament_team_user"]
        person = member["league_player"]
        identity = person["id"]
        if identity is None:
            raise ValueError("Неизвестный игрок")
        if identity not in active_ids:
            continue
        games = int(row["games_count"])
        if games < 0 or games != float(row["games_count"]):
            raise ValueError("Некорректное число матчей")
        totals = {key: float(row[key]) for key in fields}
        if any(not math.isfinite(value) or (value < 0 and key != "player_efficiency")
               for key, value in totals.items()):
            raise ValueError("Некорректные показатели игрока")
        p = grouped.setdefault(identity, {"games": 0, "totals": dict.fromkeys(fields, 0.0), "priority": (-1, -1)})
        p["games"] += games
        for key, value in totals.items():
            p["totals"][key] += value
        tournament_id = member["tournament_team"]["tournament_id"]
        priority = (int(tournament_id == season["current_tournament_id"]), int(tournament_id))
        if priority > p["priority"]:
            name = " ".join(str(person.get(key) or "") for key in ("last_name", "first_name")).strip()[:60] or "Игрок"
            number = (member.get("team_user") or {}).get("number")
            p["label"] = f"№{str(number)[:8] if number is not None else '?'} {name}"
            p["priority"] = priority
    players = []
    for p in grouped.values():
        if not all(math.isfinite(value) for value in p["totals"].values()):
            raise ValueError("Некорректная сумма показателей")
        if p["games"]:
            p.update({key: value / p["games"] for key, value in p["totals"].items()})
            players.append(p)
    return players


def format_scout_report(team: dict, season: dict, rows: list, game_date=None, attendance=None) -> str:
    """Короткие карточки за весь сезон и общий рейтинг по среднему КП."""
    players = sorted(_scout_players(team, season, rows),
                     key=lambda p: (-p["points"], -p["games"], p["label"]))

    def average(value):
        return f"{value:.1f}".replace(".", ",")

    season_name = str(season.get("name") or "ABL")[:100].removeprefix("Сезон ")
    header = [f"🔎 *Скаутинг: {str(team['name'])[:100]}*",
              f"🏆 Сезон: {season_name}"]
    if game_date:
        header.append(f"📅 Ближайшая игра: {game_date.astimezone(MOSCOW_TZ).strftime('%d.%m.%Y в %H:%M')} МСК")
    header.append(f"👥 Доезд: *{average(attendance['average'])}* игроков/матч · Матчей *{attendance['games']}*"
                  if attendance else "👥 Доезд: нет данных")
    paragraphs = ["\n".join(header)]
    if not players:
        return "\n\n".join(paragraphs + ["Пока нет индивидуальной статистики завершённых матчей этого сезона."])

    for p in players[:8]:
        made, attempts = p["totals"]["three_points_made"], p["totals"]["three_point_attempts"]
        shots = f"{made:g}/{attempts:g} · {made / attempts:.0%}" if attempts else "нет попыток"
        paragraphs.append(f"*{p['label']}*\n"
                          f"Очки *{average(p['points'])}* · Подборы *{average(p['rebounds'])}*\n"
                          f"Передачи *{average(p['assists'])}* · Перехваты *{average(p['steals'])}*\n"
                          f"Блоки *{average(p['blocks'])}* · Матчей *{p['games']}*\n"
                          f"🎯 3-очковые: {shots}")
    if max(p["games"] for p in players) < 3:
        paragraphs.append("Мало данных: у игроков меньше 3 матчей в этом сезоне.")
    paragraphs.append("*ТОП-5 игроков по КП*")
    for rank, p in enumerate(sorted(players, key=lambda p: -p["player_efficiency"])[:5], 1):
        paragraphs.append(f"{rank}. *{p['label']}*\n"
                          f"Очки *{average(p['points'])}* · Подборы *{average(p['rebounds'])}*\n"
                          f"Передачи *{average(p['assists'])}* · КП *{average(p['player_efficiency'])}*")
    return "\n\n".join(paragraphs)


def _scout_named_team(team_name: str) -> tuple | str:
    """Ищет команду в каталоге последнего сезона, независимо от расписания чата."""
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://ablforpeople.com/"}
    seasons, _ = _fetch_abl_page(f"https://mtgame.ru/api/v1/league/{LEAGUE_ID}/tournament_seasons/?not_closed=1&size=100", headers)
    if not seasons:
        return "В ABL пока нет доступного сезона."
    # API возвращает сезоны от нового к старому; старые тоже могут иметь status=open.
    season_id = int(seasons[0]["id"])
    catalog, _ = _fetch_abl_page(f"https://mtgame.ru/api/v1/tournament_season/{season_id}/teams/?short=1", headers)
    teams = {row["team"]["id"]: row["team"] for row in catalog}
    # Точное каноническое имя важнее частичного совпадения и турнирных псевдонимов:
    # HDXСЕВЕР и HDxSEVER WOMEN — разные команды, даже если у второй псевдоним HDxSEVER.
    exact = [t for t in teams.values() if clean_homo(team_name) == clean_homo(t["name"])
             or clean_alnum(translit_ru(team_name)) == clean_alnum(translit_ru(t["name"]))]
    matches = exact or [t for t in teams.values() if match_text(team_name, t["name"])]
    if not matches:
        return f"Команда {team_name[:100]} не найдена в каталоге текущего сезона ABL. Уточните название."
    if len(matches) > 1:
        names = ", ".join(t["name"][:80] for t in matches[:8])
        return f"Найдено несколько команд: {names}. Укажите более точное название после /scout."
    team = matches[0]
    memberships, _ = _fetch_abl_page(f"https://mtgame.ru/api/v1/team/{int(team['id'])}/tournament_teams/", headers)
    allowed = {row["tournament_id"] for row in catalog if row["team"]["id"] == team["id"]}
    tournaments = [row["tournament"] for row in memberships if row["tournament"]["id"] in allowed]
    if not tournaments:
        return f"Для команды {team['name'][:100]} пока нет турнира в текущем сезоне."
    tournament = max(tournaments, key=lambda t: (t.get("date") or "", t["id"]))
    membership_id = next(row["id"] for row in memberships if row["tournament"]["id"] == tournament["id"])
    return team, {**seasons[0], "current_tournament_id": tournament["id"],
                  "current_tournament_team_id": membership_id}


def _fetch_scout_statistics(team_id: int, season_id: int, group_by: str = "tournament_team_user") -> list:
    rows = []
    # ponytail: 1000 строк с запасом для сезонного состава; при обрыве не выдаём частичный итог.
    for page in range(1, 11):
        response = http_post("https://mtgame.ru/api/v1/basketball_statistic/",
                             json={"group_by": group_by, "tournament_season_ids": str(season_id),
                                   "team_id": team_id, "per_game": "0", "only_closed": "1", "page": page, "size": 100},
                             headers={"User-Agent": "Mozilla/5.0", "Referer": "https://ablforpeople.com/"},
                             timeout=ABL_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("Неожиданный формат статистики")
        rows.extend(data)
        total = int(response.headers.get("X-Page-Count", len(rows)))
        if len(rows) >= total:
            return rows
        if not data:
            raise ValueError("Неполная статистика")
    raise ValueError("Слишком много страниц статистики")


def _scout_attendance(team_id: int, season_id: int, rows: list) -> dict | None:
    if not rows:
        return None
    try:
        teams = _fetch_scout_statistics(team_id, season_id, group_by="team")
        if len(teams) != 1 or teams[0]["team_id"] != team_id:
            raise ValueError("Неизвестная команда в статистике доезда")
        games = int(teams[0]["games_count"])
        counts = [int(row["games_count"]) for row in rows]
        if games <= 0 or any(count < 0 or count > games for count in counts):
            return None
        return {"average": sum(counts) / games, "games": games}
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        # Доступные данные игроков остаются полезны и при сбое командной статистики.
        safe_print("⚠️ Не удалось получить средний доезд ABL")
        return None


def fetch_scout(team_name: str, direct: bool = False) -> str:
    team_name, _ = parse_team_and_division(team_name)
    if not team_name:
        return "Укажите название команды после /scout."
    try:
        dt = None
        if direct:
            target = _scout_named_team(team_name)
            if isinstance(target, str):
                return target
            team, season = target
        else:
            raw = fetch_raw_abl_games()
            if raw is None:
                return "⚠️ Не удалось получить расписание ABL. Попробуйте позже."
            upcoming = _team_upcoming(raw, team_name, "", datetime.now(timezone.utc))
            if not upcoming:
                return f"Пока нет назначенной ближайшей игры для команды {team_name[:100]}."
            dt, game, is_left, _, _ = upcoming[0]
            team = game.get("competitor_team" if is_left else "team") or {}
            tournament = game.get("tournament") or {}
            memberships, _ = _fetch_abl_page(
                f"https://mtgame.ru/api/v1/team/{int(team['id'])}/tournament_teams/",
                {"User-Agent": "Mozilla/5.0", "Referer": "https://ablforpeople.com/"})
            selected = next(row for row in memberships if row["tournament"]["id"] == tournament["id"])
            season = {**selected["tournament"]["season"], "current_tournament_id": tournament["id"],
                      "current_tournament_team_id": selected["id"]}
        team_id, season_id = int(team["id"]), int(season["id"])
        rows = _fetch_scout_statistics(team_id, season_id)
        if rows:
            roster, _ = _fetch_abl_page(
                f"https://mtgame.ru/api/v1/tournament_team/{int(season['current_tournament_team_id'])}/users/",
                {"User-Agent": "Mozilla/5.0", "Referer": "https://ablforpeople.com/"})
            # Текущий состав нужен и для игроков, ещё не сыгравших после перехода этапа.
            season["active_player_ids"] = {member["league_player"]["id"] for member in roster
                                           if member.get("active") is not False}
        attendance = _scout_attendance(team_id, season_id, rows)
        return format_scout_report(team, season, rows, dt, attendance)
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, OverflowError, StopIteration) as e:
        safe_print(f"⚠️ Ошибка получения статистики ABL: {e}")
        return "⚠️ Не удалось получить статистику ABL. Попробуйте позже."


SEND_ATTEMPTS = 3

def send_msg(chat_id: int | str, text: str) -> int | None:
    """Отправка сообщения с ограниченным числом повторов (429, 5xx, сбои сети).
    Возвращает message_id или None, если отправить не удалось."""
    use_markdown = True
    attempt = 0
    while attempt < SEND_ATTEMPTS:
        attempt += 1
        payload = {"chat_id": chat_id, "text": text}
        if use_markdown:
            payload["parse_mode"] = "Markdown"
        try:
            r = http_post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json=payload,
                timeout=TG_TIMEOUT
            )
            if r.status_code == 429:
                try:
                    retry_after = int(r.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    retry_after = 5
                safe_print(f"⚠️ Telegram 429 в send_msg. Ожидание {retry_after + 1} сек...")
                time.sleep(min(retry_after, 60) + 1.0)
                continue

            if isinstance(r.status_code, int) and r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"Telegram HTTP {r.status_code}")

            res = r.json()
            if res.get("ok"):
                return res["result"]["message_id"]

            err_code = res.get("error_code")
            err_desc = (res.get("description") or "").lower()
            if err_code == 403 or "kicked" in err_desc or "deactivated" in err_desc or "chat not found" in err_desc:
                disable_chat(chat_id, err_desc)
                return None

            # Резерв без Markdown, если в тексте были спецсимволы разметки
            if use_markdown:
                use_markdown = False
                attempt -= 1  # смена формата не считается сетевой попыткой
                continue
            safe_print(f"⚠️ Telegram отклонил сообщение в чат {chat_id}: {err_desc}")
            return None
        except (requests.RequestException, ValueError) as e:
            if attempt < SEND_ATTEMPTS:
                time.sleep(2.0 * attempt)
            else:
                safe_print(f"⚠️ Сбой сети при отправке в чат {chat_id} ({attempt} попыток): {e}")
    return None

DEFAULT_POLL_OPTIONS = ["Буду", "Не буду", "Под вопросом"]

def build_poll_question(g: dict, is_left: bool) -> str:
    """Формирует вопрос для опроса посещаемости матча."""
    t1 = (g.get("team") or {}).get("name") or ""
    t2 = (g.get("competitor_team") or {}).get("name") or ""
    opp = t2 if is_left else t1
    dt = parse_game_dt(g)
    msk_str = dt.astimezone(MOSCOW_TZ).strftime('%d.%m в %H:%M') if dt else ""
    date_part = f" ({msk_str})" if msk_str else ""
    opp_part = f" vs {opp}" if opp else ""
    q = f"🏀 Игра{opp_part}{date_part}: будешь?"
    return q[:300]

def send_poll(chat_id: int | str, question: str, options: list[str] = None, reply_to_message_id: int | None = None) -> int | None:
    """Отправляет публичный опрос (неанонимный) в чат с повторами при сбоях (429, 5xx, сбои сети).
    Возвращает message_id или None."""
    if options is None:
        options = DEFAULT_POLL_OPTIONS
    attempt = 0
    payload = {
        "chat_id": chat_id,
        "question": question[:300],
        "options": list(options),
        "is_anonymous": False,
        "allows_multiple_answers": False,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = int(reply_to_message_id)

    while attempt < SEND_ATTEMPTS:
        attempt += 1
        try:
            r = http_post(
                f"https://api.telegram.org/bot{TOKEN}/sendPoll",
                json=payload,
                timeout=TG_TIMEOUT
            )
            if r.status_code == 429:
                try:
                    retry_after = int(r.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    retry_after = 5
                safe_print(f"⚠️ Telegram 429 в send_poll. Ожидание {retry_after + 1} сек...")
                time.sleep(min(retry_after, 60) + 1.0)
                continue

            if isinstance(r.status_code, int) and r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"Telegram HTTP {r.status_code}")

            res = r.json()
            if res.get("ok"):
                return res["result"]["message_id"]

            err_code = res.get("error_code")
            err_desc = (res.get("description") or "").lower()
            if err_code == 403 or "kicked" in err_desc or "deactivated" in err_desc or "chat not found" in err_desc:
                disable_chat(chat_id, err_desc)
                return None

            if "reply_to_message_id" in payload:
                del payload["reply_to_message_id"]
                attempt -= 1
                continue

            safe_print(f"⚠️ Telegram отклонил опрос в чат {chat_id}: {err_desc}")
            return None
        except (requests.RequestException, ValueError) as e:
            if attempt < SEND_ATTEMPTS:
                time.sleep(2.0 * attempt)
            else:
                safe_print(f"⚠️ Сбой сети при отправке опроса в чат {chat_id} ({attempt} попыток): {e}")
    return None

def delete_msg(chat_id: int | str, message_id: int):
    try:
        r = http_post(
            f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": int(message_id)},
            timeout=TG_TIMEOUT
        ).json()
        if not r.get("ok"):
            safe_print(f"⚠️ Ошибка удаления {message_id}: {r.get('description')}")
    except Exception as e:
        safe_print(f"⚠️ Сбой сети при удалении {message_id}: {e}")

def delete_later(chat_id: int | str, message_ids: list, delay: float = 3.0):
    """Удаляет список сообщений через delay секунд (в фоне, чтобы не блокировать бота)."""
    def _worker():
        if delay > 0:
            time.sleep(delay)
        valid_ids = []
        for m in message_ids:
            if m:
                try:
                    valid_ids.append(int(m))
                except (ValueError, TypeError):
                    pass
        if not valid_ids:
            return

        valid_ids = sorted(list(set(valid_ids)))

        # 1. Пробуем удалить всё разом через deleteMessages (Bot API 7.0+)
        try:
            r = http_post(
                f"https://api.telegram.org/bot{TOKEN}/deleteMessages",
                json={"chat_id": chat_id, "message_ids": valid_ids},
                timeout=TG_TIMEOUT
            ).json()
            if r.get("ok"):
                return
        except Exception:
            pass

        # 2. Поштучное удаление с небольшой паузой
        for i, mid in enumerate(valid_ids):
            delete_msg(chat_id, mid)
            if i < len(valid_ids) - 1:
                time.sleep(0.3)
    threading.Thread(target=_worker, daemon=True).start()


def _mark_sent(chat_id: str, team: str, keys: list, last_game_id=None, announcement_message_id=None) -> bool:
    """Фиксирует отправку сразу после неё, перечитывая свежий конфиг.
    Если за время отправки чат отвязали или сменили команду — ничего не пишем."""
    def _mut(info):
        if info.get("team") != team or info.get("enabled") is False:
            return False
        stages = info.setdefault("reminded_stages", [])
        # Сохраняем старую отметку до замены last_game_id следующим матчем.
        old_gid = info.get("last_game_id")
        if old_gid is not None and f"{old_gid}:announced" not in stages:
            stages.append(f"{old_gid}:announced")
        for k in keys:
            if k not in stages:
                stages.append(k)
        if last_game_id is not None:
            info["last_game_id"] = last_game_id
        if announcement_message_id is not None:
            info.setdefault("announcement_message_ids", {})[str(last_game_id)] = announcement_message_id
        return True
    return update_chat(chat_id, _mut)

def _team_upcoming(raw_games: list, team: str, division: str, now_utc: datetime) -> list:
    upcoming = []
    for g in raw_games:
        if not isinstance(g, dict) or g.get("status") != "open":
            continue
        t1 = (g.get("team") or {}).get("name") or ""
        t2 = (g.get("competitor_team") or {}).get("name") or ""
        is_left = match_text(team, t1)
        is_right = match_text(team, t2)
        if is_left or is_right:
            dt = parse_game_dt(g)
            if dt and dt >= now_utc:
                upcoming.append((dt, g, is_left, t1, t2))
    upcoming.sort(key=lambda x: x[0])
    return upcoming

MONITOR_RETRY_SEC = 60  # повторная проверка после сбоя API

def monitor_tick(now_utc: datetime | None = None) -> bool:
    """Одна итерация мониторинга. Возвращает False, если данные ABL получить не удалось."""
    now_utc = now_utc or datetime.now(timezone.utc)
    raw_games = fetch_raw_abl_games(fallback_to_cache=True)
    if not raw_games:
        return False

    cfg = load_chats_config()
    for s_chat_id, info in cfg.items():
        touch_heartbeat(MONITOR_HEARTBEAT_FILE)
        try:
            if not isinstance(info, dict) or info.get("enabled") is False:
                continue
            team = info.get("team")
            if not team:
                continue
            division = info.get("division", "")
            stages = set(info.get("reminded_stages") or [])
            if info.get("last_game_id") is not None:
                stages.add(f"{info['last_game_id']}:announced")
            announcement_ids = info.get("announcement_message_ids") or {}
            # ponytail: храним отметки без срока; чистка требует подтверждённого архива игр.
            upcoming = _team_upcoming(raw_games, team, division, now_utc)
            for _, g, is_left, _, _ in upcoming:
                gid = g.get("id")
                if not gid:
                    continue
                ann_key = f"{gid}:announced"
                ann_mid = announcement_ids.get(str(gid))
                if ann_key not in stages:
                    card = format_game_card(g, is_left, "Новое расписание матча!")
                    ann_mid = send_msg(s_chat_id, f"⚡️ *Новое расписание матча!*\n\n{card}")
                    if not ann_mid:
                        continue
                    if not _mark_sent(s_chat_id, team, [ann_key], last_game_id=gid,
                                      announcement_message_id=ann_mid):
                        break
                    stages.add(ann_key)
                    announcement_ids[str(gid)] = ann_mid

                # Неудачный опрос повторяется отдельно, включая запуск после перезапуска.
                poll_key = f"{gid}:poll"
                if poll_key not in stages:
                    poll_q = build_poll_question(g, is_left)
                    if send_poll(s_chat_id, poll_q, reply_to_message_id=ann_mid):
                        if not _mark_sent(s_chat_id, team, [poll_key]):
                            break
                        stages.add(poll_key)
        except Exception as e:
            # Сбой в одном чате не должен останавливать рассылку остальным
            safe_print(f"⚠️ Ошибка мониторинга чата {s_chat_id}: {e}")
    return True

def auto_monitor_loop():
    """Публикует один анонс с опросом для каждого нового назначенного матча."""
    time.sleep(5)
    while True:
        touch_heartbeat(MONITOR_HEARTBEAT_FILE)
        ok = False
        try:
            ok = monitor_tick()
        except Exception as e:
            safe_print(f"⚠️ Ошибка в auto_monitor_loop: {e}")
        time.sleep(CHECK_INTERVAL_SEC if ok else MONITOR_RETRY_SEC)


import http.server
import socketserver

def start_render_web_server():
    """Фоновый HTTP-сервер для бесплатного тарифа Render Web Service."""
    port = int(os.getenv("PORT", "10000"))
    class QuietHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is alive!")
        def log_message(self, format, *args):
            return
    try:
        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("", port), QuietHandler)
        server.serve_forever()
    except Exception as e:
        safe_print(f"ℹ️ Health-check сервер на порту {port} не запущен: {e}")

def process_message(msg: dict):
    """Обрабатывает одно входящее сообщение Telegram."""
    try:
        if not msg or not isinstance(msg, dict) or "text" not in msg:
            return

        raw_text = msg["text"].strip()

        # Игнорируем любые обычные сообщения в чате — реагируем СТРОГО на слэш-команды
        if not raw_text.startswith("/"):
            return

        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not chat_id:
            return

        user_id = (msg.get("from") or {}).get("id", 0)
        chat_type = (msg.get("chat") or {}).get("type", "")
        is_private = (chat_type == "private")
        is_channel = (chat_type == "channel")
        sender_chat_id = (msg.get("sender_chat") or {}).get("id")
        is_anon_admin = bool(sender_chat_id and str(sender_chat_id) == chat_id)

        parts = raw_text.split(None, 1)
        cmd_token = parts[0]
        args = parts[1].strip() if len(parts) > 1 else ""
        cmd = cmd_token.split("@")[0].lower()

        safe_print(f"🤖 [{chat_id}] {raw_text}")
        cfg = load_chats_config()

        # --- 1. СПРАВКА И ПОМОЩЬ ---
        if cmd in ["/start", "/help"]:
            if is_private:
                send_msg(chat_id, (
                    "👋 Привет! Я бот для мониторинга расписания лиги ABL.\n\n"
                    f"🏀 По умолчанию настроен на команду: *{DEFAULT_TEAM}*\n\n"
                    "• Нажмите `/game` — узнать ближайшую игру, зал и цвет формы.\n"
                    "• `/game НазваниеКоманды` — проверить любую команду лиги.\n"
                    "• `/scout` или `/скаут` — разбор ближайшего соперника.\n"
                    "• `/scout Команда` — статистика указанной команды.\n"
                    "• `/schedule` — календарь матчей команды.\n"
                    "• `/poll` — опрос на игру (буду / не буду).\n"
                    "• `/set НазваниеКоманды` — привязать другую команду к этому диалогу.\n\n"
                    "👥 *Для добавления в группу:*\n"
                    "Добавьте бота в чат команды. Администратор может отправить `/set Название Команды`, "
                    "и бот будет автоматически оповещать всех о новых матчах!"
                ))
            else:
                if chat_id in cfg and cfg[chat_id].get("team"):
                    info = cfg[chat_id]
                    send_msg(chat_id, f"🏀 Этот чат настроен на команду: *{info['team']}*.\nДивизион определяется по расписанию лиги.\nИспользуйте команду `/game` для просмотра ближайшей игры.\n`/scout` или `/скаут` — разбор ближайшего соперника, доступен всем участникам.\n`/scout Команда` — статистика указанной команды.")
                else:
                    send_msg(chat_id, (
                        f"👋 Привет! В этой группе пока не настроена отдельная команда.\n"
                        f"• Напишите `/game` — покажет ближайшую игру команды *{DEFAULT_TEAM}*.\n"
                        "• `/scout` или `/скаут` — разбор её ближайшего соперника.\n"
                        "• `/scout Команда` — статистика указанной команды.\n"
                        f"• Чтобы закрепить за группой вашу команду, администратор должен отправить:\n"
                        f"`/set Название Команды`"
                    ))
            return

        # --- 2. НАСТРОЙКА КОМАНДЫ ДЛЯ ЧАТА (ТОЛЬКО ДЛЯ АДМИНИСТРАТОРОВ) ---
        if cmd in ["/set", "/team"]:
            if not is_private and not is_channel and not is_anon_admin and not is_group_admin(chat_id, user_id):
                w_id = send_msg(chat_id, "⛔️ Только администратор этой группы может настраивать или менять команду.")
                delete_later(chat_id, [w_id, msg.get("message_id")], delay=1.5)
                return

            if not args:
                send_msg(chat_id, "Укажите команду:\n`/set Название Команды`\nДивизион определяется по расписанию лиги.")
                return

            team, _ = parse_team_and_division(args)

            if not team:
                send_msg(chat_id, "Укажите корректное название команды.")
                return

            with CONFIG_LOCK:
                cfg = load_chats_config()
                old = cfg.get(chat_id) if isinstance(cfg.get(chat_id), dict) else {}
                same = old.get("team") == team
                # Повторный /set той же команды сохраняет отправленные анонсы и опросы.
                cfg[chat_id] = {
                    "team": team,
                    "enabled": True,
                    "last_game_id": old.get("last_game_id") if same else None,
                    "reminded_stages": list(old.get("reminded_stages") or []) if same else [],
                    "announcement_message_ids": dict(old.get("announcement_message_ids") or {}) if same else {},
                }
                save_chats_config(cfg)

            send_msg(chat_id, (
                f"✅ Чат успешно привязан!\n\n"
                f"🏀 Команда: *{team}*\n\n"
                f"• Дивизион определяется по расписанию лиги.\n"
                f"• Расписание и цвет формы будут приходить сюда автоматически.\n"
                f"• В любой момент можно написать `/game`, чтобы освежить информацию."
            ))
            return

        # --- 2.1 СБРОС / ОТВЯЗКА КОМАНДЫ (ТОЛЬКО ДЛЯ АДМИНИСТРАТОРОВ) ---
        if cmd in ["/unset", "/reset"]:
            if not is_private and not is_channel and not is_anon_admin and not is_group_admin(chat_id, user_id):
                w_id = send_msg(chat_id, "⛔️ Только администратор этой группы может отвязать команду.")
                delete_later(chat_id, [w_id, msg.get("message_id")], delay=1.5)
                return

            with CONFIG_LOCK:
                cfg = load_chats_config()
                old_team = (cfg.pop(chat_id, None) or {}).get("team", "") if chat_id in cfg else None
                if old_team is not None:
                    save_chats_config(cfg)
            if old_team is not None:
                send_msg(chat_id, f"🗑 Привязка к команде *{old_team}* удалена.\nБот больше не отслеживает игры для этого чата.")
            else:
                send_msg(chat_id, "ℹ️ К этому чату не привязана ни одна команда.")
            return

        # --- 3. ЗАПРОС РАСПИСАНИЯ ИГРОКАМИ (ДОСТУПНО ВСЕМ) ---
        if cmd in ["/scout", "/скаут"]:
            team_query = args or cfg.get(chat_id, {}).get("team") or DEFAULT_TEAM
            team_query, _ = parse_team_and_division(team_query)
            send_msg(chat_id, fetch_scout(team_query or DEFAULT_TEAM, direct=bool(args)))
            return

        if cmd in ["/game", "/next", "/abl"]:
            team_query = args
            division_query = ""

            if team_query:
                team_query, division_query = parse_team_and_division(team_query)
            elif chat_id in cfg and cfg[chat_id].get("team"):
                cfg_team = cfg[chat_id]["team"]
                cfg_div = cfg[chat_id].get("division", "")
                team_query, division_query = parse_team_and_division(cfg_team)
                if not division_query:
                    division_query = cfg_div
            else:
                team_query = DEFAULT_TEAM
                division_query = ""

            if not team_query:
                team_query = DEFAULT_TEAM

            _, reply = fetch_next_game(team_query, division_query)
            send_msg(chat_id, reply)
            return

        # --- 3.1 ОПРОС НА ИГРУ (БУДУ / НЕ БУДУ) ---
        if cmd in ["/poll", "/опрос"]:
            if not is_private and not is_channel and not is_anon_admin and not is_group_admin(chat_id, user_id):
                w_id = send_msg(chat_id, "⛔️ Только администратор этой группы может создавать опрос.")
                delete_later(chat_id, [w_id, msg.get("message_id")], delay=1.5)
                return

            team_query = args
            division_query = ""
            if team_query:
                team_query, division_query = parse_team_and_division(team_query)
            elif chat_id in cfg and cfg[chat_id].get("team"):
                cfg_team = cfg[chat_id]["team"]
                cfg_div = cfg[chat_id].get("division", "")
                team_query, division_query = parse_team_and_division(cfg_team)
                if not division_query:
                    division_query = cfg_div
            else:
                team_query = DEFAULT_TEAM

            game_id, card_or_err = fetch_next_game(team_query, division_query)
            if not game_id:
                send_msg(chat_id, card_or_err)
                return

            raw_games = fetch_raw_abl_games(fallback_to_cache=True) or []
            g_match = next((g for g in raw_games if isinstance(g, dict) and g.get("id") == game_id), None)
            if g_match:
                t1 = (g_match.get("team") or {}).get("name") or ""
                is_left = match_text(team_query, t1)
                poll_q = build_poll_question(g_match, is_left)
            else:
                poll_q = f"🏀 Игра {team_query}: будешь?"

            p_id = send_poll(chat_id, poll_q, reply_to_message_id=msg.get("message_id"))
            if not p_id:
                send_msg(chat_id, "⚠️ Не удалось создать опрос. Попробуйте позже.")
            return

        # --- 4. КАЛЕНДАРЬ НА СЕЗОН ---
        if cmd in ["/schedule", "/calendar"]:
            team_query = args
            division_query = ""

            if team_query:
                team_query, division_query = parse_team_and_division(team_query)
            elif chat_id in cfg and cfg[chat_id].get("team"):
                cfg_team = cfg[chat_id]["team"]
                cfg_div = cfg[chat_id].get("division", "")
                team_query, division_query = parse_team_and_division(cfg_team)
                if not division_query:
                    division_query = cfg_div
            else:
                team_query = DEFAULT_TEAM
                division_query = ""

            if not team_query:
                team_query = DEFAULT_TEAM

            reply = fetch_schedule(team_query, division_query)
            send_msg(chat_id, reply)
            return

        # --- 5. ДРУЖЕСТВЕННЫЙ ОТВЕТ НА МЛБЛ КОМАНДЫ ---
        if cmd == "/mlbl":
            send_msg(chat_id, "🏀 Этот бот предназначен для лиги ABL.\nИспользуйте команду `/game` для просмотра расписания матчей ABL.")
            return

    except Exception as e:
        safe_print(f"⚠️ Ошибка обработки сообщения: {e}")

# Файлы-пульсы: супервизор перезапускает бота, если цикл опроса или мониторинга завис
# (например, на блокирующем DNS-запросе, который не ограничивается timeout у requests).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
POLL_HEARTBEAT_FILE = os.path.join(SCRIPT_DIR, ".heartbeat.poll")
MONITOR_HEARTBEAT_FILE = os.path.join(SCRIPT_DIR, ".heartbeat.monitor")

def touch_heartbeat(path: str):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass

POLL_TIMEOUT_SEC = 30
# Read-таймаут обязан быть заметно больше long-poll, иначе обычный пустой опрос
# на медленной сети превращается в ReadTimeout («Сбой сети в цикле опроса»)
POLL_HTTP_TIMEOUT = (10, POLL_TIMEOUT_SEC + 20)
POLL_ERRORS_BEFORE_LOG = 3   # единичные сбои сети — норма, логируем только серию
MAX_BACKOFF_SEC = 60.0

def _poll_session() -> requests.Session:
    # Без автоповторов: цикл опроса сам управляет паузами, а пульс должен обновляться
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=0, pool_connections=1, pool_maxsize=1))
    return s

def _backoff(n: int) -> float:
    return min(1.5 * (2 ** min(max(n - 1, 0), 6)), MAX_BACKOFF_SEC)

def _safe_process(msg: dict):
    try:
        process_message(msg)
    except Exception as e:
        safe_print(f"⚠️ Ошибка обработки сообщения: {e}")

def run_bot():
    if not TOKEN:
        safe_print("❌ ОШИБКА: Токен бота не найден! Проверьте файл .env")
        return
    safe_print(f"Бот запущен (токен: {TOKEN[:10]}***, длина {len(TOKEN)} симв.)...")
    threading.Thread(target=start_render_web_server, daemon=True).start()
    threading.Thread(target=auto_monitor_loop, name="monitor", daemon=True).start()

    # Команды обрабатываются в пуле потоков: медленный ответ ABL API или Telegram
    # больше не блокирует опрос и не задерживает остальные чаты
    workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cmd")

    # Снимаем вебхук (иначе getUpdates не работает), но НЕ выбрасываем накопившиеся
    # команды — иначе всё, что прислали во время перезапуска, молча теряется
    try:
        http_post(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook",
                  json={"drop_pending_updates": False}, timeout=TG_TIMEOUT)
    except Exception as e:
        safe_print(f"⚠️ Не удалось снять вебхук: {e}")

    poll = _poll_session()
    offset = 0
    consecutive_errors = 0
    first_error_at = 0.0

    while True:
        touch_heartbeat(POLL_HEARTBEAT_FILE)
        try:
            r = poll.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT_SEC,
                        "allowed_updates": json.dumps(["message", "channel_post"])},
                timeout=POLL_HTTP_TIMEOUT
            )

            if r.status_code == 429:
                try:
                    retry_after = int(r.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    retry_after = 5
                safe_print(f"⚠️ Telegram 429 в getUpdates. Ожидание {retry_after + 1} сек...")
                time.sleep(min(retry_after, 120) + 1.0)
                continue

            if r.status_code == 409:
                # Тот же токен опрашивает другой экземпляр (второй сервер/терминал/вебхук)
                safe_print("⚠️ Telegram 409 Conflict: этим токеном уже опрашивает другой экземпляр бота. "
                           "Остановите дубликат. Повтор через 30 сек...")
                time.sleep(30)
                continue

            if r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"Telegram HTTP {r.status_code}")

            resp_data = r.json()
            if not resp_data.get("ok"):
                desc = resp_data.get("description", "Unknown error")
                if resp_data.get("error_code") == 401:
                    safe_print(f"❌ Неверный токен бота (401): {desc}. Проверьте BOT_TOKEN.")
                    time.sleep(60)  # меньше лимита пульса супервизора
                    continue
                consecutive_errors += 1
                safe_print(f"⚠️ Ошибка Telegram API: {desc}")
                time.sleep(_backoff(consecutive_errors))
                continue

            if consecutive_errors >= POLL_ERRORS_BEFORE_LOG:
                safe_print(f"✅ Связь с Telegram восстановлена (простой {time.time() - first_error_at:.0f} сек)")
            consecutive_errors = 0

            for u in resp_data.get("result", []):
                offset = max(offset, u["update_id"] + 1)
                msg = u.get("message") or u.get("channel_post")
                if msg:
                    workers.submit(_safe_process, msg)

        except requests.exceptions.ReadTimeout:
            # Сервер не закрыл long-poll вовремя — обычная ситуация на мобильной сети
            continue
        except (requests.exceptions.RequestException, ValueError) as e:
            consecutive_errors += 1
            if consecutive_errors == 1:
                first_error_at = time.time()
                poll.close()
                poll = _poll_session()  # сбрасываем возможно «битое» соединение
            delay = _backoff(consecutive_errors)
            if consecutive_errors >= POLL_ERRORS_BEFORE_LOG:
                safe_print(f"❌ Сбой сети в цикле опроса ({consecutive_errors} подряд): {e}. Повтор через {delay:.1f} сек...")
            time.sleep(delay)
        except Exception as e:
            consecutive_errors += 1
            delay = _backoff(consecutive_errors)
            safe_print(f"❌ Непредвиденная ошибка в цикле: {e}. Повтор через {delay:.1f} сек...")
            time.sleep(delay)

if __name__ == "__main__":
    run_bot()
