import os
import json
import time
import requests
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TOKEN = os.getenv("BOT_TOKEN", "8905956001:AAGm2I5butxOQeO9LjFMn_4yH99eEkPdIBg")
LEAGUE_ID = int(os.getenv("LEAGUE_ID", "2"))
DEFAULT_TEAM = os.getenv("DEFAULT_TEAM", "БК Путилково")
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
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=5
        )
    except Exception:
        pass

def delete_later(chat_id: int | str, message_ids: list, delay: float = 1.5):
    """Удаляет список сообщений через delay секунд (в фоне, чтобы не блокировать бота)."""
    def _worker():
        time.sleep(delay)
        for mid in message_ids:
            if mid:
                delete_msg(chat_id, mid)
    threading.Thread(target=_worker, daemon=True).start()


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

def run_bot():
    print("Бот запущен в публичном мультикомандном режиме...")
    threading.Thread(target=start_render_web_server, daemon=True).start()
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
                            f"🏀 По умолчанию настроен на команду: *{DEFAULT_TEAM}*\n\n"
                            "• Нажмите `/game` — узнать ближайшую игру, зал и цвет формы.\n"
                            "• `/game НазваниеКоманды` — проверить любую команду лиги.\n"
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
                    continue

                # --- 2. НАСТРОЙКА КОМАНДЫ ДЛЯ ЧАТА (ТОЛЬКО ДЛЯ АДМИНИСТРАТОРОВ) ---
                if cmd.startswith(("/set", "/team")):
                    # Если команда в группе, проверяем права админа этой группы
                    if not is_private and not is_group_admin(chat_id, user_id):
                        w_id = send_msg(chat_id, "⛔️ Только администратор этой группы может настраивать или менять команду.")
                        delete_later(chat_id, [w_id, msg.get("message_id")], delay=1.5)
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

                # --- 2.1 СБРОС / ОТВЯЗКА КОМАНДЫ (ТОЛЬКО ДЛЯ АДМИНИСТРАТОРОВ) ---
                if cmd in ["/unset", "/reset"]:
                    if not is_private and not is_group_admin(chat_id, user_id):
                        w_id = send_msg(chat_id, "⛔️ Только администратор этой группы может отвязать команду.")
                        delete_later(chat_id, [w_id, msg.get("message_id")], delay=1.5)
                        continue

                    if chat_id in cfg:
                        old_team = cfg[chat_id].get("team", "")
                        del cfg[chat_id]
                        save_chats_config(cfg)
                        send_msg(chat_id, f"🗑 Привязка к команде *{old_team}* удалена.\nБот больше не отслеживает игры для этого чата.")
                    else:
                        send_msg(chat_id, "ℹ️ К этому чату не привязана ни одна команда.")
                    continue

                # --- 3. ЗАПРОС РАСПИСАНИЯ ИГРОКАМИ (ДОСТУПНО ВСЕМ) ---
                is_game_cmd = False
                team_query = ""
                division_query = ""

                if cmd.startswith(("/game", "/next")):
                    is_game_cmd = True
                    prefix = "/next" if cmd.startswith("/next") else "/game"
                    team_query = raw_text[len(prefix):].strip()
                elif any(c in cmd for c in ["игра", "форма"]):
                    is_game_cmd = True

                if is_game_cmd:
                    if team_query:
                        if "|" in team_query:
                            team_query, division_query = [x.strip() for x in team_query.split("|", 1)]
                    elif chat_id in cfg and cfg[chat_id].get("team"):
                        team_query = cfg[chat_id]["team"]
                        division_query = cfg[chat_id].get("division", "")
                    else:
                        team_query = DEFAULT_TEAM
                        division_query = ""

                    _, reply = fetch_next_game(team_query, division_query)
                    send_msg(chat_id, reply)
                    continue

        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    run_bot()
