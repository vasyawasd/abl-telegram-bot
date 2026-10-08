import os
import json
import re
import time
import requests
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TOKEN = os.getenv("BOT_TOKEN", "8905956001:AAGm2I5butxOQeO9LjFMn_4yH99eEkPdIBg")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # ID главного администратора (0 = не задан)
LEAGUE_ID = int(os.getenv("LEAGUE_ID", "2"))
CHECK_INTERVAL_SEC = 600

TEAMS_FILE = "teams.json"
CHATS_FILE = "chats.json"

def to_slug(text: str) -> str:
    slug = re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', text.lower())
    return slug or "team"

def load_teams() -> list:
    if os.path.exists(TEAMS_FILE):
        try:
            with open(TEAMS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    default = [
        {"id": 1, "name": "БК Путилково", "division": "Москвич", "slug": "путилково_москвич"}
    ]
    save_teams(default)
    return default

def save_teams(teams: list):
    with open(TEAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(teams, f, ensure_ascii=False, indent=2)

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
    """Проверяет, является ли пользователь создателем или админом группы."""
    if ADMIN_ID and user_id == ADMIN_ID:
        return True
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
        return None, f"Пока нет назначенных игр для *{team_name}*{div_hint}."

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
    """Фоновый поток: проверяет новые игры для каждого привязанного чата."""
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

def format_teams_list(teams: list) -> str:
    if not teams:
        return "Список команд пуст.\nДобавьте команду: `/addteam Название | Дивизион`"
    lines = ["📋 *Курируемые команды и команды для групп:*\n"]
    for t in teams:
        div_str = f" ({t['division']})" if t.get('division') else ""
        lines.append(
            f"🔹 *{t['id']}. {t['name']}*{div_str}\n"
            f"   👉 Команда активации в группе: `/start_{t['id']}`\n"
        )
    lines.append("Добавить команду: `/addteam Название | Дивизион`")
    lines.append("Удалить команду: `/delteam <номер>`")
    return "\n".join(lines)

def run_bot():
    print("Бот запущен. Ожидание событий...")
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

                teams = load_teams()
                cfg = load_chats_config()

                # Команда для получения своего ID
                if cmd == "/myid":
                    send_msg(chat_id, f"🆔 Ваш Telegram User ID: `{user_id}`")
                    continue

                # --- 1. ПЕРВИЧНАЯ НАСТРОЙКА (в личных сообщениях) ---
                if cmd in ["/teams", "/setup", "/start"] and is_private:
                    send_msg(chat_id, format_teams_list(teams))
                    continue

                if cmd.startswith("/addteam"):
                    if not is_private:
                        send_msg(chat_id, "⚠️ Настройка команд доступна только в личных сообщениях с ботом.")
                        continue
                    if ADMIN_ID and user_id != ADMIN_ID:
                        send_msg(chat_id, "⛔️ Доступ запрещен. Только администратор бота может добавлять команды.")
                        continue

                    args = raw_text[8:].strip()
                    if not args:
                        send_msg(chat_id, "Формат: `/addteam Название Команды | Дивизион`\nПример: `/addteam БК Путилково | Перово`")
                        continue
                    if "|" in args:
                        t_name, d_name = [x.strip() for x in args.split("|", 1)]
                    else:
                        t_name, d_name = args, ""

                    new_id = max([t["id"] for t in teams], default=0) + 1
                    slug_str = to_slug(f"{t_name}_{d_name}" if d_name else t_name)
                    teams.append({"id": new_id, "name": t_name, "division": d_name, "slug": slug_str})
                    save_teams(teams)

                    send_msg(chat_id, (
                        f"✅ Добавлена команда №{new_id}: *{t_name}* "
                        f"{'(' + d_name + ')' if d_name else ''}\n\n"
                        f"👉 Для привязки группы отправьте в неё команду:\n"
                        f"`/start_{new_id}`"
                    ))
                    continue

                if cmd.startswith("/delteam"):
                    if not is_private:
                        send_msg(chat_id, "⚠️ Удаление команд доступно только в личных сообщениях с ботом.")
                        continue
                    if ADMIN_ID and user_id != ADMIN_ID:
                        send_msg(chat_id, "⛔️ Доступ запрещен. Только администратор бота может удалять команды.")
                        continue

                    idx_str = raw_text[8:].strip()
                    if idx_str.isdigit():
                        idx = int(idx_str)
                        teams = [t for t in teams if t["id"] != idx]
                        save_teams(teams)
                        send_msg(chat_id, f"🗑 Команда №{idx} удалена.")
                    else:
                        send_msg(chat_id, "Укажите номер: `/delteam 1`")
                    continue

                # --- 2. ПРИВЯЗКА ЧАТА ГРУППЫ (только для админов группы) ---
                if cmd.startswith("/start_"):
                    if not is_private and not is_group_admin(chat_id, user_id):
                        send_msg(chat_id, "⛔️ Только администратор группы может привязывать команду к этому чату.")
                        continue

                    param = cmd.replace("/start_", "").strip()
                    matched = None
                    for t in teams:
                        if str(t["id"]) == param or t["slug"] == param or to_slug(t["name"]) == param:
                            matched = t
                            break

                    if matched:
                        cfg[chat_id] = {
                            "team": matched["name"],
                            "division": matched.get("division", ""),
                            "last_game_id": None
                        }
                        save_chats_config(cfg)
                        div_label = f", дивизион: *{matched['division']}*" if matched.get('division') else ""
                        send_msg(chat_id, (
                            f"🎉 *Этот чат успешно привязан!*\n\n"
                            f"🏀 Команда: *{matched['name']}*{div_label}\n\n"
                            f"Бот будет автоматически присылать расписание и форму, "
                            f"а также отвечать на запросы игроков `/game`."
                        ))
                    else:
                        send_msg(chat_id, f"❌ Команда `{param}` не найдена. Напишите `/teams` в личные сообщения боту.")
                    continue

                # --- 3. ИГРОКИ (доступно всем участникам чата) ---
                if any(c in cmd for c in ["/game", "/next", "игра", "форма"]):
                    if chat_id in cfg and cfg[chat_id].get("team"):
                        info = cfg[chat_id]
                        _, reply = fetch_next_game(info["team"], info.get("division", ""))
                        send_msg(chat_id, reply)
                    else:
                        send_msg(chat_id, "⚠️ Чат еще не привязан к команде. Администратор чата должен отправить команду вида `/start_1`.")

        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    run_bot()
