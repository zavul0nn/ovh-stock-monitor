#!/usr/bin/env python3
"""OVH Linux stock monitor. Python standard library only."""
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import json
import logging
import os
import re
from pathlib import Path
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MODELS = tuple(f"vps-2027-model{i}" for i in (2, 3, 4))
ALL_MODELS = tuple(f"vps-2027-model{i}" for i in (1, 2, 3, 4))
DCS = {
    "eu-west-lim": "Germany LIM", "eu-west-rbx": "France RBX",
    "eu-west-gra": "France GRA", "eu-west-sbg": "France SBG",
    "eu-central-waw": "Poland WAW", "eu-west-eri": "UK ERI",
    "eu-south-mil": "Italy MIL",
}
STATUSES = {"available", "out-of-stock", "out-of-stock-preorder-allowed"}
OVH_URL = "https://eu.api.ovh.com/v1/vps/order/rule/datacenter"
LOG = logging.getLogger("ovh-stock")


class RemoteError(Exception):
    def __init__(self, message, retry_after=0, description=""):
        super().__init__(message)
        self.retry_after = retry_after
        self.description = description


def request_json(url, payload=None, timeout=10):
    request = urllib.request.Request(
        url, data=None if payload is None else json.dumps(payload).encode(),
        headers={"User-Agent": "OVHStockMonitor/2.0", "Accept": "application/json",
                 "Content-Type": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        delay = 0
        description = ""
        try:
            body = json.loads(exc.read(65536))
            if isinstance(body, dict) and isinstance(body.get("description"), str):
                description = body["description"]
            delay = int(body.get("parameters", {}).get("retry_after", 0))
        except (ValueError, TypeError, AttributeError):
            pass
        try:
            delay = max(delay, int(exc.headers.get("Retry-After", 0)))
        except (ValueError, TypeError):
            pass
        exc.close()
        # Never log request URLs: Telegram URLs contain the bot token.
        raise RemoteError(f"HTTP {exc.code}", max(0, delay), description) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RemoteError(type(exc).__name__) from None


def parse_stock(data):
    if not isinstance(data, dict) or not isinstance(data.get("datacenters"), list):
        raise RemoteError("Invalid OVH response structure")
    result = {}
    for row in data["datacenters"]:
        if not isinstance(row, dict):
            raise RemoteError("Invalid datacenter row")
        dc = row.get("code")
        if not isinstance(dc, str):
            raise RemoteError("Invalid datacenter code")
        if dc not in DCS:
            continue
        status = row.get("linuxStatus")
        if not isinstance(status, str) or status not in STATUSES or dc in result:
            raise RemoteError(f"Invalid/duplicate Linux status for {dc}")
        result[dc] = status
    if set(result) != set(DCS):
        raise RemoteError("OVH response misses monitored datacenters")
    return result


def fetch_stock(model, subsidiary="IE"):
    query = urllib.parse.urlencode({"ovhSubsidiary": subsidiary, "planCode": model})
    return parse_stock(request_json(f"{OVH_URL}?{query}"))


def telegram_api(token, method, payload, timeout=10):
    def safe_description(text):
        text = text.replace(token, "[redacted]") if token else text
        text = re.sub(r"https?://\S+", "[url]", text)
        text = re.sub(r"\b\d+:[A-Za-z0-9_-]{15,}\b", "[redacted]", text)
        return " ".join(text.split())[:240]

    try:
        data = request_json(f"https://api.telegram.org/bot{token}/{method}", payload, timeout=timeout)
    except RemoteError as exc:
        detail = safe_description(exc.description)
        raise RemoteError(f"{exc}: {detail}" if detail else str(exc), exc.retry_after) from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        delay = 0
        if isinstance(data, dict):
            try:
                delay = int(data.get("parameters", {}).get("retry_after", 0))
            except (ValueError, TypeError, AttributeError):
                pass
        detail = safe_description(str(data.get("description", ""))) if isinstance(data, dict) else ""
        raise RemoteError("Telegram did not confirm request" + (f": {detail}" if detail else ""), max(0, delay))
    return data.get("result")


def send_telegram(token, chat_id, message, markup=None):
    payload = {
        "chat_id": chat_id, "text": message, "disable_notification": False,
        "link_preview_options": {"is_disabled": True},
    }
    if markup is not None:
        payload["reply_markup"] = markup
    telegram_api(token, "sendMessage", payload)


def utc(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        try:
            self.initialize()
        except Exception:
            self.db.close()
            raise

    def initialize(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS stock (
                model TEXT, dc TEXT, status TEXT NOT NULL, PRIMARY KEY(model, dc));
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL,
                created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recipients (chat_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS sessions (admin_id TEXT PRIMARY KEY, action TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL,
                locations TEXT NOT NULL, started REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS watches (
                alert_id INTEGER, chat_id TEXT, acknowledged INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL, PRIMARY KEY(alert_id,chat_id));
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
        with self.db:
            for column in ("chat_id", "markup"):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE outbox ADD COLUMN {column} TEXT")
            if "alert_id" not in columns:
                self.db.execute("ALTER TABLE outbox ADD COLUMN alert_id INTEGER")
            self.db.execute("CREATE INDEX IF NOT EXISTS outbox_chat ON outbox(chat_id,id)")

    def config(self):
        defaults = {"interval": 60, "models": list(MODELS), "revision": 0}
        defaults.update({row[0]: json.loads(row[1]) for row in self.db.execute("SELECT * FROM settings")})
        return defaults

    def setting(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def recipients(self):
        return [row[0] for row in self.db.execute("SELECT chat_id FROM recipients ORDER BY chat_id")]

    def bootstrap(self, recipients, interval=60):
        with self.db:
            if not self.get("configured"):
                for chat in recipients:
                    self.db.execute("INSERT OR IGNORE INTO recipients VALUES (?)", (str(chat),))
                self.setting("interval", interval)
                self.setting("models", list(MODELS))
                self.put("configured", 1)
                # Old single-chat rows had no address. Preserve them across migration.
                for row in self.db.execute("SELECT * FROM outbox WHERE chat_id IS NULL").fetchall():
                    for chat in self.recipients():
                        self.db.execute("INSERT INTO outbox(message,created,attempts,next_try,chat_id) VALUES (?,?,?,?,?)",
                                        (row["message"], row["created"], row["attempts"], row["next_try"], chat))
                    self.db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))

    def close(self):
        self.db.close()

    def get(self, key, default=0):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def enqueue(self, message, now, chat_id=None, markup=None, alert_id=None):
        for chat in ([str(chat_id)] if chat_id is not None else self.recipients()):
            self.db.execute("INSERT INTO outbox(message,created,chat_id,markup,alert_id) VALUES (?,?,?,?,?)",
                            (message, now, chat, json.dumps(markup) if markup else None, alert_id))

    def queue_reminders(self, now):
        with self.db:
            self.db.execute("UPDATE meta SET value=value WHERE key='configured'")
            self._queue_reminders(now)

    def _queue_reminders(self, now):
        for row in self.db.execute("""SELECT a.*,w.chat_id FROM alerts a JOIN watches w ON a.id=w.alert_id
            JOIN recipients r ON r.chat_id=w.chat_id
            WHERE a.active=1 AND w.acknowledged=0 AND w.due<=?
            AND NOT EXISTS(SELECT 1 FROM outbox o WHERE o.alert_id=a.id AND o.chat_id=w.chat_id)""", (now,)).fetchall():
            locations = json.loads(row["locations"])
            self.enqueue(
                f"🚨 OVH: Linux VPS доступен\n{row['model']}\n"
                + "\n".join(DCS[dc] for dc in locations)
                + f"\nОбнаружено: {utc(row['started'])}\n"
                + f"Последняя проверка: {utc(self.get('success:' + row['model']))}\n"
                + "Напоминаю каждую минуту, пока вы не подтвердите или наличие не исчезнет.\n"
                + f"https://www.ovhcloud.com/en-ie/vps/configurator/?planCode={row['model']}",
                now, row["chat_id"], {"inline_keyboard": [[{
                    "text": "✅ Я увидел уведомление", "callback_data": f"seen:{row['id']}"}]]}, row["id"])

    def acknowledge(self, alert_id, chat):
        # Caller owns the update transaction. The chat is taken from Telegram, never callback data.
        cursor = self.db.execute("""UPDATE watches SET acknowledged=1 WHERE alert_id=? AND chat_id=?
            AND acknowledged=0 AND EXISTS(SELECT 1 FROM alerts WHERE id=? AND active=1)""",
            (alert_id, str(chat), alert_id))
        self.db.execute("DELETE FROM outbox WHERE alert_id=? AND chat_id=?", (alert_id, str(chat)))
        return cursor.rowcount > 0

    def stop_alerts(self, model):
        self.db.execute("DELETE FROM outbox WHERE alert_id IN (SELECT id FROM alerts WHERE model=?)", (model,))
        self.db.execute("UPDATE alerts SET active=0 WHERE model=?", (model,))

    def observe(self, model, values, now, revision=None):
        # Stock transitions and their notifications commit together or neither does.
        with self.db:
            # Acquire the writer lock before checking configuration to avoid races with admin edits.
            self.db.execute("UPDATE meta SET value=value WHERE key='configured'")
            if revision is not None:
                config = self.config()
                if model not in config["models"] or config["revision"] != revision:
                    return
            previous = dict(self.db.execute("SELECT dc,status FROM stock WHERE model=?", (model,)))
            vanished = [dc for dc, status in values.items() if status != "available" and previous.get(dc) == "available"]
            covered = set()
            for alert in self.db.execute("SELECT * FROM alerts WHERE model=? AND active=1", (model,)).fetchall():
                old_locations = json.loads(alert["locations"])
                remaining = [dc for dc in old_locations if values.get(dc) == "available"]
                covered.update(remaining)
                if remaining != old_locations:
                    self.db.execute("UPDATE alerts SET locations=?,active=? WHERE id=?", (json.dumps(remaining), bool(remaining), alert["id"]))
                    self.db.execute("DELETE FROM outbox WHERE alert_id=?", (alert["id"],))
            if vanished:
                self.enqueue(f"⚪ OVH: Linux VPS пропал\n{model}\n" + "\n".join(DCS[dc] for dc in vanished)
                             + f"\nОбнаружено: {utc(now)}\nПродолжаю следить за новым появлением.", now)
            new_locations = [dc for dc, status in values.items() if status == "available" and dc not in covered]
            if new_locations:
                cursor = self.db.execute("INSERT INTO alerts(model,locations,started) VALUES (?,?,?)", (model, json.dumps(new_locations), now))
                for chat in self.recipients():
                    self.db.execute("INSERT INTO watches(alert_id,chat_id,due) VALUES (?,?,?)", (cursor.lastrowid, chat, now))
            for dc, status in values.items():
                self.db.execute("INSERT OR REPLACE INTO stock VALUES (?,?,?)", (model, dc, status))
            if self.get("alert:" + model):
                self.enqueue(f"✅ Проверки {model} восстановлены ({utc(now)})", now)
            self.put("failure:" + model, 0)
            self.put("alert:" + model, 0)
            self.put("success:" + model, now)
            self._queue_reminders(now)

    def failure(self, model, now, alert_after):
        with self.db:
            since = self.get("failure:" + model) or now
            self.put("failure:" + model, since)
            if now - since >= alert_after and not self.get("alert:" + model):
                self.enqueue(f"⚠️ Нет достоверных данных OVH для {model} с {utc(since)}. Проверьте логи.", now)
                self.put("alert:" + model, 1)

    def deliver_one(self, sender, now):
        row = self.db.execute("""SELECT o.* FROM outbox o WHERE o.next_try<=?
            AND NOT EXISTS (SELECT 1 FROM outbox older WHERE older.chat_id=o.chat_id AND older.id<o.id)
            ORDER BY o.id LIMIT 1""", (now,)).fetchone()
        if not row:
            return False
        try:
            sender(row["chat_id"], row["message"], json.loads(row["markup"]) if row["markup"] else None)
        except RemoteError as exc:
            delay = max(min(300, 5 * 2 ** min(row["attempts"], 6)), exc.retry_after)
            with self.db:
                self.db.execute("UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=?", (now + delay, row["id"]))
            LOG.warning("Telegram delivery failed: chat_id=%s queue_id=%s alert_id=%s keyboard=%s; %s; retry in %ss",
                        row["chat_id"], row["id"], row["alert_id"], bool(row["markup"]), exc, delay)
            return False
        with self.db:
            self.db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
            if row["alert_id"] is not None:
                self.db.execute("UPDATE watches SET due=? WHERE alert_id=? AND chat_id=?", (now + 60, row["alert_id"], row["chat_id"]))
            self.put("delivery", now)
        return True

    def healthy(self, now, max_age):
        config = self.config()
        models_ok = all(now - self.get("success:" + m) <= max(max_age, config["interval"] + 60) for m in config["models"])
        oldest = self.db.execute("SELECT MIN(created) FROM outbox").fetchone()[0]
        return (models_ok and now - self.get("sender") <= max_age
                and (not self.get("admin_enabled") or now - self.get("admin_poll") <= max_age)
                and (oldest is None or now - oldest <= max_age))


@contextlib.contextmanager
def instance_lock(path):
    with open(str(path) + ".lock", "a+b") as handle:
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def positive_env(name, default, minimum=1):
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def due_models(config, last_poll, retry_until, now):
    return [model for model in config["models"]
            if last_poll.get(model, 0) + config["interval"] <= now and retry_until.get(model, 0) <= now]


def run(path):
    from admin import Admin, parse_ids, poll_updates
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN")
    admins = parse_ids(os.getenv("TELEGRAM_ADMIN_IDS", ""), positive=True)
    recipients_text = os.getenv("TELEGRAM_CHAT_IDS", "") or os.getenv("TELEGRAM_CHAT_ID", "")
    recipients = parse_ids(recipients_text) if recipients_text else admins
    interval = positive_env("POLL_INTERVAL_SECONDS", 60, 15)
    alert_after = positive_env("ALERT_AFTER_SECONDS", 180)
    heartbeat = positive_env("HEARTBEAT_SECONDS", 86400)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    store = Store(path)
    store.bootstrap(recipients, interval)
    with store.db:
        store.put("admin_enabled", 1)
        store.enqueue("✅ Монитор OVH запущен. Настройки: /admin в личном чате с ботом.", time.time())

    def delivery_worker():
        delivery = None
        try:
            delivery = Store(path)
            while not stop.is_set():
                now = time.time()
                with delivery.db:
                    delivery.put("sender", now)
                delivery.queue_reminders(now)
                delivery.deliver_one(lambda chat, text, markup: send_telegram(token, chat, text, markup), now)
                stop.wait(1.1)
        except Exception:
            LOG.error("Delivery worker stopped unexpectedly")
            stop.set()
            raise
        finally:
            if delivery is not None: delivery.close()

    def admin_worker():
        control = None
        try:
            control = Store(path)
            poll_updates(token, Admin(control, admins), stop)
        except Exception:
            LOG.error("Admin worker stopped unexpectedly")
            stop.set()
            raise
        finally:
            if control is not None: control.close()

    control_worker = threading.Thread(target=admin_worker, name="admin")
    control_worker.start()
    worker = threading.Thread(target=delivery_worker, name="telegram")
    worker.start()
    last_poll, retry_until = {}, {}
    previous_revision = -1
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            while not stop.is_set():
                now = time.time()
                config = store.config()
                if config["revision"] != previous_revision:
                    last_poll.clear()
                    previous_revision = config["revision"]
                futures = {pool.submit(fetch_stock, model, os.getenv("OVH_SUBSIDIARY", "IE")): model
                           for model in due_models(config, last_poll, retry_until, now)}
                for future in concurrent.futures.as_completed(futures):
                    model = futures[future]
                    checked = time.time()
                    try:
                        values = future.result()
                    except RemoteError as exc:
                        if model in store.config()["models"]:
                            store.failure(model, checked, alert_after)
                        last_poll[model] = checked
                        retry_until[model] = checked + exc.retry_after
                        LOG.warning("%s check failed: %s", model, exc)
                    else:
                        store.observe(model, values, checked, revision=config["revision"])
                        last_poll[model] = checked
                        LOG.info("%s: %s/7 Linux available", model, sum(v == "available" for v in values.values()))
                with store.db:
                    if now - store.get("heartbeat", now) >= heartbeat:
                        checks = ", ".join(f"{m}: {utc(store.get('success:' + m))}" for m in store.config()["models"])
                        store.enqueue("💓 Монитор OVH работает. Последние успешные проверки: " + checks, now)
                        store.put("heartbeat", now)
                    elif not store.get("heartbeat"):
                        store.put("heartbeat", now)
                stop.wait(1)
    finally:
        stop.set()
        worker.join(timeout=15)
        control_worker.join(timeout=35)
        store.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=("run", "check", "healthcheck", "test-notification"), default="run")
    args = parser.parse_args()
    path = Path(os.getenv("STATE_DB", "/data/monitor.sqlite3"))
    if args.command == "check":
        models = MODELS
        if path.exists():
            store = Store(path)
            try: models = store.config()["models"]
            finally: store.close()
        for model in models:
            print(json.dumps({model: fetch_stock(model, os.getenv("OVH_SUBSIDIARY", "IE"))}, ensure_ascii=False))
    elif args.command == "test-notification":
        from admin import parse_ids
        chats = parse_ids(os.getenv("TELEGRAM_CHAT_IDS", "") or os.getenv("TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_ADMIN_IDS", ""))
        if path.exists():
            store = Store(path)
            try:
                if store.get("configured"): chats = store.recipients()
            finally: store.close()
        if not chats: raise ValueError("No recipients configured")
        for chat in chats:
            send_telegram(os.environ["TELEGRAM_BOT_TOKEN"], chat, "✅ Тест доставки OVH: Telegram работает.")
        print("Telegram confirmed delivery")
    elif args.command == "healthcheck":
        if not path.exists():
            return 1
        store = Store(path)
        try:
            return 0 if store.healthy(time.time(), positive_env("HEALTH_MAX_AGE_SECONDS", 300)) else 1
        finally:
            store.close()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with instance_lock(path):
            run(path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RemoteError, ValueError, OSError, sqlite3.Error) as exc:
        LOG.error("Monitor stopped: %s", type(exc).__name__ if not isinstance(exc, (RemoteError, ValueError)) else exc)
        raise SystemExit(1)
