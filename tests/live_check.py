"""Opt-in: real OVH VPS-1, temporary SQLite, local Telegram HTTP substitute."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor as m


def main():
    model = "vps-2027-model1"
    report = {"model": model, "started_utc": m.utc(time.time()),
              "ovh_source": m.OVH_URL, "real_ovh": True,
              "real_telegram": False, "telegram_destination": "local HTTP substitute"}

    class Telegram(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.server.fail_once:
                self.server.fail_once = False
                self.send_response(500)
                body = {"ok": False}
            else:
                self.server.messages.append(message)
                self.send_response(200)
                body = {"ok": True, "result": {"message_id": len(self.server.messages)}}
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Telegram)
    server.messages, server.fail_once = [], True
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    real_request = m.request_json

    def redirected_request(url, payload=None, timeout=10):
        if url.startswith("https://api.telegram.org/botLOCAL-TEST/"):
            url = f"http://127.0.0.1:{server.server_port}/sendMessage"
        return real_request(url, payload, timeout)

    def send(chat, text, markup):
        with patch.object(m, "request_json", side_effect=redirected_request):
            m.send_telegram("LOCAL-TEST", "local-test-chat", text, markup)

    def queued(store):
        return store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    try:
        with tempfile.TemporaryDirectory(prefix="ovh-live-") as temp:
            path = Path(temp) / "live.sqlite3"
            first = m.fetch_stock(model)
            available = [dc for dc, status in first.items() if status == "available"]
            if not available:
                raise RuntimeError("VPS-1 has no available Linux DC now; positive live check inconclusive")
            report["first_observation"] = {"utc": m.utc(time.time()), "statuses": first}
            store = m.Store(path)
            store.bootstrap(["local-test-chat"])
            try:
                store.observe(model, first, time.time())
                assert queued(store) == 1
                store.observe(model, first, time.time())
                assert queued(store) == 1, "Duplicate notifications"
                assert not store.deliver_one(send, time.time()), "Expected simulated HTTP 500"
                assert queued(store) == 1, "Failure lost an event"
                retry_at = store.db.execute("SELECT next_try FROM outbox ORDER BY id LIMIT 1").fetchone()[0]
            finally:
                store.close()
            # A real pause also lets the retry deadline elapse before reopening SQLite.
            time.sleep(15)
            second = m.fetch_stock(model)
            assert second == first, "Stock changed between live polls; rerun the stable-stock scenario"
            report["second_observation"] = {"utc": m.utc(time.time()), "statuses": second}
            added = sum(status == "available" and first[dc] != "available" for dc, status in second.items())
            store = m.Store(path)
            store.bootstrap(["local-test-chat"])
            try:
                assert queued(store) == 1, "Restart lost events"
                assert time.time() >= retry_at
                store.observe(model, second, time.time())
                assert queued(store) == 1
                while store.deliver_one(send, time.time()):
                    pass
                assert queued(store) == 0
                assert len(server.messages) == 1
                assert all(model in msg["text"] and msg["disable_notification"] is False for msg in server.messages)
                assert all(m.DCS[dc] in server.messages[0]["text"] for dc in available)
                assert server.messages[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("seen:")
                report.update({"result": "PASS", "initial_available_count": len(available),
                               "new_appearances_second_poll": added,
                               "duplicate_notifications": 0, "queue_after_delivery": queued(store),
                               "simulated_http_500_retained_queue": True,
                               "reopened_database_retained_queue": True,
                               "locally_received_notifications": server.messages})
            finally:
                store.close()
    finally:
        server.shutdown()
        worker.join()
        server.server_close()
    output = Path(__file__).resolve().parents[1] / "live-test-result.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "locally_received_notifications"}, indent=2))
    print("Report:", output)


if __name__ == "__main__":
    main()
