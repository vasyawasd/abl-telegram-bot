import os
import json
import time
import requests
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TOKEN = os.getenv("BOT_TOKEN", "8905956001:AAGm2I5butxOQeO9LjFMn_4yH99eEkPdIBg")
LEAGUE_ID = int(os.getenv("LEAGUE_ID", "2"))
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
    url = f"https://mtgame.ru/api/v1/league/{LEAGUE_ID}/games/"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://ablforpeople.com/"}
    resp = requests.get(url, headers=headers, timeout=10).json()

    now = datetime.now(timezone.utc)
    team_lower = team_name.lower().strip()
    div_lower = division_name.lower().strip()
    upcoming = []

    for g in resp:
        if g.get("status") != "open":
            continue
        t1 = (g.get("team") or {}).get("name") or ""
        t2 = (g.get("competitor_team") or {}).get("name") or ""
        tourn_name = (g.get("tournament") or {}).get("name") or ""

        is_left = team_lower in t1.lower()
        is_right = team_lower in t2.lower()
        div_match = not div_lower or div_lower in tourn_name.lower()

        if (is_left or is_right) and div_match:
            dt_str = g.get("datetime")
            if dt_str:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if dt >= now:
                    upcoming.append((dt, g, is_left, t1, t2))

    if not upcoming:
        div_hint = f" ({division_name})" if division_name else ""
        return None, f"Пока нет открытых назначенных игр для команды *{team_name}*{div_hint}."

    upcoming.sort(key=lambda x: x[0])
    dt, g, is_left, t1, t2 = upcoming[0]
    msk_dt = dt.astimezone(ZoneInfo("Europe/Moscow"))

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
    return g["id"], text

def send_msg(chat_id: int | str, text: str):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10
    )

def auto_monitor_loop():
    """Фоновый мониторинг: проверяет новые игры индивидуально для каждого чата."""
    time.sleep(5)
    while True:
        try:
            cfg = load_chats_config()
            changed = False
            for s_chat_id, info in cfg.items():
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

def run_bot():
    print("Бот запущен в публичном мультикомандном режиме...")
    threading.Thread(target=auto_monitor_loop, daemon=True).start()

    offset = 0
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35
            ).json()

            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("channel_post")
                if not msg or "text" not in msg:
                    continue

                chat_id = str(msg["chat"]["id"])
                user_id = msg.get("from", {}).get("id", 0)
                is_private = msg.get("chat", {}).get("type") == "private"
                raw_text = msg["text"].strip()
                cmd = raw_text.split("@")[0].lower()

                cfg = load_chats_config()

                # --- 1. СПРАВКА И ПОМОЩЬ ---
                if cmd in ["/start", "/help"]:
                    if is_private:
                        send_msg(chat_id, (
                            "👋 Привет! Я бот для мониторинга расписания лиги ABL.\n\n"
                            "🏀 *Как подключить к вашей команде:*\n"
                            "1. Добавьте меня в чат вашей команды.\n"
                            "2. Администратор чата должен отправить команду настройки:\n"
                            "`/set Название Команды`\n"
                            "*(или с дивизионом: `/set Название | Дивизион`)*\n\n"
                            "После этого я буду автоматически присылать расписание и цвет формы в чат!"
                        ))
                    else:
                        if chat_id in cfg and cfg[chat_id].get("team"):
                            info = cfg[chat_id]
                            div_s = f" ({info['division']})" if info.get('division') else ""
                            send_msg(chat_id, f"🏀 Этот чат настроен на команду: *{info['team']}*{div_s}.\nИспользуйте команду `/game` для просмотра ближайшей игры.")
                        else:
                            send_msg(chat_id, "👋 Привет! Чтобы настроить бота, администратор чата должен отправить:\n`/set Название Команды | Дивизион`")
                    continue

                # --- 2. НАСТРОЙКА КОМАНДЫ ДЛЯ ЧАТА (ТОЛЬКО ДЛЯ АДМИНИСТРАТОРОВ) ---
                if cmd.startswith(("/set", "/team")):
                    # Если команда в группе, проверяем права админа этой группы
                    if not is_private and not is_group_admin(chat_id, user_id):
                        send_msg(chat_id, "⛔️ Только администратор этой группы может настраивать или менять команду.")
                        continue

                    prefix = "/team" if cmd.startswith("/team") else "/set"
                    args = raw_text[len(prefix):].strip()
                    if not args:
                        send_msg(chat_id, "Укажите команду:\n`/set Название Команды`\nили с дивизионом:\n`/set Название Команды | Дивизион`")
                        continue

                    if "|" in args:
                        team, division = [x.strip() for x in args.split("|", 1)]
                    else:
                        team, division = args, ""

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
                    continue

                # --- 3. ЗАПРОС РАСПИСАНИЯ ИГРОКАМИ (ДОСТУПНО ВСЕМ) ---
                if any(c in cmd for c in ["/game", "/next", "игра", "форма"]):
                    if chat_id in cfg and cfg[chat_id].get("team"):
                        info = cfg[chat_id]
                        _, reply = fetch_next_game(info["team"], info.get("division", ""))
                        send_msg(chat_id, reply)
                    elif is_private:
                        send_msg(chat_id, "В личных сообщениях укажите команду:\n`/game НазваниеКоманды`")
                    else:
                        send_msg(chat_id, "⚠️ Бот еще не настроен для этой группы. Администратор должен отправить:\n`/set Название Команды`")

        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    run_bot()
