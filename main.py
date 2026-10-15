import os
import sys
import re
import json
import time
import socket
import requests
import threading
from datetime import datetime, timezone, timedelta

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
except Exception:
    pass

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
CHECK_INTERVAL_SEC = 600
CHATS_FILE = "chats.json"

def load_chats_config() -> dict:
    if os.path.exists(CHATS_FILE):
        try:
            with open(CHATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_chats_config(data: dict):
    with open(CHATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_group_admin(chat_id: str | int, user_id: int) -> bool:
    """Проверяет права пользователя в конкретной группе Telegram."""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getChatMember",
            params={"chat_id": chat_id, "user_id": user_id},
            timeout=5
        ).json()
        if r.get("ok"):
            status = r.get("result", {}).get("status")
            return status in ["creator", "administrator"]
    except Exception:
        pass
    return False

def fetch_next_game(team_name: str, division_name: str = ""):
    parsed_team, parsed_div = parse_team_and_division(team_name)
    team_name = parsed_team or team_name
    division_name = division_name or parsed_div

    if not team_name:
        return None, "⚠️ Пожалуйста, укажите название команды: `/game НазваниеКоманды`"

    url = f"https://mtgame.ru/api/v1/league/{LEAGUE_ID}/games/"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://ablforpeople.com/"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        resp = r.json()
    except Exception as e:
        safe_print(f"⚠️ Сбой при запросе к ABL API: {e}")
        return None, "⚠️ Не удалось получить данные о расписании ABL (ошибка сети/сервера). Попробуйте позже."

    if not isinstance(resp, list):
        return None, "⚠️ Не удалось получить данные о расписании ABL. Попробуйте позже."

    now = datetime.now(timezone.utc)
    upcoming = []

    for g in resp:
        if not isinstance(g, dict):
            continue
        if g.get("status") != "open":
            continue
        t1 = (g.get("team") or {}).get("name") or ""
        t2 = (g.get("competitor_team") or {}).get("name") or ""
        tourn_name = (g.get("tournament") or {}).get("name") or ""

        is_left = match_text(team_name, t1)
        is_right = match_text(team_name, t2)
        div_match = not division_name or match_text(division_name, tourn_name)

        if (is_left or is_right) and div_match:
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
        div_hint = f" ({division_name})" if division_name else ""
        return None, f"Пока нет открытых назначенных игр для команды *{team_name}*{div_hint}."

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
    parsed_team, parsed_div = parse_team_and_division(team_name)
    team_name = parsed_team or team_name
    division_name = division_name or parsed_div

    if not team_name:
        return "⚠️ Пожалуйста, укажите название команды: `/schedule НазваниеКоманды`"

    url = f"https://mtgame.ru/api/v1/league/{LEAGUE_ID}/games/"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://ablforpeople.com/"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        resp = r.json()
    except Exception as e:
        safe_print(f"⚠️ Сбой при запросе к ABL API: {e}")
        return "⚠️ Не удалось получить данные о расписании ABL (ошибка сети/сервера). Попробуйте позже."

    if not isinstance(resp, list):
        return "⚠️ Не удалось получить данные о расписании ABL. Попробуйте позже."

    now = datetime.now(timezone.utc)
    min_dt = datetime.min.replace(tzinfo=timezone.utc)
    team_games = []

    for g in resp:
        if not isinstance(g, dict):
            continue
        t1 = (g.get("team") or {}).get("name") or ""
        t2 = (g.get("competitor_team") or {}).get("name") or ""
        tourn_name = (g.get("tournament") or {}).get("name") or ""

        is_left = match_text(team_name, t1)
        is_right = match_text(team_name, t2)
        div_match = not division_name or match_text(division_name, tourn_name)

        if (is_left or is_right) and div_match:
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
        div_hint = f" ({division_name})" if division_name else ""
        return f"Игр для команды *{team_name}*{div_hint} не найдено."

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
            time_display = msk_dt.strftime('%d.%m в %H:%M') if dt > datetime(2000, 1, 1, tzinfo=timezone.utc) else "Дата уточняется"
            lines.append(f"• *{time_display}* — vs {opp} ({form}) | {court}")

    if finished:
        lines.append("\n*🏁 Завершённые матчи:*")
        for dt, g, is_left, t1, t2 in finished[-5:]:
            s1 = g.get("team_score", 0)
            s2 = g.get("competitor_team_score", 0)
            lines.append(f"• {t1} {s1}:{s2} {t2}")

    return "\n".join(lines)

def send_msg(chat_id: int | str, text: str) -> int | None:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        ).json()
        if r.get("ok"):
            return r["result"]["message_id"]
        # Резерв без Markdown, если в тексте были спецсимволы
        r2 = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        ).json()
        if r2.get("ok"):
            return r2["result"]["message_id"]
    except Exception:
        pass
    return None

def delete_msg(chat_id: int | str, message_id: int):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": int(message_id)},
            timeout=5
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
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/deleteMessages",
                json={"chat_id": chat_id, "message_ids": valid_ids},
                timeout=5
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


def auto_monitor_loop():
    """Фоновый мониторинг: проверяет новые игры индивидуально для каждого чата."""
    time.sleep(5)
    while True:
        try:
            cfg = load_chats_config()
            changed = False
            for s_chat_id, info in cfg.items():
                if not isinstance(info, dict):
                    continue
                team = info.get("team")
                if not team:
                    continue
                division = info.get("division", "")
                last_id = info.get("last_game_id")

                game_id, text = fetch_next_game(team, division)
                if game_id and game_id != last_id:
                    send_msg(s_chat_id, f"⚡️ *Новое расписание матча!*\n\n{text}")
                    info["last_game_id"] = game_id
                    changed = True

            if changed:
                save_chats_config(cfg)
        except Exception:
            pass
        time.sleep(CHECK_INTERVAL_SEC)

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
        server = socketserver.TCPServer(("", port), QuietHandler)
        server.serve_forever()
    except Exception:
        pass

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
                    "• `/schedule` — календарь матчей команды.\n"
                    "• `/set НазваниеКоманды` — привязать другую команду к этому диалогу.\n\n"
                    "👥 *Для добавления в группу:*\n"
                    "Добавьте бота в чат команды. Администратор может отправить `/set Название Команды`, "
                    "и бот будет автоматически оповещать всех о новых матчах!"
                ))
            else:
                if chat_id in cfg and cfg[chat_id].get("team"):
                    info = cfg[chat_id]
                    div_s = f" ({info['division']})" if info.get('division') else ""
                    send_msg(chat_id, f"🏀 Этот чат настроен на команду: *{info['team']}*{div_s}.\nИспользуйте команду `/game` для просмотра ближайшей игры.")
                else:
                    send_msg(chat_id, (
                        f"👋 Привет! В этой группе пока не настроена отдельная команда.\n"
                        f"• Напишите `/game` — покажет ближайшую игру команды *{DEFAULT_TEAM}*.\n"
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
                send_msg(chat_id, "Укажите команду:\n`/set Название Команды`\nили с дивизионом:\n`/set Название Команды | Дивизион`")
                return

            team, division = parse_team_and_division(args)

            if not team:
                send_msg(chat_id, "Укажите корректное название команды.")
                return

            cfg[chat_id] = {
                "team": team,
                "division": division,
                "last_game_id": None
            }
            save_chats_config(cfg)

            div_label = f", дивизион: *{division}*" if division else ""
            send_msg(chat_id, (
                f"✅ Чат успешно привязан!\n\n"
                f"🏀 Команда: *{team}*{div_label}\n\n"
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

            if chat_id in cfg:
                old_team = cfg[chat_id].get("team", "")
                del cfg[chat_id]
                save_chats_config(cfg)
                send_msg(chat_id, f"🗑 Привязка к команде *{old_team}* удалена.\nБот больше не отслеживает игры для этого чата.")
            else:
                send_msg(chat_id, "ℹ️ К этому чату не привязана ни одна команда.")
            return

        # --- 3. ЗАПРОС РАСПИСАНИЯ ИГРОКАМИ (ДОСТУПНО ВСЕМ) ---
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

def run_bot():
    if not TOKEN:
        safe_print("❌ ОШИБКА: Токен бота не найден! Проверьте файл .env")
        return
    safe_print(f"Бот запущен (токен: {TOKEN[:10]}***, длина {len(TOKEN)} симв.)...")
    threading.Thread(target=start_render_web_server, daemon=True).start()
    threading.Thread(target=auto_monitor_loop, daemon=True).start()

    # Сбрасываем любые чужие вебхуки и спам
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook", json={"drop_pending_updates": True}, timeout=10)
    except Exception:
        pass

    offset = 0
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35
            ).json()

            if not r.get("ok"):
                desc = r.get("description", "Unknown error")
                safe_print(f"⚠️ Ошибка Telegram API: {desc}")
                time.sleep(3)
                continue

            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("channel_post")
                if msg:
                    process_message(msg)

        except Exception as e:
            safe_print(f"❌ Ошибка в цикле: {e}")
            time.sleep(3)

if __name__ == "__main__":
    run_bot()
