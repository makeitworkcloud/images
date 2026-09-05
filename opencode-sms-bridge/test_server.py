import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image
from twilio.request_validator import RequestValidator

from server import (
    BridgeError,
    OpenCodeClient,
    Routing,
    SQLiteStore,
    Settings,
    UnsupportedMedia,
    create_ingress_app,
    load_routing,
    normalize_e164,
    sanitize_image,
    sender_hash,
)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key()
        self.routing = Routing(
            account_sid="AC1234567890",
            approved_senders=frozenset({"+15559999999"}),
            channels={
                "+15550000001": "lawnmowerman",
                "+15550000002": "grillmaster",
                "+15550000003": "homesteader",
                "+15550000004": "homerepair",
            },
        )
        self.settings = Settings(
            mode="ingress",
            routing=self.routing,
            state_path=Path(self.tempdir.name) / "state.db",
            state_key=self.key,
            sender_hash_key=b"sender-hash-key",
            canonical_webhook_url="https://sms.example.invalid/twilio/inbound",
            twilio_auth_token="auth-token",
            media_allowed_hosts=frozenset({"api.twilio.com"}),
            max_media_bytes=1024 * 1024,
            max_audio_seconds=120,
            image_parts_enabled=False,
            opencode_base_url="",
            opencode_username="opencode",
            opencode_password="",
            opencode_timeout_seconds=120,
            twilio_api_key_sid="",
            twilio_api_key_secret="",
            whisper_url="",
            whisper_model="base",
        )
        self.store = SQLiteStore(self.settings.state_path, self.key)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_e164_rejects_noncanonical_values(self):
        self.assertEqual(normalize_e164("+15551234567"), "+15551234567")
        for value in ("15551234567", "+1 555 123 4567", "+abc"):
            with self.assertRaises(Exception):
                normalize_e164(value)

    def test_routing_requires_the_existing_primary_agents(self):
        routing_path = Path(self.tempdir.name) / "routing.json"
        payload = {
            "accountSid": "AC1234567890",
            "approvedSenders": ["+15559999999"],
            "channels": {
                "+15550000001": {"agent": "lawnmowerman"},
                "+15550000002": {"agent": "grillmaster"},
                "+15550000003": {"agent": "homesteader"},
                "+15550000004": {"agent": "homerepair"},
            },
        }
        routing_path.write_text(json.dumps(payload))
        self.assertEqual(load_routing(routing_path).channels, self.routing.channels)

        payload["channels"]["+15550000001"]["agent"] = "lawnmowerman-sms"
        routing_path.write_text(json.dumps(payload))
        with self.assertRaises(BridgeError):
            load_routing(routing_path)

    def test_queue_is_deduplicated_and_payload_is_encrypted(self):
        payload = {"from": "+15559999999", "to": "+15550000001", "body": "confidential body", "media": [], "agent": "lawnmowerman"}
        identifier = sender_hash(self.settings.sender_hash_key, payload["from"])
        self.assertTrue(self.store.enqueue("SM123", "lawnmowerman", identifier, payload))
        self.assertFalse(self.store.enqueue("SM123", "lawnmowerman", identifier, payload))
        with sqlite3.connect(self.settings.state_path) as connection:
            stored = connection.execute("SELECT payload FROM jobs").fetchone()[0]
        self.assertNotIn(b"confidential body", stored)
        claimed = self.store.claim()
        self.assertEqual(claimed["payload"]["body"], "confidential body")

    def test_ingress_accepts_signed_approved_message_once(self):
        form = {"AccountSid": "AC1234567890", "MessageSid": "SM123", "From": "+15559999999", "To": "+15550000001", "Body": "hello", "NumMedia": "0"}
        signature = RequestValidator("auth-token").compute_signature(self.settings.canonical_webhook_url, form)
        client = TestClient(create_ingress_app(self.settings, self.store))
        headers = {"X-Twilio-Signature": signature}
        with self.assertLogs("opencode-sms-bridge", level="INFO") as captured:
            self.assertEqual(client.post("/twilio/inbound", data=form, headers=headers).status_code, 200)
        telemetry = "\n".join(captured.output)
        self.assertIn("event=inbound_queued channel=lawnmowerman media_count=0", telemetry)
        for unsafe_value in (form["From"], form["To"], form["MessageSid"], form["Body"]):
            self.assertNotIn(unsafe_value, telemetry)
        self.assertEqual(client.post("/twilio/inbound", data=form, headers=headers).status_code, 200)
        self.assertIsNotNone(self.store.claim())
        self.assertIsNone(self.store.claim())

    def test_ingress_ignores_unapproved_sender_before_queueing(self):
        form = {"AccountSid": "AC1234567890", "MessageSid": "SM124", "From": "+15558888888", "To": "+15550000001", "Body": "hello", "NumMedia": "0"}
        signature = RequestValidator("auth-token").compute_signature(self.settings.canonical_webhook_url, form)
        client = TestClient(create_ingress_app(self.settings, self.store))
        with patch.object(self.store, "enqueue", wraps=self.store.enqueue) as enqueue:
            with self.assertLogs("opencode-sms-bridge", level="INFO") as captured:
                response = client.post("/twilio/inbound", data=form, headers={"X-Twilio-Signature": signature})
        telemetry = "\n".join(captured.output)
        self.assertEqual(response.status_code, 200)
        self.assertIn("event=inbound_ignored reason=account-destination-or-sender", telemetry)
        for unsafe_value in (form["From"], form["To"], form["MessageSid"], form["Body"]):
            self.assertNotIn(unsafe_value, telemetry)
        enqueue.assert_not_called()
        self.assertIsNone(self.store.claim())

    def test_prompt_uses_v2_admission_wait_and_message_flow(self):
        client = OpenCodeClient(self.settings)
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": {"id": "in_123"}},
                {},
                {"data": [{"type": "assistant", "content": [{"type": "text", "text": "reply"}]}]},
            ],
        ) as request:
            self.assertEqual(client.prompt("ses_123", [{"type": "text", "text": "hello"}]), "reply")
        request.assert_has_calls(
            [
                call("/api/session/ses_123/prompt", {"prompt": {"text": "hello"}}),
                call("/api/session/ses_123/wait"),
                call("/api/session/ses_123/message?order=desc&limit=200", method="GET"),
            ]
        )
        self.assertEqual(request.call_count, 3)

    def test_prompt_rejects_unmapped_file_parts(self):
        client = OpenCodeClient(self.settings)
        with self.assertRaises(UnsupportedMedia):
            client.prompt("ses_123", [{"type": "file", "mime": "image/png", "filename": "image", "url": "data:image/png;base64,"}])

    def test_image_sanitization_removes_exif(self):
        image = Image.new("RGB", (8, 8), color="red")
        original = tempfile.SpooledTemporaryFile()
        image.save(original, "JPEG")
        original.seek(0)
        sanitized, mime = sanitize_image(original.read(), "image/jpeg")
        self.assertEqual(mime, "image/jpeg")
        reopened = Image.open(__import__("io").BytesIO(sanitized))
        self.assertFalse(reopened.getexif())
        self.assertTrue(base64.b64encode(sanitized))


if __name__ == "__main__":
    unittest.main()
