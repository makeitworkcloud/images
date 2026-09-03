import hashlib
import http.client
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route


ARTIFACT_ROOT = Path(os.environ.get("ARTIFACT_ROOT", "/artifacts")).resolve()
PROFILE_CONFIG_PATH = Path(os.environ.get("PROFILE_CONFIG_PATH", "/etc/agent-pipe/profiles.json"))
MCP_ALLOWED_HOSTS = [host.strip() for host in os.environ["MCP_ALLOWED_HOSTS"].split(",") if host.strip()]
CHUNK_SIZE = 1024 * 1024


class TransferError(ValueError):
    pass


def load_profiles() -> dict[str, dict]:
    payload = json.loads(PROFILE_CONFIG_PATH.read_text())
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise RuntimeError("transfer profiles are missing")
    return profiles


PROFILES = load_profiles()


def profile(name: str) -> dict:
    selected = PROFILES.get(name)
    if not isinstance(selected, dict):
        raise TransferError("unknown transfer profile")
    return selected


def artifact_path(value: str, *, must_exist: bool) -> Path:
    if not isinstance(value, str) or not value:
        raise TransferError("artifact path must be a non-empty relative path")
    candidate = (ARTIFACT_ROOT / value).resolve()
    if ARTIFACT_ROOT not in candidate.parents:
        raise TransferError("artifact path is outside the artifact root")
    if must_exist and not candidate.is_file():
        raise TransferError("artifact does not exist")
    if not must_exist and candidate.exists():
        raise TransferError("destination artifact already exists")
    return candidate


def signed_target(profile_name: str, value: str) -> tuple[dict, str, str]:
    if not isinstance(value, str):
        raise TransferError("signed URL must be a string")
    selected = profile(profile_name)
    parsed = urlsplit(value)
    allowed_hosts = {host.lower() for host in selected.get("allowedHosts", [])}
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.hostname is None
        or parsed.hostname.lower() not in allowed_hosts
    ):
        raise TransferError("signed URL is not an allowed HTTPS endpoint")
    decoded_path = unquote(parsed.path)
    if not any(decoded_path.startswith(prefix) for prefix in selected.get("pathPrefixes", [])):
        raise TransferError("signed URL is outside the allowed object prefix")
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    for parameter in selected.get("requiredQueryParameters", []):
        if not parameters.get(parameter):
            raise TransferError("signed URL is missing required authorization parameters")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return selected, parsed.hostname, target


def connection(host: str) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(host, timeout=60)


def ensure_size(size: int, selected: dict) -> None:
    if size < 0 or size > int(selected["maxBytes"]):
        raise TransferError("artifact exceeds the configured transfer limit")


def put_file(source: Path, host: str, target: str, selected: dict) -> int:
    size = source.stat().st_size
    ensure_size(size, selected)
    client = connection(host)
    try:
        client.putrequest("PUT", target, skip_host=True, skip_accept_encoding=True)
        client.putheader("Host", host)
        client.putheader("Content-Length", str(size))
        client.endheaders()
        with source.open("rb") as artifact:
            while chunk := artifact.read(CHUNK_SIZE):
                client.send(chunk)
        response = client.getresponse()
        response.read()
    except OSError as error:
        raise TransferError("upload connection failed") from error
    finally:
        client.close()
    if not 200 <= response.status < 300:
        raise TransferError(f"upload endpoint returned HTTP {response.status}")
    return size


def get_response(host: str, target: str) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse]:
    client = connection(host)
    try:
        client.request("GET", target, headers={"Host": host})
        return client, client.getresponse()
    except OSError as error:
        client.close()
        raise TransferError("download connection failed") from error


def response_size(response: http.client.HTTPResponse, selected: dict) -> int:
    value = response.getheader("Content-Length")
    if value is None:
        raise TransferError("download endpoint did not provide Content-Length")
    try:
        size = int(value)
    except ValueError as error:
        raise TransferError("download endpoint returned invalid Content-Length") from error
    ensure_size(size, selected)
    return size


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
    """Return non-sensitive metadata for an artifact under the shared artifact root."""
    source = artifact_path(artifact, must_exist=True)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return {"artifact": artifact, "bytes": source.stat().st_size, "sha256": digest.hexdigest()}


@mcp.tool
def upload_artifact(profile_name: str, artifact: str, signed_put_url: str) -> dict:
    """Upload an artifact with a caller-supplied, profile-validated signed PUT URL."""
    source = artifact_path(artifact, must_exist=True)
    selected, host, target = signed_target(profile_name, signed_put_url)
    return {"status": "uploaded", "bytes": put_file(source, host, target, selected)}


@mcp.tool
def verify_download(profile_name: str, signed_get_url: str) -> dict:
    """Check a signed GET URL without exposing its object bytes."""
    selected, host, target = signed_target(profile_name, signed_get_url)
    client, response = get_response(host, target)
    try:
        if not 200 <= response.status < 300:
            raise TransferError(f"download endpoint returned HTTP {response.status}")
        size = response_size(response, selected)
        response.read(1)
        return {"status": "available", "bytes": size}
    finally:
        client.close()


@mcp.tool
def download_artifact(profile_name: str, signed_get_url: str, destination: str) -> dict:
    """Download a profile-validated signed URL into a new artifact path."""
    selected, host, target = signed_target(profile_name, signed_get_url)
    destination_path = artifact_path(destination, must_exist=False)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    client, response = get_response(host, target)
    temporary_path = None
    try:
        if not 200 <= response.status < 300:
            raise TransferError(f"download endpoint returned HTTP {response.status}")
        size = response_size(response, selected)
        with tempfile.NamedTemporaryFile(dir=destination_path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            shutil.copyfileobj(response, temporary, CHUNK_SIZE)
        temporary_path.replace(destination_path)
        return {"status": "downloaded", "artifact": destination, "bytes": size}
    finally:
        client.close()
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


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
