import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import admin as a
import monitor as m


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db"
        self.store = m.Store(self.path)
        self.store.bootstrap(["10", "20"])
        self.admin = a.Admin(self.store, ["10", "11"])
        self.update_id = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def update(self, action=None, text=None, user=10, chat=None, kind="private", update_id=None):
        self.update_id += 1
        msg = {"chat": {"id": user if chat is None else chat, "type": kind}, "from": {"id": user}, "text": text}
        result = {"update_id": self.update_id if update_id is None else update_id}
        if action is None: result["message"] = msg
        else: result["callback_query"] = {"id": str(self.update_id), "from": {"id": user}, "message": msg, "data": action}
        self.admin.process(result, 1000)
        return result

    def test_private_admin_gets_menu(self):
        self.update(text="/admin")
        row = self.store.db.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual(row["chat_id"], "10")
        self.assertIn("inline_keyboard", json.loads(row["markup"]))

    def test_unauthorized_message_and_callback_cannot_mutate(self):
        self.update(text="/admin", user=30)
        self.update(action="minutes:10", user=30)
        self.update(action="delete:20", user=30)
        self.assertEqual(self.store.config()["interval"], 60)
        self.assertEqual(self.store.recipients(), ["10", "20"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_admin_group_callback_is_rejected(self):
        self.update(action="minutes:10", chat=-100, kind="supergroup")
        self.assertEqual(self.store.config()["interval"], 60)

    def test_callback_sender_not_message_sender_controls_auth(self):
        update = {"update_id": 1, "callback_query": {"id": "cb", "from": {"id": 30},
                  "data": "minutes:10", "message": {"from": {"id": 10}, "chat": {"id": 10, "type": "private"}}}}
        self.admin.process(update, 1000)
        self.assertEqual(self.store.config()["interval"], 60)

    def test_custom_interval_survives_restart_and_env_bootstrap(self):
        self.update(action="input:interval")
        self.update(text="17")
        self.store.close()
        self.store = m.Store(self.path)
        self.store.bootstrap(["99"], 30)
        self.assertEqual(self.store.config()["interval"], 17 * 60)
        self.assertEqual(self.store.recipients(), ["10", "20"])

    def test_invalid_interval_keeps_setting_and_input_session(self):
        self.update(action="input:interval")
        for text in ("0", "-1", "1.5", "1441", "abc"):
            self.update(text=text)
            self.assertEqual(self.store.config()["interval"], 60)
        self.update(text="7")
        self.assertEqual(self.store.config()["interval"], 420)

    def test_multiple_ids_dedup_and_negative_ids(self):
        self.update(action="input:chats")
        self.update(text="30, -10012345 30;40")
        self.assertEqual(set(self.store.recipients()), {"10", "20", "30", "40", "-10012345"})

    def test_invalid_batch_does_not_partially_add(self):
        self.update(action="input:chats")
        self.update(text="30, invalid")
        self.assertNotIn("30", self.store.recipients())

    def test_sessions_are_per_admin(self):
        self.update(action="input:interval", user=10)
        self.update(action="input:chats", user=11)
        self.update(text="30", user=11)
        self.update(text="9", user=10)
        self.assertIn("30", self.store.recipients())
        self.assertEqual(self.store.config()["interval"], 540)

    def test_expired_session_cannot_change_settings(self):
        self.update(action="input:interval")
        self.admin.process({"update_id": 100, "message": {"from": {"id": 10}, "chat": {"id": 10, "type": "private"}, "text": "7"}}, 1700)
        self.assertEqual(self.store.config()["interval"], 60)

    def test_remove_confirmation_then_cancels_pending_only_for_target(self):
        with self.store.db: self.store.enqueue("stock", 900)
        self.update(action="remove:20")
        self.assertIn("20", self.store.recipients())
        self.update(action="delete:20")
        self.assertNotIn("20", self.store.recipients())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE chat_id='20'").fetchone()[0], 0)
        self.assertGreater(self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE chat_id='10'").fetchone()[0], 0)

    def test_selected_models_and_last_model_guard(self):
        self.update(action="model:1:1")
        self.assertIn(m.ALL_MODELS[0], self.store.config()["models"])
        for number in (2, 3, 4, 1): self.update(action=f"model:{number}:0")
        self.assertEqual(self.store.config()["models"], [m.ALL_MODELS[0]])

    def test_replayed_update_not_applied_twice(self):
        event = self.update(action="model:1:1")
        count = self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.admin.process(event, 1001)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], count)
        self.assertEqual(self.store.config()["revision"], 1)

    def test_stale_button_sets_state_instead_of_toggling(self):
        self.update(action="model:1:1")
        self.update(action="model:1:1")
        self.assertIn(m.ALL_MODELS[0], self.store.config()["models"])
        self.assertEqual(self.store.config()["revision"], 1)

    def test_disabled_inflight_model_cannot_emit(self):
        revision = self.store.config()["revision"]
        self.update(action="model:2:0")
        self.store.observe(m.MODELS[0], {dc: "available" for dc in m.DCS}, 1001, revision)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM stock").fetchone()[0], 0)

    def test_reenable_resets_baseline(self):
        self.store.observe(m.MODELS[0], {dc: "available" for dc in m.DCS}, 900)
        self.update(action="model:2:0")
        self.update(action="model:2:1")
        with self.store.db: self.store.db.execute("DELETE FROM outbox")
        self.store.observe(m.MODELS[0], {dc: "available" for dc in m.DCS}, 1001, self.store.config()["revision"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2)

    def test_menu_response_failure_rolls_back_settings_and_offset(self):
        with patch.object(self.store, "enqueue", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError): self.update(action="minutes:20")
        self.assertEqual(self.store.config()["interval"], 60)
        self.assertEqual(self.store.get("update_offset"), 0)

    def test_chat_pagination(self):
        with self.store.db: self.admin.add_chats([str(100+i) for i in range(25)], 900)
        self.update(action="chats:8")
        row = self.store.db.execute("SELECT markup FROM outbox ORDER BY id DESC").fetchone()
        buttons = [b for r in json.loads(row[0])["inline_keyboard"] for b in r]
        self.assertEqual(sum(b["callback_data"].startswith("remove:") for b in buttons), 8)
        self.assertTrue(any(b["callback_data"] == "chats:16" for b in buttons))

    def test_failure_one_chat_does_not_block_other(self):
        with self.store.db: self.store.enqueue("stock", 1000)
        sent = []
        def sender(chat, text, markup):
            if chat == "10": raise m.RemoteError("HTTP 403")
            sent.append(chat)
        self.assertFalse(self.store.deliver_one(sender, 1000))
        self.assertTrue(self.store.deliver_one(sender, 1001))
        self.assertEqual(sent, ["20"])
        self.assertEqual(self.store.db.execute("SELECT chat_id FROM outbox").fetchone()[0], "10")

    def test_only_failed_chat_retried_after_restart(self):
        with self.store.db: self.store.enqueue("stock", 1000)
        self.store.deliver_one(lambda *_: None, 1000)
        self.store.close()
        self.store = m.Store(self.path)
        chats = []
        self.store.deliver_one(lambda chat, *_: chats.append(chat), 1001)
        self.assertEqual(chats, ["20"])

    def test_long_interval_health_and_enabled_models(self):
        self.update(action="minutes:30")
        for model in m.MODELS: self.store.observe(model, {dc: "out-of-stock" for dc in m.DCS}, 1000)
        with self.store.db:
            self.store.db.execute("DELETE FROM outbox")
            self.store.put("sender", 2000)
        self.assertTrue(self.store.healthy(2000, 300))

    def test_interval_change_reschedules_without_restart(self):
        last = {model: 1000 for model in m.MODELS}
        self.update(action="minutes:30")
        self.assertEqual(m.due_models(self.store.config(), last, {}, 1100), [])
        self.update(action="minutes:1")
        self.assertEqual(m.due_models(self.store.config(), last, {}, 1100), list(m.MODELS))
        self.assertNotIn(m.MODELS[0], m.due_models(self.store.config(), last, {m.MODELS[0]: 1200}, 1100))

    def test_only_selected_tariffs_are_scheduled(self):
        self.update(action="model:2:0")
        self.update(action="model:1:1")
        self.assertEqual(set(m.due_models(self.store.config(), {}, {}, 1000)), {m.ALL_MODELS[0], m.MODELS[1], m.MODELS[2]})

    def test_status_lists_failed_recipients(self):
        with self.store.db: self.store.enqueue("stock", 900)
        def fail(*_): raise m.RemoteError("HTTP 403")
        self.store.deliver_one(fail, 1000)
        self.update(action="status")
        text = self.store.db.execute("SELECT message FROM outbox ORDER BY id DESC").fetchone()[0]
        self.assertIn("10: 1 сообщ.", text)

    def test_polling_uses_offset_and_acknowledges_callback(self):
        stop = threading.Event()
        update = {"update_id": 7, "callback_query": {"id": "cb", "from": {"id": 10},
                  "data": "minutes:5", "message": {"chat": {"id": 10, "type": "private"}}}}
        calls = []
        def api(token, method, payload, **kwargs):
            calls.append((method, payload))
            if method == "getUpdates":
                if len(calls) == 1: return [update]
                stop.set()
                return []
            return True
        with patch.object(a, "telegram_api", side_effect=api): a.poll_updates("test", self.admin, stop)
        self.assertEqual(self.store.config()["interval"], 300)
        self.assertEqual(calls[-1][1]["offset"], 8)
        self.assertEqual(calls[1][0], "answerCallbackQuery")


class MigrationTests(unittest.TestCase):
    def test_old_single_chat_queue_preserved_and_addressed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,message TEXT,created REAL,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0)")
            db.execute("INSERT INTO outbox(message,created) VALUES ('old pending event',1000)")
            db.commit(); db.close()
            store = m.Store(path)
            try:
                store.bootstrap(["10", "20"])
                rows = store.db.execute("SELECT message,chat_id FROM outbox").fetchall()
                self.assertEqual({tuple(row) for row in rows}, {("old pending event", "10"), ("old pending event", "20")})
                store.bootstrap(["30"])
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2)
            finally: store.close()


if __name__ == "__main__": unittest.main()
