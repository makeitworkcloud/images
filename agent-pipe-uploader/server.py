import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import stat
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route


ARTIFACT_ROOT = Path(os.environ.get("ARTIFACT_ROOT", "/artifacts")).absolute()
PROFILE_CONFIG_PATH = Path(os.environ.get("PROFILE_CONFIG_PATH", "/etc/agent-pipe/profiles.json"))
MCP_ALLOWED_HOSTS = [host.strip() for host in os.environ["MCP_ALLOWED_HOSTS"].split(",") if host.strip()]
CHUNK_SIZE = 1024 * 1024
OPERATIONS = {"upload", "download", "verify"}


class TransferError(ValueError):
    pass


def load_profiles() -> dict[str, dict]:
    payload = json.loads(PROFILE_CONFIG_PATH.read_text())
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise RuntimeError("transfer profiles are missing")
    return profiles


PROFILES = load_profiles()


def profile(name: str, operation: str) -> dict:
    selected = PROFILES.get(name)
    if not isinstance(selected, dict):
        raise TransferError("unknown transfer profile")
    operations = selected.get("operations")
    if (
        not isinstance(operations, list)
        or not operations
        or any(not isinstance(item, str) or item not in OPERATIONS for item in operations)
        or len(operations) != len(set(operations))
    ):
        raise TransferError("transfer profile requires an explicit valid operations list")
    if operation not in OPERATIONS or operation not in operations:
        raise TransferError("operation is not allowed by the transfer profile")
    for key in ("allowedHosts", "pathPrefixes", "requiredQueryParameters"):
        values = selected.get(key)
        if not isinstance(values, list) or not values or any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            raise TransferError("transfer profile has invalid URL constraints")
    if type(selected.get("maxBytes")) is not int or selected["maxBytes"] < 0:
        raise TransferError("transfer profile has an invalid size limit")
    return selected


@contextmanager
def artifact_parent(value: str, *, create: bool = False):
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise TransferError("artifact path must be a normalized relative file path")
    parts = value.split("/")
    parent = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(ARTIFACT_ROOT, flags)
        for part in parts[:-1]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=parent)
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        yield parent, parts[-1]
    except OSError:
        raise TransferError("artifact filesystem operation failed") from None
    finally:
        if parent is not None:
            os.close(parent)


@contextmanager
def artifact_file(parent: int, name: str):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise TransferError("artifact must be a regular file")
        yield handle


def file_state(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_metadata(handle) -> dict:
    before = os.fstat(handle.fileno())
    digest = hashlib.sha256()
    count = 0
    while chunk := handle.read(CHUNK_SIZE):
        count += len(chunk)
        digest.update(chunk)
    if count != before.st_size or file_state(before) != file_state(os.fstat(handle.fileno())):
        raise TransferError("artifact changed while being read")
    return {"bytes": count, "sha256": digest.hexdigest()}


def signed_target(profile_name: str, value: str, operation: str) -> tuple[dict, str, str]:
    selected = profile(profile_name, operation)
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        raise TransferError("signed URL must be an ASCII URL without whitespace")
    try:
        parsed = urlsplit(value)
        allowed = (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and "#" not in value
            and parsed.hostname is not None
            and parsed.hostname.lower() in {host.lower() for host in selected["allowedHosts"]}
        )
    except ValueError:
        raise TransferError("signed URL is not an allowed HTTPS endpoint") from None
    if not allowed:
        raise TransferError("signed URL is not an allowed HTTPS endpoint")
    decoded_path = unquote(parsed.path)
    if (
        not decoded_path.startswith("/")
        or "//" in decoded_path
        or "\\" in decoded_path
        or "%" in decoded_path
        or any(part in (".", "..") for part in decoded_path.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded_path)
        or re.search(r"%2f|%5c", parsed.path, re.IGNORECASE)
    ):
        raise TransferError("signed URL has an unsafe object path")
    if not any(decoded_path.startswith(prefix) for prefix in selected["pathPrefixes"]):
        raise TransferError("signed URL is outside the allowed object prefix")
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    for parameter in selected["requiredQueryParameters"]:
        values = parameters.get(parameter, [])
        if len(values) != 1 or not values[0].strip():
            raise TransferError("signed URL requires unique non-empty authorization parameters")
    target = parsed.path
    if parsed.query:
        target += "?" + parsed.query
    return selected, parsed.hostname, target


def connection(host: str) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(host, timeout=60)


def ensure_size(size: int, selected: dict) -> None:
    if size < 0 or size > selected["maxBytes"]:
        raise TransferError("artifact exceeds the configured transfer limit")


def put_file(source, host: str, target: str, selected: dict) -> int:
    before = os.fstat(source.fileno())
    size = before.st_size
    ensure_size(size, selected)
    client = response = None
    try:
        client = connection(host)
        client.putrequest("PUT", target, skip_host=True, skip_accept_encoding=True)
        client.putheader("Host", host)
        client.putheader("Content-Length", str(size))
        client.endheaders()
        count = 0
        while chunk := source.read(min(CHUNK_SIZE, size - count + 1)):
            count += len(chunk)
            if count > size:
                raise TransferError("artifact changed during upload")
            client.send(chunk)
        if count != size or file_state(before) != file_state(os.fstat(source.fileno())):
            raise TransferError("artifact changed during upload")
        response = client.getresponse()
        if not 200 <= response.status < 300:
            raise TransferError(f"upload endpoint returned HTTP {response.status}")
        return count
    except (OSError, http.client.HTTPException):
        raise TransferError("upload failed") from None
    finally:
        for resource in (response, client):
            if resource is not None:
                with suppress(OSError):
                    resource.close()


@contextmanager
def get_response(host: str, target: str):
    client = response = None
    try:
        client = connection(host)
        client.request("GET", target, headers={"Host": host, "Accept-Encoding": "identity"})
        response = client.getresponse()
        yield response
    except (OSError, http.client.HTTPException):
        raise TransferError("download failed") from None
    finally:
        for resource in (response, client):
            if resource is not None:
                with suppress(OSError):
                    resource.close()


def response_size(response: http.client.HTTPResponse, selected: dict) -> int:
    if response.status != 200:
        raise TransferError(f"download endpoint returned HTTP {response.status}")
    headers = response.getheaders()
    if any(name.lower() == "transfer-encoding" for name, _ in headers):
        raise TransferError("download endpoint returned unsupported transfer framing")
    if any(name.lower() == "content-encoding" and value.lower().strip() != "identity"
           for name, value in headers):
        raise TransferError("download endpoint returned unsupported content encoding")
    lengths = [value.strip(" \t") for name, value in headers if name.lower() == "content-length"]
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,20}", lengths[0]):
        raise TransferError("download endpoint requires one valid Content-Length")
    size = int(lengths[0])
    ensure_size(size, selected)
    return size


def read_response(response: http.client.HTTPResponse, selected: dict, output=None) -> dict:
    size = response_size(response, selected)
    count = 0
    digest = hashlib.sha256()
    while chunk := response.read(min(CHUNK_SIZE, size - count + 1)):
        count += len(chunk)
        if count > size or count > selected["maxBytes"]:
            raise TransferError("download body exceeds its declared or configured size")
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
    if count != size:
        raise TransferError("download body does not match Content-Length")
    return {"bytes": count, "sha256": digest.hexdigest()}


class AllowedHostMCP:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            host = headers.get(b"host", b"").decode().split(":", 1)[0].lower()
            if host not in {item.lower() for item in MCP_ALLOWED_HOSTS}:
                await PlainTextResponse("MCP host is not allowed", status_code=421)(scope, receive, send)
                return
        await self.app(scope, receive, send)


mcp = FastMCP("agent-pipe")


@mcp.tool
def inspect_artifact(artifact: str) -> dict:
    """Return non-sensitive metadata for a regular file under the artifact root."""
    with artifact_parent(artifact) as (parent, name), artifact_file(parent, name) as source:
        return {"artifact": artifact, **read_metadata(source)}


@mcp.tool
def upload_artifact(profile_name: str, artifact: str, signed_put_url: str) -> dict:
    """Upload an artifact with a caller-supplied, profile-validated signed PUT URL."""
    selected, host, target = signed_target(profile_name, signed_put_url, "upload")
    with artifact_parent(artifact) as (parent, name), artifact_file(parent, name) as source:
        return {"status": "uploaded", "bytes": put_file(source, host, target, selected)}


@mcp.tool
def verify_download(profile_name: str, signed_get_url: str) -> dict:
    """Read and hash the complete signed GET response without retaining or exposing its bytes."""
    selected, host, target = signed_target(profile_name, signed_get_url, "verify")
    with get_response(host, target) as response:
        return {"status": "available", **read_response(response, selected)}


@mcp.tool
def download_artifact(profile_name: str, signed_get_url: str, destination: str) -> dict:
    """Download and hash a profile-validated signed URL into a new artifact file."""
    selected, host, target = signed_target(profile_name, signed_get_url, "download")
    with artifact_parent(destination, create=True) as (parent, name):
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise TransferError("destination artifact already exists")
        temporary_name = None
        try:
            with get_response(host, target) as response:
                candidate = f".agent-pipe-{secrets.token_hex(16)}.tmp"
                descriptor = os.open(
                    candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=parent,
                )
                temporary_name = candidate
                with os.fdopen(descriptor, "wb") as output:
                    metadata = read_response(response, selected, output)
                    output.flush()
                    if file_state(os.fstat(output.fileno())) != file_state(
                        os.stat(candidate, dir_fd=parent, follow_symlinks=False)
                    ):
                        raise TransferError("temporary artifact changed before publication")
                    # Same-directory hard linking atomically refuses an existing destination.
                    os.link(candidate, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            return {"status": "downloaded", "artifact": destination, **metadata}
        finally:
            if temporary_name is not None:
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent)


@mcp.tool
def remove_artifact(artifact: str, expected_sha256: str) -> dict:
    """Remove one local regular file only if its SHA-256 matches; requires caller approval."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise TransferError("expected_sha256 must be a SHA-256 hex digest")
    with artifact_parent(artifact) as (parent, name), artifact_file(parent, name) as source:
        before = file_state(os.fstat(source.fileno()))
        metadata = read_metadata(source)
        if metadata["sha256"] != expected_sha256.lower():
            raise TransferError("artifact SHA-256 does not match; nothing was removed")
        if (
            before != file_state(os.fstat(source.fileno()))
            or before != file_state(os.stat(name, dir_fd=parent, follow_symlinks=False))
        ):
            raise TransferError("artifact changed before removal; nothing was removed")
        os.unlink(name, dir_fd=parent)
        return {"status": "removed", "artifact": artifact, **metadata}


async def healthz(request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    mcp_app = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
        stateless_http=True,
    )
    app = Starlette(
        routes=[Route("/healthz", healthz), Mount("/", app=AllowedHostMCP(mcp_app))],
        lifespan=mcp_app.lifespan,
    )
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning", access_log=False)
