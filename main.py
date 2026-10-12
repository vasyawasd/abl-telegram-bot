import os
import json
import time
import socket
import requests
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Принудительно используем IPv4 для Android / Termux (избегаем Errno 101 Network is unreachable)
try:
    import urllib3.util.connection as urllib3_cn
    urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass

def get_bot_token():
    t = os.getenv("BOT_TOKEN")
    if not t and os.path.exists(".env"):
        try:
            with open(".env", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("BOT_TOKEN="):
                        t = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    if not t:
        try:
            t = input("Введите токен Telegram-бота (BOT_TOKEN): ").strip()
            if t:
                with open(".env", "w", encoding="utf-8") as f:
                    f.write(f"BOT_TOKEN={t}\n")
        except Exception:
            pass
    return t or ""

TOKEN = get_bot_token()
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

def delete_later(chat_id: int | str, message_ids: list, delay: float = 2.5):
    """Удаляет список сообщений через delay секунд (в фоне, чтобы не блокировать бота)."""
    def _worker():
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
        for mid in valid_ids:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
                    json={"chat_id": chat_id, "message_id": mid},
                    timeout=5
                )
                time.sleep(0.2)
            except Exception:
                pass
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
    if not TOKEN:
        print("❌ ОШИБКА: Токен бота не найден! Проверьте файл .env")
        return
    print(f"Бот запущен (токен: {TOKEN[:10]}***, длина {len(TOKEN)} симв.)...")
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
                print(f"⚠️ Ошибка Telegram API: {desc}")
                time.sleep(3)
                continue

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
                clean_text = raw_text.strip().lower().rstrip("?!.,; ")

                # Логируем только команды бота, чтобы не спамить в терминал обычной перепиской
                if raw_text.startswith("/") or clean_text in ["игра", "форма", "когда игра", "какая форма", "расписание"]:
                    print(f"🤖 [{chat_id}] {raw_text}")

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

                if cmd.startswith(("/game", "/next", "/abl")):
                    is_game_cmd = True
                    prefix = "/abl" if cmd.startswith("/abl") else ("/next" if cmd.startswith("/next") else "/game")
                    team_query = raw_text[len(prefix):].strip()
                elif clean_text in ["игра", "форма", "когда игра", "какая форма", "расписание"]:
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

        except Exception as e:
            print(f"❌ Ошибка в цикле: {e}")
            time.sleep(3)

if __name__ == "__main__":
    run_bot()
