import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo


class BookingFlowTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_PATH": self.directory.name + "/bookings.sqlite3",
            "ADMIN_PASSWORD": "test-admin-password",
            "ADMIN_EMAIL": "coach@example.com",
            "MANAGE_LINK_SECRET": "test-manage-secret",
            "STRIPE_SECRET_KEY": "sk_test_placeholder",
            "STRIPE_WEBHOOK_SECRET": "whsec_test_placeholder",
            "PUBLIC_BASE_URL": "http://localhost",
            "ALLOW_LOCAL_FLASK_SQLITE": "1",
        })
        self.environment.start()
        import app as booking_app
        self.booking_app = booking_app
        booking_app.initialize()
        self.client = booking_app.app.test_client()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_venmo_request_needs_coach_payment_confirmation(self):
        with patch.dict(os.environ, {"PAYMENT_METHOD": "venmo", "VENMO_USERNAME": "Dacialaine"}):
            config = self.client.get("/api/config").json
            self.assertTrue(config["payments_ready"])
            self.assertEqual(config["venmo_url"], "https://venmo.com/u/Dacialaine")
            self.client.post("/api/admin/login", json={"email": "coach@example.com", "password": "test-admin-password"})
            day = (datetime.now(ZoneInfo("America/Los_Angeles")).date() + timedelta(days=3)).isoformat()
            self.assertEqual(self.client.post("/api/admin/availability", json={"specific_date": day, "start_time": "10:00", "end_time": "11:00"}).status_code, 200)
            slot = next(s for s in self.client.get("/api/slots?date=" + day).json["slots"] if s["label"] == "10:00 AM")
            details = {"parent": "Example Parent", "gymnast": "Example Gymnast", "email": "parent@example.com", "phone": "555-0100", "start_at": slot["start_at"], "minutes": 30, "policy_version": config["policy_version"], "agree": True}
            self.assertEqual(self.client.post("/api/checkout", json={**details, "agree": False}).status_code, 400)
            response = self.client.post("/api/checkout", json=details)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json["status"], "pending")
            self.assertEqual(response.json["amount_cents"], 4000)
            self.assertNotIn(slot["start_at"], [s["start_at"] for s in self.client.get("/api/slots?date=" + day).json["slots"]])
            booking_id = response.json["reference"]
            with self.booking_app.db() as connection:
                self.assertEqual(connection.execute("SELECT status FROM bookings WHERE id=?", (booking_id,)).fetchone()[0], "pending")
            self.assertEqual(self.client.post("/api/admin/booking", json={"id": booking_id, "action": "confirm_payment"}).status_code, 200)
            with self.booking_app.db() as connection:
                self.assertEqual(connection.execute("SELECT status FROM bookings WHERE id=?", (booking_id,)).fetchone()[0], "confirmed")
                self.assertEqual(connection.execute("SELECT status FROM payments WHERE booking_id=?", (booking_id,)).fetchone()[0], "paid")

    def test_payment_and_parent_changes(self):
        app = self.booking_app
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/book").status_code, 200)
        config = self.client.get("/api/config").json
        self.assertEqual([(t["minutes"], t["price_cents"]) for t in config["lesson_types"]], [(30, 4000), (40, 5300)])

        self.assertEqual(self.client.post("/api/admin/login", json={"email": "wrong@example.com", "password": "test-admin-password"}).status_code, 401)
        login = self.client.post("/api/admin/login", json={"email": "coach@example.com", "password": "test-admin-password"})
        self.assertEqual(login.status_code, 200)
        local_day = datetime.now(ZoneInfo("America/Los_Angeles")).date() + timedelta(days=4)
        day = local_day.isoformat()
        self.assertEqual(self.client.post("/api/admin/availability", json={"specific_date": day, "start_time": "10:00", "end_time": "12:00"}).status_code, 200)
        slots = self.client.get("/api/slots?date=" + day).json["slots"]
        first = next(s for s in slots if s["label"] == "10:00 AM")
        later = next(s for s in slots if s["label"] == "11:00 AM")

        details = {"parent": "Example Parent", "gymnast": "Example Gymnast", "email": "parent@example.com", "phone": "555-0100", "start_at": first["start_at"], "minutes": 30, "policy_version": config["policy_version"]}
        self.assertEqual(self.client.post("/api/checkout", json={**details, "agree": False}).status_code, 400)
        with patch.object(app, "stripe_post", return_value={"id": "cs_test_example", "url": "https://checkout.stripe.com/test"}):
            checkout = self.client.post("/api/checkout", json={**details, "agree": True})
        self.assertEqual(checkout.status_code, 200, checkout.json)
        with app.db() as connection:
            booking = connection.execute("SELECT id,status FROM bookings").fetchone()
        self.assertEqual(booking["status"], "pending")
        self.assertEqual(self.client.post("/api/admin/booking", json={"id": booking["id"], "action": "confirm_payment"}).status_code, 400)
        self.assertEqual(self.client.post("/api/checkout", json={**details, "agree": True}).status_code, 400)

        event = {"id": "evt_test_example", "type": "checkout.session.completed", "data": {"object": {"id": "cs_test_example", "client_reference_id": booking["id"], "payment_status": "paid", "amount_total": 4000, "currency": "usd", "payment_intent": "pi_test_example"}}}
        raw = json.dumps(event).encode()
        timestamp = "2000000000"
        signature = hmac.new(os.environ["STRIPE_WEBHOOK_SECRET"].encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        with patch.object(app.time, "time", return_value=int(timestamp)):
            for _ in range(2):
                response = self.client.post("/api/stripe/webhook", data=raw, headers={"Stripe-Signature": f"t={timestamp},v1={signature}"})
                self.assertEqual(response.status_code, 200, response.json)
        with app.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM bookings WHERE id=?", (booking["id"],)).fetchone()[0], "confirmed")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0], 1)

        token = app.manage_token(booking["id"])
        self.assertTrue(self.client.get("/api/manage?token=" + token).json["can_change"])
        self.assertEqual(self.client.post("/api/manage/change", json={"token": token, "action": "reschedule", "start_at": later["start_at"]}).status_code, 200)
        self.assertEqual(self.client.post("/api/manage/change", json={"token": token, "action": "cancel"}).status_code, 200)
        self.assertEqual(self.client.get("/api/manage?token=" + token).json["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
