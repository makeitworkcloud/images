import base64
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image
from twilio.request_validator import RequestValidator

from server import (
    BRIDGE_ERROR_CODES,
    BridgeError,
    OPENCODE_OPERATION_PROMPT,
    OPENCODE_OPERATION_SESSION_CREATE,
    OpenCodeClient,
    Routing,
    SQLiteStore,
    Settings,
    UnsupportedMedia,
    classify_request_failure,
    classify_response_error,
    create_ingress_app,
    load_routing,
    normalize_e164,
    opencode_request_error_code,
    opencode_response_error_code,
    process_job,
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

    def test_documented_flow_uses_exact_routes_methods_and_bodies(self):
        settings = replace(self.settings, opencode_base_url="https://opencode.example.invalid")
        client = OpenCodeClient(settings)
        session_body = json.dumps({"id": "ses_new"}).encode()
        message_body = json.dumps(
            {"info": {"id": "msg_1", "role": "assistant"}, "parts": [{"type": "text", "text": "reply"}]}
        ).encode()
        with patch("server.build_opener") as opener_factory:
            context = opener_factory.return_value.open.return_value.__enter__.return_value
            context.read.side_effect = [session_body, message_body]
            session_id = client.create_session()
            reply = client.prompt(session_id, "homesteader", [{"type": "text", "text": "hi"}])
        self.assertEqual(session_id, "ses_new")
        self.assertEqual(reply, "reply")
        open_mock = opener_factory.return_value.open
        self.assertEqual(open_mock.call_count, 2)
        requests = [call.args[0] for call in open_mock.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["POST", "POST"])
        self.assertEqual(
            [request.full_url for request in requests],
            [
                "https://opencode.example.invalid/session",
                "https://opencode.example.invalid/session/ses_new/message",
            ],
        )
        self.assertEqual(json.loads(requests[0].data.decode()), {})
        self.assertEqual(
            json.loads(requests[1].data.decode()),
            {"agent": "homesteader", "parts": [{"type": "text", "text": "hi"}]},
        )
        for request in requests:
            for unsupported in ("/api", "/prompt", "prompt_async", "/wait"):
                self.assertNotIn(unsupported, request.full_url)

    def test_session_create_sends_only_documented_fields(self):
        client = OpenCodeClient(self.settings)
        with patch.object(client, "_request", return_value={"id": "ses_123"}) as request:
            self.assertEqual(client.create_session(), "ses_123")
        request.assert_called_once_with("/session", {}, operation=OPENCODE_OPERATION_SESSION_CREATE)

    def test_session_create_rejects_invalid_response(self):
        client = OpenCodeClient(self.settings)
        for invalid in ({}, {"data": {"id": "ses_123"}}, "ses_123"):
            with self.subTest(invalid=invalid):
                with patch.object(client, "_request", return_value=invalid):
                    with self.assertRaises(BridgeError) as raised:
                        client.create_session()
                self.assertEqual(raised.exception.error_code, "opencode-response-invalid")
                self.assertNotIn("ses_123", str(raised.exception))

    def test_prompt_makes_single_blocking_message_request(self):
        client = OpenCodeClient(self.settings)
        completed = {"info": {"id": "msg_123", "role": "assistant"}, "parts": [{"type": "text", "text": "reply"}]}
        with patch.object(client, "_request", return_value=completed) as request:
            self.assertEqual(
                client.prompt("ses_123", "lawnmowerman", [{"type": "text", "text": "hello"}]),
                "reply",
            )
        self.assertEqual(request.call_count, 1)
        request.assert_called_once_with(
            "/session/ses_123/message",
            {"agent": "lawnmowerman", "parts": [{"type": "text", "text": "hello"}]},
            operation=OPENCODE_OPERATION_PROMPT,
        )
        for invoked in request.call_args_list:
            path = invoked.args[0]
            self.assertNotIn("/api", path)
            self.assertNotIn("/prompt", path)
            self.assertNotIn("prompt_async", path)
            self.assertNotIn("/wait", path)

    def test_prompt_joins_text_parts_into_one_documented_text_part(self):
        client = OpenCodeClient(self.settings)
        completed = {"info": {"id": "msg_123"}, "parts": [{"type": "text", "text": "reply"}]}
        with patch.object(client, "_request", return_value=completed) as request:
            client.prompt(
                "ses_123",
                "grillmaster",
                [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}],
            )
        request.assert_called_once_with(
            "/session/ses_123/message",
            {"agent": "grillmaster", "parts": [{"type": "text", "text": "line one\nline two"}]},
            operation=OPENCODE_OPERATION_PROMPT,
        )

    def test_prompt_extracts_assistant_text_from_info_and_parts(self):
        client = OpenCodeClient(self.settings)
        completed = {
            "info": {"id": "msg_123", "role": "assistant"},
            "parts": [
                {"type": "step-start"},
                {"type": "tool", "tool": "read", "state": {"content": "raw file detail"}},
                {"type": "text", "text": " part one "},
                {"type": "text", "text": "part two"},
            ],
        }
        with patch.object(client, "_request", return_value=completed) as request:
            self.assertEqual(
                client.prompt("ses_123", "lawnmowerman", [{"type": "text", "text": "hello"}]),
                "part one part two",
            )
        self.assertEqual(request.call_count, 1)

    def test_prompt_rejects_invalid_message_results_safely(self):
        client = OpenCodeClient(self.settings)
        invalid_results = (
            {},
            {"id": "in_123"},
            {"data": {"id": "in_123"}},
            {"data": {"info": {"id": "msg_123"}, "parts": [{"type": "text", "text": "wrapped"}]}},
            {"info": {"id": "msg_123"}, "parts": "not-a-list"},
            {"info": "not-an-object", "parts": []},
            {"info": {"id": "msg_123"}, "parts": [{"type": "tool", "state": {"output": "raw detail"}}]},
            {"info": {"id": "msg_123"}, "parts": [{"type": "text", "text": "   "}]},
            [{"type": "text", "text": "list"}],
            "raw string",
        )
        for result in invalid_results:
            with self.subTest(result=result):
                with patch.object(client, "_request", return_value=result):
                    with self.assertRaises(BridgeError) as raised:
                        client.prompt("ses_123", "lawnmowerman", [{"type": "text", "text": "hello"}])
                self.assertEqual(raised.exception.error_code, "opencode-response-invalid")
                self.assertNotIn("in_123", str(raised.exception))
                self.assertNotIn("raw detail", str(raised.exception))

    def test_prompt_classifies_structurally_valid_error_envelope(self):
        client = OpenCodeClient(self.settings)
        errored = {
            "info": {
                "id": "msg_123",
                "role": "assistant",
                "error": {
                    "name": "APIError",
                    "data": {
                        "message": "provider quota exhausted",
                        "statusCode": 429,
                        "isRetryable": False,
                        "responseHeaders": {"authorization": "Bearer private-token"},
                        "responseBody": "private provider response",
                        "metadata": {"url": "https://provider.example.invalid/request"},
                    },
                },
            },
            "parts": [],
        }
        expected_code = "opencode-response-error:api:429:nonretryable"
        self.assertIn(expected_code, BRIDGE_ERROR_CODES)
        with patch.object(client, "_request", return_value=errored):
            with self.assertRaises(BridgeError) as raised:
                client.prompt("ses_123", "lawnmowerman", [{"type": "text", "text": "hello"}])
        self.assertEqual(raised.exception.error_code, expected_code)
        for unsafe_value in (
            "provider quota exhausted",
            "private-token",
            "private provider response",
            "provider.example.invalid",
            "msg_123",
        ):
            self.assertNotIn(unsafe_value, str(raised.exception))
            self.assertNotIn(unsafe_value, raised.exception.error_code)

    def test_response_error_classifier_uses_only_bounded_fields(self):
        cases = (
            (
                {"name": "ProviderAuthError", "data": {"providerID": "provider-private", "message": "credential detail"}},
                "opencode-response-error:provider-auth",
            ),
            (
                {"name": "ContextOverflowError", "data": {"message": "context detail", "responseBody": "private body"}},
                "opencode-response-error:context-overflow",
            ),
            (
                {"name": "MessageAbortedError", "data": {"message": "interrupt detail"}},
                "opencode-response-error:aborted",
            ),
            (
                {"name": "MessageOutputLengthError", "data": {}},
                "opencode-response-error:output-length",
            ),
            (
                {"name": "StructuredOutputError", "data": {"message": "schema detail", "retries": 2}},
                "opencode-response-error:structured-output",
            ),
            (
                {"name": "ContentFilterError", "data": {"message": "filter detail"}},
                "opencode-response-error:content-filter",
            ),
            (
                {"name": "APIError", "data": {"message": "detail", "statusCode": "429", "isRetryable": "false"}},
                "opencode-response-error:api:no-status:unknown",
            ),
            (
                {"name": "UnknownError", "data": {"message": "detail", "ref": "private-ref"}},
                "opencode-response-error:unknown",
            ),
            ("not-an-error", "opencode-response-error:unknown"),
        )
        for error, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                self.assertEqual(opencode_response_error_code(error), expected_code)
                self.assertIn(expected_code, BRIDGE_ERROR_CODES)
        self.assertEqual(classify_response_error({"name": "APIError", "data": None}), "api:no-status:unknown")
        self.assertNotIn("credential detail", opencode_response_error_code(cases[0][0]))
        self.assertNotIn("private-ref", opencode_response_error_code(cases[-2][0]))

    def test_prompt_rejects_unmapped_file_parts(self):
        client = OpenCodeClient(self.settings)
        with self.assertRaises(UnsupportedMedia):
            client.prompt(
                "ses_123",
                "lawnmowerman",
                [{"type": "file", "mime": "image/png", "filename": "image", "url": "data:image/png;base64,"}],
            )

    def test_opencode_request_failures_map_to_static_operation_and_category(self):
        settings = replace(self.settings, opencode_base_url="https://opencode.example.invalid")
        client = OpenCodeClient(settings)
        failures = (
            (URLError(ConnectionRefusedError()), "transport"),
            (URLError(TimeoutError()), "transport"),
            (URLError("unknown url type"), "url-configuration"),
            (ValueError("malformed url detail"), "url-configuration"),
            (OError := OSError("socket detail"), "os"),
            (HTTPError("https://opencode.example.invalid/session", 404, "client detail", None, None), "http-4xx"),
            (HTTPError("https://opencode.example.invalid/session", 429, "rate detail", None, None), "http-4xx"),
            (HTTPError("https://opencode.example.invalid/session", 500, "server detail", None, None), "http-5xx"),
            (HTTPError("https://opencode.example.invalid/session", 503, "unavailable detail", None, None), "http-5xx"),
            (HTTPError("https://opencode.example.invalid/session", 302, "redirect detail", None, None), "unknown"),
        )
        for failure, category in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch("server.build_opener") as opener_factory:
                    opener_factory.return_value.open.side_effect = failure
                    with self.assertRaises(BridgeError) as raised:
                        client.create_session()
                self.assertEqual(
                    raised.exception.error_code,
                    f"opencode-request-failed:session-create:{category}",
                )
                self.assertEqual(str(raised.exception), "OpenCode request failed")
                self.assertNotIn("detail", raised.exception.error_code)
                self.assertNotIn("opencode.example.invalid", raised.exception.error_code)

    def test_prompt_request_failure_maps_to_prompt_operation(self):
        settings = replace(self.settings, opencode_base_url="https://opencode.example.invalid")
        client = OpenCodeClient(settings)
        with patch("server.build_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = URLError(TimeoutError())
            with self.assertRaises(BridgeError) as raised:
                client.prompt("ses_123", "lawnmowerman", [{"type": "text", "text": "hello"}])
        self.assertEqual(
            raised.exception.error_code,
            "opencode-request-failed:prompt:transport",
        )
        self.assertNotIn("ses_123", raised.exception.error_code)
        self.assertNotIn("opencode.example.invalid", str(raised.exception))

    def test_classifier_preserves_unknown_fallback_for_unmatched_errors(self):
        unmatched = (
            RuntimeError("unclassified detail"),
            KeyError("odd detail"),
            HTTPError("https://secret.example.invalid/x", 302, "redirect detail", None, None),
        )
        for failure in unmatched:
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(classify_request_failure(failure), "unknown")
                composed = opencode_request_error_code("prompt", classify_request_failure(failure))
                self.assertEqual(composed, "opencode-request-failed:prompt:unknown")
                self.assertNotIn("detail", composed)
                self.assertNotIn("secret.example.invalid", composed)

    def test_composed_request_codes_stay_within_bounded_taxonomy(self):
        for operation in ("session-create", "prompt"):
            for category in ("http-4xx", "http-5xx", "transport", "url-configuration", "os", "unknown"):
                self.assertIn(opencode_request_error_code(operation, category), BRIDGE_ERROR_CODES)
        self.assertNotIn("opencode-request-failed:wait:transport", BRIDGE_ERROR_CODES)
        self.assertNotIn("opencode-request-failed:message-list:transport", BRIDGE_ERROR_CODES)
        self.assertNotIn("opencode-request-failed:prompt-async:transport", BRIDGE_ERROR_CODES)
        self.assertNotIn("opencode-request-failed:message:transport", BRIDGE_ERROR_CODES)
        self.assertEqual(
            opencode_request_error_code("no-such-operation", "no-such-category"),
            "opencode-request-failed:unknown:unknown",
        )
        self.assertEqual(opencode_request_error_code(None, None), "opencode-request-failed:unknown:unknown")
        self.assertNotIn("no-such-operation", opencode_request_error_code("no-such-operation", "transport"))

    def test_bridge_error_defaults_to_safe_bounded_code(self):
        self.assertEqual(BridgeError("worker configuration is incomplete").error_code, "opencode-response-invalid")
        self.assertEqual(BridgeError("legacy detail", "opencode-failed").error_code, "opencode-response-invalid")
        self.assertEqual(BridgeError("OpenCode prompt has no text", "opencode-input-invalid").error_code, "opencode-input-invalid")
        self.assertEqual(
            BridgeError("OpenCode response reported an error", "opencode-response-error:api:401:nonretryable").error_code,
            "opencode-response-error:api:401:nonretryable",
        )

    def test_process_job_persists_bounded_error_code(self):
        payload = {"from": "+15559999999", "to": "+15550000001", "body": "hello", "media": [], "agent": "lawnmowerman"}
        identifier = sender_hash(self.settings.sender_hash_key, payload["from"])
        self.store.enqueue("SM301", "lawnmowerman", identifier, payload)
        job = self.store.claim()
        self.store.remember_session("lawnmowerman", identifier, "ses_301")
        client = OpenCodeClient(self.settings)
        failure = BridgeError("OpenCode prompt has no text", "opencode-input-invalid")
        with patch.object(client, "prompt", side_effect=failure):
            with self.assertLogs("opencode-sms-bridge", level="WARNING") as captured:
                process_job(self.settings, self.store, client, job)
        telemetry = "\n".join(captured.output)
        self.assertIn("event=job_failed stage=opencode channel=lawnmowerman error_code=opencode-input-invalid", telemetry)
        self.assertNotIn("opencode-failed", telemetry)
        self.assertNotIn(payload["body"], telemetry)
        with sqlite3.connect(self.settings.state_path) as connection:
            row = connection.execute("SELECT status, detail_code FROM jobs WHERE message_sid='SM301'").fetchone()
        self.assertEqual(tuple(row), ("failed", "opencode-input-invalid"))

    def test_process_job_persists_static_request_failure_code_without_raw_detail(self):
        settings = replace(self.settings, opencode_base_url="https://opencode.example.invalid")
        payload = {
            "from": "+15559999999",
            "to": "+15550000001",
            "body": "hello https://secret.example.invalid/token",
            "media": [],
            "agent": "lawnmowerman",
        }
        identifier = sender_hash(self.settings.sender_hash_key, payload["from"])
        self.store.enqueue("SM302", "lawnmowerman", identifier, payload)
        job = self.store.claim()
        self.store.remember_session("lawnmowerman", identifier, "ses_302")
        client = OpenCodeClient(settings)
        with patch("server.build_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = URLError(ConnectionRefusedError("socket detail"))
            with self.assertLogs("opencode-sms-bridge", level="WARNING") as captured:
                process_job(settings, self.store, client, job)
        telemetry = "\n".join(captured.output)
        self.assertIn(
            "event=job_failed stage=opencode channel=lawnmowerman error_code=opencode-request-failed:prompt:transport",
            telemetry,
        )
        for unsafe_value in (
            "socket detail",
            "opencode.example.invalid",
            "secret.example.invalid",
            "ses_302",
            payload["body"],
        ):
            self.assertNotIn(unsafe_value, telemetry)
        with sqlite3.connect(self.settings.state_path) as connection:
            row = connection.execute("SELECT status, detail_code FROM jobs WHERE message_sid='SM302'").fetchone()
        self.assertEqual(tuple(row), ("failed", "opencode-request-failed:prompt:transport"))

    def test_worker_sends_through_messaging_service_without_from_number(self):
        settings = replace(
            self.settings,
            mode="worker",
            opencode_base_url="https://opencode.example.invalid",
            twilio_api_key_sid="SKtestkey",
            twilio_api_key_secret="testsecret",
            twilio_messaging_service_sid="MGtestservice",
        )
        payload = {"from": "+15559999999", "to": "+15550000001", "body": "hello", "media": [], "agent": "lawnmowerman"}
        identifier = sender_hash(self.settings.sender_hash_key, payload["from"])
        self.store.enqueue("SM401", "lawnmowerman", identifier, payload)
        job = self.store.claim()
        self.store.remember_session("lawnmowerman", identifier, "ses_401")
        client = OpenCodeClient(settings)
        with patch.object(client, "prompt", return_value="  Mow   dry grass  at noon.  "):
            with patch("server.Client") as client_factory:
                with self.assertLogs("opencode-sms-bridge", level="INFO") as captured:
                    process_job(settings, self.store, client, job)
        client_factory.assert_called_once_with("SKtestkey", "testsecret", "AC1234567890")
        create = client_factory.return_value.messages.create
        create.assert_called_once_with(
            to="+15559999999",
            body="Mow dry grass at noon.",
            messaging_service_sid="MGtestservice",
        )
        self.assertNotIn("from_", create.call_args.kwargs)
        telemetry = "\n".join(captured.output)
        self.assertIn("event=job_sent channel=lawnmowerman", telemetry)
        for unsafe_value in (payload["from"], payload["to"], "MGtestservice", "Mow dry grass at noon.", "ses_401"):
            self.assertNotIn(unsafe_value, telemetry)
        with sqlite3.connect(self.settings.state_path) as connection:
            row = connection.execute("SELECT status, detail_code FROM jobs WHERE message_sid='SM401'").fetchone()
        self.assertEqual(tuple(row), ("sent", "ok"))

    def test_worker_keeps_direct_from_number_when_messaging_service_is_unset(self):
        settings = replace(
            self.settings,
            mode="worker",
            opencode_base_url="https://opencode.example.invalid",
            twilio_api_key_sid="SKtestkey",
            twilio_api_key_secret="testsecret",
        )
        self.assertEqual(settings.twilio_messaging_service_sid, "")
        payload = {"from": "+15559999999", "to": "+15550000003", "body": "hello", "media": [], "agent": "homesteader"}
        identifier = sender_hash(self.settings.sender_hash_key, payload["from"])
        self.store.enqueue("SM402", "homesteader", identifier, payload)
        job = self.store.claim()
        self.store.remember_session("homesteader", identifier, "ses_402")
        client = OpenCodeClient(settings)
        with patch.object(client, "prompt", return_value="raised beds ready"):
            with patch("server.Client") as client_factory:
                with self.assertLogs("opencode-sms-bridge", level="INFO") as captured:
                    process_job(settings, self.store, client, job)
        create = client_factory.return_value.messages.create
        create.assert_called_once_with(to="+15559999999", from_="+15550000003", body="raised beds ready")
        self.assertNotIn("messaging_service_sid", create.call_args.kwargs)
        telemetry = "\n".join(captured.output)
        self.assertIn("event=job_sent channel=homesteader", telemetry)
        for unsafe_value in (payload["from"], payload["to"], "raised beds ready", "ses_402"):
            self.assertNotIn(unsafe_value, telemetry)
        with sqlite3.connect(self.settings.state_path) as connection:
            row = connection.execute("SELECT status, detail_code FROM jobs WHERE message_sid='SM402'").fetchone()
        self.assertEqual(tuple(row), ("sent", "ok"))

    def test_worker_marks_delivery_unknown_and_hides_provider_detail_on_send_failure(self):
        settings = replace(
            self.settings,
            mode="worker",
            opencode_base_url="https://opencode.example.invalid",
            twilio_api_key_sid="SKtestkey",
            twilio_api_key_secret="testsecret",
            twilio_messaging_service_sid="MGtestservice",
        )
        payload = {"from": "+15559999999", "to": "+15550000002", "body": "hello", "media": [], "agent": "grillmaster"}
        identifier = sender_hash(self.settings.sender_hash_key, payload["from"])
        self.store.enqueue("SM403", "grillmaster", identifier, payload)
        job = self.store.claim()
        self.store.remember_session("grillmaster", identifier, "ses_403")
        client = OpenCodeClient(settings)
        with patch.object(client, "prompt", return_value="preheat the grill"):
            with patch("server.Client") as client_factory:
                client_factory.return_value.messages.create.side_effect = RuntimeError(
                    "provider detail 21606 credential"
                )
                with self.assertLogs("opencode-sms-bridge", level="WARNING") as captured:
                    process_job(settings, self.store, client, job)
        create = client_factory.return_value.messages.create
        create.assert_called_once_with(
            to="+15559999999",
            body="preheat the grill",
            messaging_service_sid="MGtestservice",
        )
        self.assertNotIn("from_", create.call_args.kwargs)
        telemetry = "\n".join(captured.output)
        self.assertIn("event=job_delivery_unknown stage=twilio channel=grillmaster", telemetry)
        for unsafe_value in (
            "provider detail",
            "21606",
            "credential",
            payload["from"],
            payload["to"],
            "MGtestservice",
            "preheat the grill",
            "ses_403",
        ):
            self.assertNotIn(unsafe_value, telemetry)
        with sqlite3.connect(self.settings.state_path) as connection:
            row = connection.execute("SELECT status, detail_code FROM jobs WHERE message_sid='SM403'").fetchone()
        self.assertEqual(tuple(row), ("delivery-unknown", "twilio-send-failed"))

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
