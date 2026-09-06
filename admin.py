"""Private Telegram admin menu. All mutations and update offsets commit atomically."""
import re
import time

from monitor import ALL_MODELS, LOG, RemoteError, Store, telegram_api, utc


def parse_ids(text, positive=False):
    values = re.split(r"[,;\s]+", text.strip())
    if not text.strip() or any(not re.fullmatch(r"-?[1-9][0-9]{0,15}", x) for x in values):
        raise ValueError("Нужны числовые Telegram ID через пробел или запятую.")
    if any(abs(int(x)) >= 2**52 or (positive and int(x) <= 0) for x in values):
        raise ValueError("Некорректный Telegram ID.")
    return list(dict.fromkeys(str(int(x)) for x in values))


def keyboard(rows):
    return {"inline_keyboard": [[{"text": title, "callback_data": action} for title, action in row] for row in rows]}


class Admin:
    def __init__(self, store, ids):
        self.store = store
        self.ids = {str(x) for x in ids}

    def reply(self, chat, text, now, rows=None):
        self.store.enqueue(text, now, chat, keyboard(rows) if rows else None)

    def menu(self, chat, now, page="main"):
        config = self.store.config()
        back = [("← Меню", "main")]
        if page == "models":
            rows = []
            for model in ALL_MODELS:
                enabled = model in config["models"]
                rows.append([(f"{'✅' if enabled else '⬜'} VPS-{model[-1]}", f"model:{model[-1]}:{0 if enabled else 1}")])
            self.reply(chat, "Выберите тарифы Linux в Европе. Кнопка применяет изменение сразу.\n"
                       "При включении тарифа проверим и уже доступные VPS.", now, rows + [back])
        elif page == "interval":
            self.reply(chat, f"Проверять каждые {config['interval'] / 60:g} мин.\nМожно ввести своё целое число от 1 до 1440 минут.", now,
                       [[(f"{n} мин", f"minutes:{n}") for n in (1, 2, 5)],
                        [(f"{n} мин", f"minutes:{n}") for n in (10, 30, 60)],
                        [("Ввести число", "input:interval")], back])
        elif page.startswith("chats"):
            parts = page.split(":")
            offset = max(0, int(parts[1])) if len(parts) > 1 else 0
            chats = self.store.recipients()
            offset = min(offset, max(0, ((len(chats) - 1) // 8) * 8))
            rows = [[(f"Удалить {cid}", f"remove:{cid}")] for cid in chats[offset:offset+8]]
            nav = []
            if offset: nav.append(("←", f"chats:{max(0, offset-8)}"))
            if offset + 8 < len(chats): nav.append(("→", f"chats:{offset+8}"))
            if nav: rows.append(nav)
            rows += [[("Добавить ID", "input:chats"), ("Добавить меня", "add_me")],
                     [("Тест всем получателям", "test")], back]
            self.reply(chat, f"Получателей: {len(chats)}.\n" + ("\n".join(chats[offset:offset+8]) or "Список пуст — уведомления о наличии отправлять некому."), now, rows)
        elif page == "status":
            lines = [f"Интервал: {config['interval'] / 60:g} мин", f"Получателей: {len(self.store.recipients())}"]
            for model in config["models"]:
                last = self.store.get("success:" + model)
                lines.append(f"VPS-{model[-1]}: {utc(last) if last else 'ожидает проверки'}")
            pending = self.store.db.execute("SELECT COUNT(*), MIN(created) FROM outbox").fetchone()
            lines.append(f"В очереди: {pending[0]}")
            if pending[1]: lines.append(f"Самое старое: {utc(pending[1])}")
            failures = self.store.db.execute("SELECT chat_id, COUNT(*) FROM outbox WHERE attempts>0 GROUP BY chat_id LIMIT 10").fetchall()
            if failures:
                lines.append("Ожидают повторной доставки:")
                lines.extend(f"{row[0]}: {row[1]} сообщ." for row in failures)
            self.reply(chat, "\n".join(lines), now, [[("Обновить", "status")], back])
        else:
            self.reply(chat, "⚙️ Управление монитором OVH\n"
                       f"Интервал: {config['interval'] / 60:g} мин\n"
                       f"Тарифы: {', '.join('VPS-' + m[-1] for m in config['models'])}\n"
                       f"Получателей: {len(self.store.recipients())}", now,
                       [[("⏱ Интервал", "interval"), ("🖥 Тарифы", "models")],
                        [("📨 Получатели", "chats"), ("📊 Статус", "status")]])

    def set_interval(self, value):
        if not value.isdecimal() or not 1 <= int(value) <= 1440:
            raise ValueError("Введите целое число минут от 1 до 1440.")
        self.store.setting("interval", int(value) * 60)

    def add_chats(self, ids, now):
        for cid in ids:
            cursor = self.store.db.execute("INSERT OR IGNORE INTO recipients VALUES (?)", (cid,))
            if cursor.rowcount:
                self.reply(cid, "✅ Этот чат добавлен в уведомления OVH. Здесь будут новые события появления VPS.", now)

    def process(self, update, now=None):
        now = time.time() if now is None else now
        update_id = update.get("update_id")
        if type(update_id) is not int:
            raise RemoteError("Invalid Telegram update ID")
        callback = update.get("callback_query") or {}
        message = callback.get("message") or update.get("message") or {}
        user = callback.get("from") or message.get("from") or {}
        chat = message.get("chat") or {}
        cid, uid = str(chat.get("id", "")), str(user.get("id", ""))
        authorized = uid in self.ids and chat.get("type") == "private" and cid == uid and not user.get("is_bot")
        with self.store.db:
            self.store.db.execute("UPDATE meta SET value=value WHERE key='configured'")
            if update_id < self.store.get("update_offset", -1):
                return None
            acknowledgement = re.fullmatch(r"seen:([1-9][0-9]*)", callback.get("data", ""))
            can_ack = cid in self.store.recipients() and not user.get("is_bot") and (
                (chat.get("type") == "private" and cid == uid) or uid in self.ids)
            if acknowledgement and can_ack:
                if self.store.acknowledge(int(acknowledgement[1]), cid):
                    self.reply(cid, "✅ Принято. Напоминания об этом появлении отключены для этого чата. Проверки продолжаются.", now)
            elif authorized:
                try:
                    if callback:
                        self.action(cid, callback.get("data", ""), now)
                    else:
                        self.message(cid, message.get("text", ""), now)
                except ValueError as exc:
                    self.reply(cid, str(exc), now, [[("Отмена", "main")]])
            elif not callback and chat.get("type") == "private" and message.get("text") == "/id":
                self.reply(cid, f"Ваш Telegram ID: {uid}", now)
            self.store.put("update_offset", update_id + 1)
        return callback.get("id")

    def action(self, chat, data, now):
        if data in ("main", "models", "interval", "status") or re.fullmatch(r"chats(?::\d{1,8})?", data):
            self.store.db.execute("DELETE FROM sessions WHERE admin_id=?", (chat,))
            self.menu(chat, now, data)
        elif data.startswith("minutes:"):
            self.set_interval(data.split(":", 1)[1])
            self.store.db.execute("DELETE FROM sessions WHERE admin_id=?", (chat,))
            self.menu(chat, now, "interval")
        elif re.fullmatch(r"model:[1-4]:[01]", data):
            _, number, enabled = data.split(":")
            model = "vps-2027-model" + number
            config = self.store.config()
            selected = set(config["models"])
            changed = (model in selected) != (enabled == "1")
            if changed:
                if enabled == "1": selected.add(model)
                else: selected.remove(model)
                if not selected: raise ValueError("Оставьте хотя бы один тариф.")
                self.store.setting("models", sorted(selected))
                self.store.setting("revision", config["revision"] + 1)
                self.store.stop_alerts(model)
                self.store.db.execute("DELETE FROM stock WHERE model=?", (model,))
                for prefix in ("success:", "failure:", "alert:"):
                    self.store.db.execute("DELETE FROM meta WHERE key=?", (prefix + model,))
            self.menu(chat, now, "models")
        elif data in ("input:interval", "input:chats"):
            action = data.split(":")[1]
            self.store.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)", (chat, action, now + 600))
            self.reply(chat, "Введите число минут (1–1440)." if action == "interval" else
                       "Пришлите один или несколько числовых chat ID через пробел или запятую.\n"
                       "Личный чат должен сначала открыть бота; для группы/канала боту нужны права отправки.", now,
                       [[("Отмена", "main")]])
        elif data == "add_me":
            self.add_chats([chat], now)
            self.menu(chat, now, "chats")
        elif data.startswith("remove:"):
            cid = parse_ids(data.split(":", 1)[1])[0]
            self.reply(chat, f"Удалить получателя {cid} и его ожидающие сообщения?", now,
                       [[("Удалить", f"delete:{cid}"), ("Отмена", "chats")]])
        elif data.startswith("delete:"):
            cid = parse_ids(data.split(":", 1)[1])[0]
            self.store.db.execute("DELETE FROM recipients WHERE chat_id=?", (cid,))
            self.store.db.execute("DELETE FROM watches WHERE chat_id=?", (cid,))
            self.store.db.execute("DELETE FROM outbox WHERE chat_id=?", (cid,))
            self.menu(chat, now, "chats")
        elif data == "test":
            self.store.enqueue("✅ Тест уведомлений OVH для этого получателя.", now)
            self.reply(chat, f"Тест поставлен в очередь для {len(self.store.recipients())} чатов. Это ещё не подтверждение доставки.", now)
        else:
            raise ValueError("Кнопка неизвестна. Откройте /admin заново.")

    def message(self, chat, text, now):
        if text in ("/start", "/admin", "/cancel"):
            self.store.db.execute("DELETE FROM sessions WHERE admin_id=?", (chat,))
            self.menu(chat, now)
        elif text == "/id":
            self.reply(chat, f"Ваш Telegram ID: {chat}", now)
        else:
            session = self.store.db.execute("SELECT * FROM sessions WHERE admin_id=?", (chat,)).fetchone()
            if not session or session["expires"] < now:
                self.reply(chat, "Откройте /admin для настройки.", now)
                return
            if session["action"] == "interval":
                self.set_interval(text.strip())
                page = "interval"
            else:
                self.add_chats(parse_ids(text), now)
                page = "chats"
            self.store.db.execute("DELETE FROM sessions WHERE admin_id=?", (chat,))
            self.menu(chat, now, page)


def poll_updates(token, admin, stop):
    while not stop.is_set():
        try:
            updates = telegram_api(token, "getUpdates", {
                "offset": int(admin.store.get("update_offset", 0)), "timeout": 20,
                "allowed_updates": ["message", "callback_query"],
            }, timeout=30)
            if not isinstance(updates, list): raise RemoteError("Invalid Telegram updates")
            for update in updates:
                if stop.is_set(): break
                callback_id = admin.process(update)
                if callback_id:
                    try: telegram_api(token, "answerCallbackQuery", {"callback_query_id": callback_id})
                    except RemoteError: pass
            with admin.store.db: admin.store.put("admin_poll", time.time())
        except RemoteError as exc:
            LOG.warning("Admin polling failed: %s", exc)
            stop.wait(max(5, exc.retry_after))
