import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import uvicorn
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from twilio.request_validator import RequestValidator
from twilio.rest import Client


LOG = logging.getLogger("opencode-sms-bridge")
CHANNEL_AGENTS = frozenset({"lawnmowerman", "grillmaster", "homesteader", "homerepair"})
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
MAX_WEBHOOK_BYTES = 64 * 1024


class BridgeError(RuntimeError):
    pass


class UnsupportedMedia(BridgeError):
    pass


@dataclass(frozen=True)
class Routing:
    account_sid: str
    approved_senders: frozenset[str]
    channels: dict[str, str]


@dataclass(frozen=True)
class Settings:
    mode: str
    routing: Routing
    state_path: Path
    state_key: bytes
    sender_hash_key: bytes
    canonical_webhook_url: str
    twilio_auth_token: str
    media_allowed_hosts: frozenset[str]
    max_media_bytes: int
    max_audio_seconds: int
    image_parts_enabled: bool
    opencode_base_url: str
    opencode_username: str
    opencode_password: str
    opencode_timeout_seconds: int
    twilio_api_key_sid: str
    twilio_api_key_secret: str
    whisper_url: str
    whisper_model: str

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.environ.get("BRIDGE_MODE", "").strip()
        if mode not in {"ingress", "worker"}:
            raise BridgeError("BRIDGE_MODE must be ingress or worker")
        routing = load_routing(Path(require_env("ROUTING_CONFIG_PATH")))
        canonical = require_env("CANONICAL_WEBHOOK_URL")
        parsed = urlsplit(canonical)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise BridgeError("CANONICAL_WEBHOOK_URL must be an HTTPS URL without query or fragment")
        return cls(
            mode=mode,
            routing=routing,
            state_path=Path(require_env("STATE_PATH")),
            state_key=require_env("STATE_ENCRYPTION_KEY").encode(),
            sender_hash_key=require_env("SENDER_HASH_KEY").encode(),
            canonical_webhook_url=canonical,
            twilio_auth_token=require_env("TWILIO_AUTH_TOKEN"),
            media_allowed_hosts=frozenset(
                item.strip().lower()
                for item in os.environ.get("TWILIO_MEDIA_ALLOWED_HOSTS", "api.twilio.com").split(",")
                if item.strip()
            ),
            max_media_bytes=positive_int("MAX_MEDIA_BYTES", 5 * 1024 * 1024),
            max_audio_seconds=positive_int("MAX_AUDIO_SECONDS", 120),
            image_parts_enabled=os.environ.get("OPENCODE_IMAGE_PARTS_ENABLED", "false").lower() == "true",
            opencode_base_url=os.environ.get("OPENCODE_API_BASE_URL", "").rstrip("/"),
            opencode_username=os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"),
            opencode_password=os.environ.get("OPENCODE_SERVER_PASSWORD", ""),
            opencode_timeout_seconds=positive_int("OPENCODE_TIMEOUT_SECONDS", 120),
            twilio_api_key_sid=os.environ.get("TWILIO_API_KEY_SID", ""),
            twilio_api_key_secret=os.environ.get("TWILIO_API_KEY_SECRET", ""),
            whisper_url=os.environ.get("WHISPER_URL", "").rstrip("/"),
            whisper_model=os.environ.get("WHISPER_MODEL", "base"),
        )

    def worker_ready(self) -> None:
        required = {
            "OPENCODE_API_BASE_URL": self.opencode_base_url,
            "OPENCODE_SERVER_PASSWORD": self.opencode_password,
            "TWILIO_API_KEY_SID": self.twilio_api_key_sid,
            "TWILIO_API_KEY_SECRET": self.twilio_api_key_secret,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise BridgeError("worker configuration is incomplete: " + ", ".join(missing))


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BridgeError(f"{name} is required")
    return value


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise BridgeError(f"{name} must be an integer") from error
    if value <= 0:
        raise BridgeError(f"{name} must be positive")
    return value


def normalize_e164(value: Any) -> str:
    if not isinstance(value, str):
        raise BridgeError("phone number is invalid")
    normalized = value.strip()
    if not normalized.startswith("+") or not normalized[1:].isdigit() or not 8 <= len(normalized) <= 16:
        raise BridgeError("phone number must be E.164")
    return normalized


def load_routing(path: Path) -> Routing:
    try:
        payload = json.loads(path.read_text())
        account_sid = str(payload["accountSid"])
        approved_senders = frozenset(normalize_e164(item) for item in payload["approvedSenders"])
        channels = {
            normalize_e164(phone): str(config["agent"])
            for phone, config in payload["channels"].items()
        }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BridgeError("routing configuration is invalid") from error
    if not account_sid.startswith("AC") or len(account_sid) < 10:
        raise BridgeError("routing account SID is invalid")
    if not approved_senders:
        raise BridgeError("routing configuration needs an approved sender")
    if len(channels) != 4 or set(channels.values()) != CHANNEL_AGENTS:
        raise BridgeError("routing configuration must map four numbers to the four fixed primary agents")
    return Routing(account_sid=account_sid, approved_senders=approved_senders, channels=channels)


def sender_hash(key: bytes, sender: str) -> str:
    return hmac.new(key, sender.encode(), hashlib.sha256).hexdigest()


class SQLiteStore:
    def __init__(self, path: Path, key: bytes):
        self.path = path
        self.fernet = Fernet(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                  message_sid TEXT PRIMARY KEY,
                  channel TEXT NOT NULL,
                  sender_hash TEXT NOT NULL,
                  payload BLOB NOT NULL,
                  status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL,
                  claimed_at INTEGER,
                  detail_code TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                  channel TEXT NOT NULL,
                  sender_hash TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  PRIMARY KEY(channel, sender_hash)
                );
                """
            )

    def enqueue(self, message_sid: str, channel: str, sender_id: str, payload: dict[str, Any]) -> bool:
        ciphertext = self.fernet.encrypt(json.dumps(payload, separators=(",", ":")).encode())
        with self._connect() as connection:
            result = connection.execute(
                """INSERT OR IGNORE INTO jobs
                   (message_sid, channel, sender_hash, payload, status, created_at)
                   VALUES (?, ?, ?, ?, 'queued', ?)""",
                (message_sid, channel, sender_id, ciphertext, int(time.time())),
            )
        return result.rowcount == 1

    def claim(self) -> dict[str, Any] | None:
        stale_before = int(time.time()) - 300
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET status='queued', claimed_at=NULL WHERE status='processing' AND claimed_at < ?",
                (stale_before,),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            connection.execute(
                "UPDATE jobs SET status='processing', attempts=attempts+1, claimed_at=? WHERE message_sid=?",
                (int(time.time()), row["message_sid"]),
            )
            connection.execute("COMMIT")
        try:
            payload = json.loads(self.fernet.decrypt(row["payload"]).decode())
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.finish(row["message_sid"], "failed", "payload-unreadable")
            raise BridgeError("queued payload cannot be decrypted") from error
        return {"message_sid": row["message_sid"], "channel": row["channel"], "sender_hash": row["sender_hash"], "payload": payload}

    def session(self, channel: str, sender_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT session_id FROM sessions WHERE channel=? AND sender_hash=?", (channel, sender_id)
            ).fetchone()
        return None if row is None else row["session_id"]

    def remember_session(self, channel: str, sender_id: str, session_id: str) -> str:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sessions (channel, sender_hash, session_id) VALUES (?, ?, ?)",
                (channel, sender_id, session_id),
            )
        return self.session(channel, sender_id) or session_id

    def begin_send(self, message_sid: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE jobs SET status='sending' WHERE message_sid=? AND status='processing'", (message_sid,)
            )
        return result.rowcount == 1

    def finish(self, message_sid: str, status: str, detail_code: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, detail_code=? WHERE message_sid=?", (status, detail_code, message_sid)
            )


def parse_form(body: bytes) -> dict[str, str]:
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="invalid form") from error
    result: dict[str, str] = {}
    for key, value in pairs:
        if key in result:
            raise HTTPException(status_code=400, detail="duplicate form key")
        result[key] = value
    return result


def empty_twiml() -> Response:
    return Response(EMPTY_TWIML, media_type="application/xml")


def validate_webhook(settings: Settings, form: dict[str, str], signature: str | None) -> bool:
    if not signature:
        return False
    return RequestValidator(settings.twilio_auth_token).validate(settings.canonical_webhook_url, form, signature)


def incoming_payload(settings: Settings, form: dict[str, str]) -> tuple[str, str, str, dict[str, Any]] | None:
    try:
        account_sid = form["AccountSid"]
        message_sid = form["MessageSid"]
        source = normalize_e164(form["From"])
        destination = normalize_e164(form["To"])
        num_media = int(form.get("NumMedia", "0"))
    except (KeyError, ValueError, BridgeError) as error:
        raise HTTPException(status_code=400, detail="invalid message") from error
    if account_sid != settings.routing.account_sid or destination not in settings.routing.channels:
        return None
    if source not in settings.routing.approved_senders:
        return None
    if not message_sid or len(message_sid) > 64 or num_media < 0 or num_media > 3:
        raise HTTPException(status_code=400, detail="invalid message metadata")
    media = []
    for index in range(num_media):
        url = form.get(f"MediaUrl{index}")
        content_type = form.get(f"MediaContentType{index}")
        if not url or not content_type:
            raise HTTPException(status_code=400, detail="invalid media metadata")
        media.append({"url": url, "contentType": content_type.lower()})
    channel = settings.routing.channels[destination]
    payload = {"from": source, "to": destination, "body": form.get("Body", ""), "media": media, "agent": channel}
    return message_sid, channel, sender_hash(settings.sender_hash_key, source), payload


def create_ingress_app(settings: Settings, store: SQLiteStore) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.post("/twilio/inbound")
    async def inbound(request: FastAPIRequest) -> Response:
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("application/x-www-form-urlencoded"):
            raise HTTPException(status_code=415, detail="form encoding required")
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="request too large")
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="request too large")
        form = parse_form(body)
        if not validate_webhook(settings, form, request.headers.get("x-twilio-signature")):
            raise HTTPException(status_code=403, detail="invalid signature")
        message = incoming_payload(settings, form)
        if message is None:
            return empty_twiml()
        message_sid, channel, source_id, payload = message
        store.enqueue(message_sid, channel, source_id, payload)
        return empty_twiml()

    return app


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def checked_media_url(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.hostname is None
        or parsed.hostname.lower() not in allowed_hosts
    ):
        raise UnsupportedMedia("media URL is not a Twilio HTTPS endpoint")


def download_media(settings: Settings, item: dict[str, str]) -> tuple[bytes, str]:
    checked_media_url(item["url"], settings.media_allowed_hosts)
    credentials = base64.b64encode(f"{settings.routing.account_sid}:{settings.twilio_auth_token}".encode()).decode()
    request = Request(item["url"], headers={"Authorization": f"Basic {credentials}", "Accept": "*/*"})
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > settings.max_media_bytes:
                raise UnsupportedMedia("media exceeds configured size")
            actual_type = response.headers.get_content_type().lower()
            data = response.read(settings.max_media_bytes + 1)
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise UnsupportedMedia("media download failed") from error
    if len(data) > settings.max_media_bytes:
        raise UnsupportedMedia("media exceeds configured size")
    declared_type = item["contentType"].split(";", 1)[0].lower()
    if actual_type != declared_type:
        raise UnsupportedMedia("media content type does not match")
    return data, actual_type


def sanitize_image(data: bytes, mime: str) -> tuple[bytes, str]:
    expected = {"image/jpeg": "JPEG", "image/png": "PNG"}
    if mime not in expected:
        raise UnsupportedMedia("only JPEG and PNG images are supported")
    try:
        with Image.open(io.BytesIO(data)) as check:
            check.verify()
        with Image.open(io.BytesIO(data)) as image:
            if image.format != expected[mime]:
                raise UnsupportedMedia("image magic bytes do not match content type")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((4096, 4096))
            output = io.BytesIO()
            if mime == "image/jpeg":
                image.convert("RGB").save(output, "JPEG", quality=85, optimize=True)
            else:
                image.convert("RGBA").save(output, "PNG", optimize=True)
            return output.getvalue(), mime
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise UnsupportedMedia("image cannot be safely decoded") from error


def audio_duration(data: bytes, mime: str, maximum: int) -> None:
    suffix = {"audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav"}.get(mime)
    if suffix is None:
        raise UnsupportedMedia("unsupported audio type")
    with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
        handle.write(data)
        handle.flush()
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", handle.name],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            duration = float(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise UnsupportedMedia("audio duration could not be verified") from error
    if result.returncode != 0 or duration <= 0 or duration > maximum:
        raise UnsupportedMedia("audio duration is outside the configured limit")


def transcribe(settings: Settings, data: bytes, mime: str) -> str:
    if not settings.whisper_url:
        raise UnsupportedMedia("audio transcription is not configured")
    boundary = f"----opencode-sms-{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{settings.whisper_model}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio\"\r\nContent-Type: {mime}\r\n\r\n".encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = Request(
        settings.whisper_url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
    )
    try:
        with build_opener(NoRedirect).open(request, timeout=60) as response:
            payload = json.loads(response.read().decode())
            text = payload.get("text", "")
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
        raise UnsupportedMedia("audio transcription failed") from error
    if not isinstance(text, str) or not text.strip():
        raise UnsupportedMedia("audio transcription was empty")
    return text.strip()


class OpenCodeClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        basic = base64.b64encode(f"{settings.opencode_username}:{settings.opencode_password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {basic}", "Content-Type": "application/json"}

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.settings.opencode_base_url}{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers=self.headers,
        )
        try:
            with build_opener(NoRedirect).open(request, timeout=self.settings.opencode_timeout_seconds) as response:
                return json.loads(response.read().decode())
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
            raise BridgeError("OpenCode request failed") from error

    def create_session(self, agent: str) -> str:
        response = self._request("/api/session", {"agent": agent})
        data = response.get("data", response)
        session_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise BridgeError("OpenCode session response was invalid")
        return session_id

    def prompt(self, session_id: str, parts: list[dict[str, str]]) -> str:
        response = self._request(f"/api/session/{session_id}/message", {"parts": parts})
        data = response.get("data", response)
        candidates = data.get("parts", []) if isinstance(data, dict) else []
        text = "".join(part.get("text", "") for part in candidates if part.get("type") == "text")
        if not text.strip():
            raise BridgeError("OpenCode response did not contain text")
        return text.strip()


def build_parts(settings: Settings, payload: dict[str, Any]) -> list[dict[str, str]]:
    parts: list[dict[str, str]] = []
    body = payload.get("body", "")
    if isinstance(body, str) and body.strip():
        parts.append({"type": "text", "text": body.strip()})
    for item in payload.get("media", []):
        data, mime = download_media(settings, item)
        if mime.startswith("image/"):
            if not settings.image_parts_enabled:
                raise UnsupportedMedia("image analysis is not configured")
            sanitized, safe_mime = sanitize_image(data, mime)
            encoded = base64.b64encode(sanitized).decode()
            parts.append({"type": "file", "mime": safe_mime, "filename": "twilio-image", "url": f"data:{safe_mime};base64,{encoded}"})
        elif mime.startswith("audio/"):
            audio_duration(data, mime, settings.max_audio_seconds)
            parts.append({"type": "text", "text": "Audio MMS transcript:\n" + transcribe(settings, data, mime)})
        else:
            raise UnsupportedMedia("unsupported media type")
    if not parts:
        raise UnsupportedMedia("message has no usable text, image, or audio")
    return parts


def sms_body(value: str) -> str:
    cleaned = " ".join(value.split())
    return cleaned[:1500] if cleaned else "I could not prepare a response. Please try again."


def process_job(settings: Settings, store: SQLiteStore, client: OpenCodeClient, job: dict[str, Any]) -> None:
    try:
        session_id = store.session(job["channel"], job["sender_hash"])
        if session_id is None:
            session_id = store.remember_session(job["channel"], job["sender_hash"], client.create_session(job["payload"]["agent"]))
        response = client.prompt(session_id, build_parts(settings, job["payload"]))
    except UnsupportedMedia:
        response = "This channel cannot process that attachment yet. Please send text or try a supported attachment later."
    except BridgeError:
        store.finish(job["message_sid"], "failed", "opencode-failed")
        return
    if not store.begin_send(job["message_sid"]):
        return
    try:
        twilio = Client(settings.twilio_api_key_sid, settings.twilio_api_key_secret, settings.routing.account_sid)
        twilio.messages.create(to=job["payload"]["from"], from_=job["payload"]["to"], body=sms_body(response))
    except Exception:  # The helper library's exception details can include provider data; do not log them.
        store.finish(job["message_sid"], "delivery-unknown", "twilio-send-failed")
        return
    store.finish(job["message_sid"], "sent", "ok")


def create_worker_app(settings: Settings, store: SQLiteStore) -> FastAPI:
    settings.worker_ready()
    client = OpenCodeClient(settings)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.last_cycle = 0.0

    @app.on_event("startup")
    async def start_worker() -> None:
        async def loop() -> None:
            while True:
                app.state.last_cycle = time.monotonic()
                job = await asyncio.to_thread(store.claim)
                if job is None:
                    await asyncio.sleep(1)
                    continue
                await asyncio.to_thread(process_job, settings, store, client, job)
        asyncio.create_task(loop())

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        healthy = time.monotonic() - app.state.last_cycle < 30
        return JSONResponse({"status": "ok" if healthy else "unhealthy"}, status_code=200 if healthy else 503)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    store = SQLiteStore(settings.state_path, settings.state_key)
    if settings.mode == "ingress":
        uvicorn.run(create_ingress_app(settings, store), host="0.0.0.0", port=8080, log_level="warning", access_log=False)
    else:
        uvicorn.run(create_worker_app(settings, store), host="127.0.0.1", port=8081, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
