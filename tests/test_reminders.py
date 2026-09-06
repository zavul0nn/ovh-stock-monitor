import json
from pathlib import Path
import tempfile
import unittest

import admin as a
import monitor as m


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db"
        self.store = m.Store(self.path)
        self.store.bootstrap(["10", "20"])
        self.model = m.MODELS[0]
        self.values = {dc: "out-of-stock" for dc in m.DCS}
        self.dc, self.dc2 = list(m.DCS)[:2]
        self.sent = []

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def observe(self, now=1000, dcs=None):
        for dc in (dcs if dcs is not None else [self.dc]): self.values[dc] = "available"
        self.store.observe(self.model, self.values, now)

    def drain(self, now):
        while self.store.deliver_one(lambda chat, text, markup: self.sent.append((chat, text, markup)), now): pass

    def count(self):
        return self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def current_alert(self):
        return self.store.db.execute("SELECT MAX(id) FROM alerts").fetchone()[0]

    def ack(self, alert=None, chat="10"):
        with self.store.db: return self.store.acknowledge(alert or self.current_alert(), chat)

    def test_initial_locations_grouped_per_model_with_button(self):
        self.observe(dcs=list(m.DCS))
        self.assertEqual(self.count(), 2)
        self.drain(1000)
        for chat, text, markup in self.sent:
            for dc in m.DCS: self.assertIn(m.DCS[dc], text)
            self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "seen:1")

    def test_every_60_seconds_until_ack(self):
        self.observe(); self.drain(1000)
        self.store.queue_reminders(1059); self.assertEqual(self.count(), 0)
        self.store.queue_reminders(1060); self.assertEqual(self.count(), 2)
        self.drain(1060)
        self.store.queue_reminders(1120); self.assertEqual(self.count(), 2)
        self.ack(chat="10"); self.ack(chat="20")
        self.assertEqual(self.count(), 0)
        self.store.queue_reminders(2000); self.assertEqual(self.count(), 0)

    def test_ack_is_per_chat(self):
        self.observe(); self.drain(1000); self.ack(chat="10")
        self.store.queue_reminders(1060); self.drain(1060)
        self.assertEqual([x[0] for x in self.sent], ["10", "20", "20"])

    def test_missing_stock_once_even_after_ack(self):
        self.observe(); self.drain(1000); self.ack(chat="10"); self.ack(chat="20")
        self.values[self.dc] = "out-of-stock"
        self.store.observe(self.model, self.values, 1100)
        self.assertEqual(self.count(), 2)
        self.drain(1100)
        self.assertTrue(all("пропал" in x[1] and x[2] is None for x in self.sent[-2:]))
        self.store.observe(self.model, self.values, 1200)
        self.store.queue_reminders(2000)
        self.assertEqual(self.count(), 0)

    def test_disappearance_cancels_pending_reminders(self):
        self.observe(); self.drain(1000)
        self.store.queue_reminders(1060)
        self.values[self.dc] = "out-of-stock"
        self.store.observe(self.model, self.values, 1061)
        self.assertEqual(self.count(), 2)
        self.assertTrue(all(row[0] is None for row in self.store.db.execute("SELECT alert_id FROM outbox")))

    def test_reappearance_gets_new_button_old_ack_cannot_silence(self):
        self.observe(); old = self.current_alert(); self.drain(1000)
        self.values[self.dc] = "out-of-stock"
        self.store.observe(self.model, self.values, 1100); self.drain(1100)
        self.observe(1200); new = self.current_alert()
        self.assertNotEqual(old, new)
        self.assertFalse(self.ack(old))
        self.assertEqual(self.count(), 2)

    def test_restart_keeps_ack_and_reminder_deadline(self):
        self.observe(); self.drain(1000); self.ack(chat="10")
        self.store.close(); self.store = m.Store(self.path)
        self.store.queue_reminders(1059); self.assertEqual(self.count(), 0)
        self.store.queue_reminders(1060); self.drain(1060)
        self.assertEqual(self.sent[-1][0], "20")

    def test_other_tariff_continues_after_ack(self):
        self.observe(); self.drain(1000); self.ack(chat="10"); self.ack(chat="20")
        self.store.observe(m.MODELS[1], self.values, 1100)
        self.assertEqual(self.count(), 2)
        self.drain(1100)
        self.assertIn(m.MODELS[1], self.sent[-1][1])

    def test_partial_disappearance_keeps_only_remaining_in_reminder(self):
        self.observe(dcs=[self.dc, self.dc2]); self.drain(1000)
        self.values[self.dc] = "out-of-stock"
        self.store.observe(self.model, self.values, 1030); self.drain(1030)
        self.store.queue_reminders(1060); self.drain(1060)
        self.assertIn(m.DCS[self.dc2], self.sent[-1][1])
        self.assertNotIn(m.DCS[self.dc], self.sent[-1][1])

    def test_new_location_after_ack_starts_separate_cycle(self):
        self.observe(); self.drain(1000); self.ack(chat="10"); self.ack(chat="20")
        self.observe(1100, [self.dc2]); self.drain(1100)
        self.assertIn(m.DCS[self.dc2], self.sent[-1][1])
        self.assertNotIn(m.DCS[self.dc], self.sent[-1][1])

    def test_no_backlog_of_identical_reminders_on_delivery_failure(self):
        self.observe()
        def fail(*_): raise m.RemoteError("HTTP 500")
        self.store.deliver_one(fail, 1000)
        for now in (1060, 1120, 1180, 2000): self.store.queue_reminders(now)
        self.assertEqual(self.count(), 2)

    def test_disabled_model_stops_reminders(self):
        self.observe(); self.drain(1000)
        with self.store.db: self.store.stop_alerts(self.model)
        self.store.queue_reminders(2000); self.assertEqual(self.count(), 0)

    def test_private_nonadmin_recipient_can_ack_own_chat(self):
        self.observe(); self.drain(1000)
        control = a.Admin(self.store, ["10"])
        control.process({"update_id": 1, "callback_query": {"id": "cb", "from": {"id": 20},
            "data": "seen:1", "message": {"chat": {"id": 20, "type": "private"}}}}, 1010)
        row = self.store.db.execute("SELECT acknowledged FROM watches WHERE chat_id='20'").fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(self.store.db.execute("SELECT acknowledged FROM watches WHERE chat_id='10'").fetchone()[0], 0)

    def test_forwarded_button_cannot_ack_original_chat(self):
        self.observe(); self.drain(1000)
        control = a.Admin(self.store, ["10"])
        control.process({"update_id": 1, "callback_query": {"id": "cb", "from": {"id": 30},
            "data": "seen:1", "message": {"chat": {"id": 30, "type": "private"}}}}, 1010)
        self.assertEqual(self.store.db.execute("SELECT SUM(acknowledged) FROM watches").fetchone()[0], 0)

    def test_preorder_is_disappearance(self):
        self.observe(); self.drain(1000)
        self.values[self.dc] = "out-of-stock-preorder-allowed"
        self.store.observe(self.model, self.values, 1100); self.drain(1100)
        self.assertIn("пропал", self.sent[-1][1])


if __name__ == "__main__": unittest.main()
