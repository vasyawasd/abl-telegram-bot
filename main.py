import os
import json
import time
import requests
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TOKEN = os.getenv("BOT_TOKEN", "8905956001:AAGm2I5butxOQeO9LjFMn_4yH99eEkPdIBg")
DEFAULT_TEAM = "БК Путилково"
LEAGUE_ID = 2
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
        div_hint = f" в дивизионе '{division_name}'" if division_name else ""
        return None, f"Пока нет назначенных игр для {team_name}{div_hint}."

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
    """Проверяет обновления для каждого чата с учетом его дивизиона."""
    time.sleep(5)
    while True:
        try:
            cfg = load_chats_config()
            changed = False
            for s_chat_id, info in cfg.items():
                team = info.get("team", DEFAULT_TEAM)
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
    print(f"Бот запущен. Мониторинг по дивизионам активен...")
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
                text = msg["text"].strip()
                text_low = text.lower()
                cfg = load_chats_config()

                # Команда настройки: /set дивизион (или /set команда | дивизион)
                if text_low.startswith("/set"):
                    args = text[4:].strip()
                    if "|" in args:
                        team, division = [x.strip() for x in args.split("|", 1)]
                    elif args:
                        team = DEFAULT_TEAM
                        division = args
                    else:
                        team = DEFAULT_TEAM
                        division = ""

                    cfg[chat_id] = {
                        "team": team,
                        "division": division,
                        "last_game_id": cfg.get(chat_id, {}).get("last_game_id")
                    }
                    save_chats_config(cfg)
                    div_label = f", дивизион: *{division}*" if division else " (все дивизионы)"
                    send_msg(chat_id, f"✅ Чат привязан к: *{team}*{div_label}\n\nТеперь бот будет присылать расписание сюда автоматически!")
                    continue

                # Инициализация чата при добавлении
                if chat_id not in cfg:
                    cfg[chat_id] = {"team": DEFAULT_TEAM, "division": "", "last_game_id": None}
                    save_chats_config(cfg)

                if any(cmd in text_low for cmd in ["/game", "/next", "игра", "форма", "/start"]):
                    info = cfg[chat_id]
                    _, reply = fetch_next_game(info.get("team", DEFAULT_TEAM), info.get("division", ""))
                    send_msg(chat_id, reply)

        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    run_bot()
