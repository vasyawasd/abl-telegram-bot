import os
import sys
import time
import signal
import shutil
import subprocess
import atexit

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(SCRIPT_DIR, ".bot.lock")
PID_FILE = os.path.join(SCRIPT_DIR, ".bot.pid")
HEARTBEAT_FILES = [
    os.path.join(SCRIPT_DIR, ".heartbeat.poll"),
    os.path.join(SCRIPT_DIR, ".heartbeat.monitor"),
]
# Цикл опроса обновляет пульс минимум раз в ~60 сек (long-poll + backoff),
# мониторинг — раз в 5 мин (+ до ~2 мин на рассылку). Берём с запасом.
HEARTBEAT_LIMITS = [180, 900]
STARTUP_GRACE_SEC = 120

lock_file_handle = None

def acquire_single_instance_lock():
    """Гарантирует, что запущен строго один экземпляр супервизора (защита от Telegram 409 Conflict)."""
    global lock_file_handle
    try:
        lock_file_handle = open(LOCK_FILE, "a+")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Записываем текущий PID
        with open(PID_FILE, "w", encoding="utf-8") as pf:
            pf.write(str(os.getpid()))
    except (IOError, OSError):
        print("⚠️ [Supervisor] Другой экземпляр бота уже запущен. Завершение работы.")
        sys.exit(0)

def release_single_instance_lock():
    global lock_file_handle
    try:
        if lock_file_handle:
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(lock_file_handle.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            lock_file_handle.close()
    except Exception:
        pass
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except Exception:
            pass

def acquire_wake_lock():
    """Для Android / Termux: удерживает CPU активным при выключенном экране."""
    if shutil.which("termux-wake-lock"):
        try:
            subprocess.run(["termux-wake-lock"], check=False)
            print("🔋 [Termux] Wake-lock успешно активирован (защита от сна Android).")
        except Exception as e:
            print(f"⚠️ [Termux] Не удалось включить wake-lock: {e}")

def release_wake_lock():
    if shutil.which("termux-wake-unlock"):
        try:
            subprocess.run(["termux-wake-unlock"], check=False)
            print("🔋 [Termux] Wake-lock освобожден.")
        except Exception:
            pass

running = True
child_proc = None

def handle_termination(sig, frame):
    global running, child_proc
    print(f"\n🛑 [Supervisor] Получен сигнал {sig}. Корректное завершение бота...")
    running = False
    if child_proc and child_proc.poll() is None:
        try:
            child_proc.terminate()
            child_proc.wait(timeout=5)
        except Exception:
            try:
                child_proc.kill()
            except Exception:
                pass
    release_wake_lock()
    release_single_instance_lock()
    sys.exit(0)

def heartbeat_stale(start_time: float) -> str | None:
    """Возвращает имя зависшего цикла, если его пульс давно не обновлялся."""
    now = time.time()
    if now - start_time < STARTUP_GRACE_SEC:
        return None
    for path, limit in zip(HEARTBEAT_FILES, HEARTBEAT_LIMITS):
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            age = now - start_time  # пульса нет с момента старта
        if age > limit:
            return f"{os.path.basename(path)} ({age:.0f} сек без обновления)"
    return None

def wait_with_watchdog(proc, start_time: float) -> int:
    """Ждёт завершения бота; если цикл завис (не обновляет пульс) — убивает процесс."""
    while True:
        code = proc.poll()
        if code is not None:
            return code
        stale = heartbeat_stale(start_time)
        if stale:
            print(f"🧊 [Supervisor] Бот завис: {stale}. Принудительный перезапуск...", flush=True)
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=10)
                except Exception:
                    pass
            return 1
        time.sleep(10)

def run_supervisor():
    global running, child_proc
    acquire_single_instance_lock()
    acquire_wake_lock()
    atexit.register(release_single_instance_lock)
    atexit.register(release_wake_lock)

    signal.signal(signal.SIGINT, handle_termination)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_termination)

    main_script = os.path.join(SCRIPT_DIR, "main.py")
    backoff = 2.0
    MAX_BACKOFF = 30.0

    print("🛡 [Supervisor] Неубиваемый супервизор активен. Запуск бота...")

    while running:
        start_time = time.time()
        for hb in HEARTBEAT_FILES:
            try:
                os.remove(hb)
            except OSError:
                pass
        try:
            child_proc = subprocess.Popen([sys.executable, main_script], cwd=SCRIPT_DIR)
            exit_code = wait_with_watchdog(child_proc, start_time)
        except Exception as e:
            print(f"❌ [Supervisor] Ошибка запуска процесса бота: {e}")
            exit_code = 1

        duration = time.time() - start_time
        if not running:
            break

        if exit_code == 0:
            print("✅ [Supervisor] Бот штатно завершил работу.")
            break
        else:
            print(f"⚠️ [Supervisor] Бот упал (код {exit_code}, проработал {duration:.1f} сек).")
            # Если бот проработал дольше 60 сек, считаем запуск успешным и сбрасываем задержку
            if duration > 60:
                backoff = 2.0
            else:
                backoff = min(backoff * 2.0, MAX_BACKOFF)

            print(f"⏳ [Supervisor] Автоматический перезапуск через {backoff:.1f} сек...")
            time.sleep(backoff)

if __name__ == "__main__":
    run_supervisor()
