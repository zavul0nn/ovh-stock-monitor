import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import monitor as m


def payload(status="out-of-stock"):
    return {"datacenters": [dict(code=dc, linuxStatus=status, status="available", windowsStatus="available") for dc in m.DCS]}


class ParserTests(unittest.TestCase):
    def test_windows_and_general_available_are_ignored(self):
        self.assertEqual(set(m.parse_stock(payload()).values()), {"out-of-stock"})

    def test_available(self):
        self.assertEqual(len(m.parse_stock(payload("available"))), 7)

    def test_preorder_is_not_available(self):
        self.assertEqual(set(m.parse_stock(payload("out-of-stock-preorder-allowed")).values()), {"out-of-stock-preorder-allowed"})

    def test_missing_dc_rejected(self):
        data = payload()
        data["datacenters"].pop()
        with self.assertRaises(m.RemoteError): m.parse_stock(data)

    def test_missing_linux_status_rejected(self):
        data = payload()
        del data["datacenters"][0]["linuxStatus"]
        with self.assertRaises(m.RemoteError): m.parse_stock(data)

    def test_unknown_status_rejected(self):
        with self.assertRaises(m.RemoteError): m.parse_stock(payload("unknown"))

    def test_duplicate_rejected(self):
        data = payload()
        data["datacenters"].append(data["datacenters"][0])
        with self.assertRaises(m.RemoteError): m.parse_stock(data)

    def test_invalid_structures(self):
        for data in (None, [], {}, {"datacenters": []}, {"datacenters": [None]}, {"datacenters": [{"code": []}]}):
            with self.subTest(data=data), self.assertRaises(m.RemoteError): m.parse_stock(data)

    def test_non_europe_ignored(self):
        data = payload()
        data["datacenters"].append({"code": "ca-east-bhs", "linuxStatus": "available"})
        self.assertEqual(len(m.parse_stock(data)), 7)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = m.Store(self.path)
        self.store.bootstrap(["123"])
        self.model = m.MODELS[0]
        self.values = m.parse_stock(payload())
        self.dc = next(iter(m.DCS))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def count(self):
        return self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def available(self, now=1000):
        self.values[self.dc] = "available"
        self.store.observe(self.model, self.values, now)

    def test_initial_available_notifies(self):
        self.available()
        self.assertEqual(self.count(), 1)

    def test_initial_oos_silent(self):
        self.store.observe(self.model, self.values, 1000)
        self.assertEqual(self.count(), 0)

    def test_transition_only_no_spam(self):
        self.store.observe(self.model, self.values, 900)
        self.available()
        self.available(1100)
        self.assertEqual(self.count(), 1)

    def test_second_appearance_notifies(self):
        self.available()
        self.values[self.dc] = "out-of-stock"
        self.store.observe(self.model, self.values, 1100)
        self.available(1200)
        self.assertEqual(self.count(), 2)

    def test_preorder_notifies_only_when_available(self):
        self.values[self.dc] = "out-of-stock-preorder-allowed"
        self.store.observe(self.model, self.values, 900)
        self.assertEqual(self.count(), 0)
        self.available()
        self.assertEqual(self.count(), 1)

    def test_restart_retains_queue_and_deduplicates(self):
        self.available()
        self.store.close()
        self.store = m.Store(self.path)
        self.available(1100)
        self.assertEqual(self.count(), 1)
        messages = []
        self.assertTrue(self.store.deliver_one(lambda chat, text, markup: messages.append(text), 1200))
        self.assertEqual(self.count(), 0)
        self.assertIn(m.DCS[self.dc], messages[0])

    def test_delivery_failure_retries_after_restart(self):
        self.available()
        def fail(*_): raise m.RemoteError("HTTP 500")
        self.assertFalse(self.store.deliver_one(fail, 1000))
        self.store.close()
        self.store = m.Store(self.path)
        messages = []
        self.assertFalse(self.store.deliver_one(lambda chat, text, markup: messages.append(text), 1004))
        self.assertTrue(self.store.deliver_one(lambda chat, text, markup: messages.append(text), 1005))
        self.assertEqual(len(messages), 1)

    def test_rate_limit_blocks_following_messages(self):
        self.available()
        with self.store.db: self.store.enqueue("second", 1000)
        def fail(*_): raise m.RemoteError("HTTP 429", 123)
        self.store.deliver_one(fail, 1000)
        messages = []
        self.assertFalse(self.store.deliver_one(lambda chat, text, markup: messages.append(text), 1122))
        self.assertTrue(self.store.deliver_one(lambda chat, text, markup: messages.append(text), 1123))
        self.assertEqual(len(messages), 1)
        self.assertTrue(self.store.deliver_one(lambda chat, text, markup: messages.append(text), 1125))

    def test_disappeared_stock_replaces_pending_reminder(self):
        self.available()
        self.values[self.dc] = "out-of-stock"
        self.store.observe(self.model, self.values, 1100)
        messages = []
        self.store.deliver_one(lambda chat, text, markup: messages.append(text), 1200)
        self.assertIn("Обнаружено:", messages[0])
        self.assertIn("пропал", messages[0])

    def test_atomic_rollback_when_enqueue_fails(self):
        self.store.observe(self.model, self.values, 900)
        with patch.object(self.store, "enqueue", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError): self.available()
        row = self.store.db.execute("SELECT status FROM stock WHERE model=? AND dc=?", (self.model, self.dc)).fetchone()
        self.assertEqual(row[0], "out-of-stock")
        self.available(1100)
        self.assertEqual(self.count(), 1)

    def test_bad_source_does_not_reset_stock(self):
        self.available()
        self.store.failure(self.model, 1100, 180)
        self.available(1200)
        self.assertEqual(self.count(), 1)

    def test_alert_once_and_recovery(self):
        for now in (1000, 1100, 1180, 1300): self.store.failure(self.model, now, 180)
        self.assertEqual(self.count(), 1)
        self.store.observe(self.model, self.values, 1400)
        self.store.observe(self.model, self.values, 1500)
        self.assertEqual(self.count(), 2)

    def test_one_model_failure_does_not_block_others(self):
        self.store.failure(m.MODELS[1], 900, 180)
        self.available()
        self.assertEqual(self.count(), 1)

    def test_health_checks_sources_sender_and_queue(self):
        self.assertFalse(self.store.healthy(1000, 300))
        for model in m.MODELS: self.store.observe(model, self.values, 1000)
        with self.store.db: self.store.put("sender", 1000)
        self.assertTrue(self.store.healthy(1100, 300))
        self.assertFalse(self.store.healthy(1400, 300))
        with self.store.db: self.store.enqueue("old", 100)
        self.assertFalse(self.store.healthy(1100, 300))

    def test_corrupt_db_fails_instead_of_resetting(self):
        path = Path(self.temp.name) / "bad.sqlite3"
        path.write_text("corrupted")
        with self.assertRaises(m.sqlite3.DatabaseError): m.Store(path)

    def test_concurrent_sender_and_observer(self):
        with concurrent_pool() as pool:
            def send():
                other = m.Store(self.path)
                try:
                    for i in range(30): other.deliver_one(lambda *_: None, 1000 + i)
                finally: other.close()
            task = pool.submit(send)
            for i in range(30):
                self.values[self.dc] = "available" if i % 2 == 0 else "out-of-stock"
                self.store.observe(self.model, self.values, 1000 + i)
            task.result()
        rows = self.store.db.execute("PRAGMA integrity_check").fetchone()
        self.assertEqual(rows[0], "ok")


def concurrent_pool():
    return m.concurrent.futures.ThreadPoolExecutor(max_workers=1)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                if self.path.startswith("/stock?"):
                    query = m.urllib.parse.parse_qs(m.urllib.parse.urlparse(self.path).query)
                    body = payload("available") if query.get("planCode") == [m.MODELS[0]] else {}
                    self.send_response(200); self.end_headers()
                    self.wfile.write(json.dumps(body).encode())
                elif self.path == "/bad":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"<html>maintenance</html>")
                else:
                    self.send_response(503); self.end_headers()
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.messages.append(body)
                code, data = self.server.reply
                self.send_response(code); self.end_headers(); self.wfile.write(json.dumps(data).encode())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.messages = []
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.thread.join(); cls.server.server_close()

    def test_real_http_ovh_query_and_parse(self):
        with patch.object(m, "OVH_URL", self.url + "/stock"):
            self.assertEqual(set(m.fetch_stock(m.MODELS[0]).values()), {"available"})

    def test_html_rejected(self):
        with self.assertRaises(m.RemoteError): m.request_json(self.url + "/bad")

    def test_http_failure_sanitized(self):
        with self.assertRaises(m.RemoteError) as exc: m.request_json(self.url + "/SECRET")
        self.assertEqual(str(exc.exception), "HTTP 503")

    def send(self):
        real_request = m.request_json
        with patch.object(m, "request_json", side_effect=lambda url, payload, **kwargs: real_request(self.url + "/telegram", payload, **kwargs)):
            m.send_telegram("SECRET", "123", "test")

    def test_telegram_success_and_audible_message(self):
        self.server.reply = (200, {"ok": True, "result": {"message_id": 1}})
        self.send()
        self.assertFalse(self.server.messages[-1]["disable_notification"])
        self.assertEqual(self.server.messages[-1]["chat_id"], "123")

    def test_telegram_200_ok_false_is_failure(self):
        self.server.reply = (200, {"ok": False})
        with self.assertRaises(m.RemoteError): self.send()

    def test_telegram_429_retry_after(self):
        self.server.reply = (429, {"ok": False, "parameters": {"retry_after": 42}})
        with self.assertRaises(m.RemoteError) as exc: self.send()
        self.assertEqual(exc.exception.retry_after, 42)

    def test_telegram_403_keeps_safe_error(self):
        self.server.reply = (403, {"ok": False, "description": "SECRET"})
        with self.assertRaises(m.RemoteError) as exc: self.send()
        self.assertNotIn("SECRET", str(exc.exception))

    def test_network_timeout(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("SECRET")):
            with self.assertRaises(m.RemoteError) as exc: m.request_json(self.url)
            self.assertEqual(str(exc.exception), "TimeoutError")

    def test_end_to_end_http_transition_retry_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "db"
            store = m.Store(path)
            store.bootstrap(["123"])
            try:
                store.observe(m.MODELS[0], m.parse_stock(payload()), 900)
                with patch.object(m, "OVH_URL", self.url + "/stock"):
                    store.observe(m.MODELS[0], m.fetch_stock(m.MODELS[0]), 1000)
                self.server.reply = (500, {"ok": False})
                self.assertFalse(store.deliver_one(lambda *_: self.send(), 1000))
            finally: store.close()
            store = m.Store(path)
            try:
                self.server.reply = (200, {"ok": True})
                sent = 0
                while store.deliver_one(lambda *_: self.send(), 1005): sent += 1
                self.assertEqual(sent, 1)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
            finally: store.close()


class RunnerTests(unittest.TestCase):
    def test_run_polls_all_models_sends_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "db"
            handlers, messages, checked = {}, [], []
            def fetch(model, subsidiary):
                checked.append((model, subsidiary))
                values = m.parse_stock(payload())
                if model == m.MODELS[0]: values[next(iter(m.DCS))] = "available"
                return values
            def send(token, chat, message, markup=None):
                messages.append(message)
                if len(messages) == 2: handlers[m.signal.SIGTERM]()
            def timeout():
                if m.signal.SIGTERM in handlers: handlers[m.signal.SIGTERM]()
            timer = threading.Timer(8, timeout)
            timer.start()
            try:
                with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "1", "TELEGRAM_ADMIN_IDS": "1"}), \
                     patch.object(m.signal, "signal", side_effect=lambda sig, handler: handlers.update({sig: handler})), \
                     patch("admin.poll_updates", side_effect=lambda token, admin, stop: stop.wait()), \
                     patch.object(m, "fetch_stock", side_effect=fetch), \
                     patch.object(m, "send_telegram", side_effect=send):
                    m.run(path)
            finally:
                timer.cancel(); timer.join()
            self.assertEqual({x[0] for x in checked}, set(m.MODELS))
            self.assertEqual(len(messages), 2)
            self.assertIn("Linux VPS доступен", messages[1])
            store = m.Store(path)
            try:
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
                with store.db: store.put("admin_poll", m.time.time())
                self.assertTrue(store.healthy(m.time.time(), 300))
            finally: store.close()

    def test_double_instance_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "db"
            with m.instance_lock(path):
                with self.assertRaises(OSError):
                    with m.instance_lock(path): pass
            with m.instance_lock(path): pass

    def test_missing_credentials_fails_before_start(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError): m.run("unused")

    def test_interval_validation(self):
        with patch.dict(os.environ, {"POLL_INTERVAL_SECONDS": "0"}):
            with self.assertRaises(ValueError): m.positive_env("POLL_INTERVAL_SECONDS", 60, 15)


if __name__ == "__main__": unittest.main()
