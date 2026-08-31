"""Expose the Plus AI Presentation APIs as a small, stateless MCP server."""

from __future__ import annotations

import os
import re
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

API_BASE_URL = "https://api.plusdocs.com/r/v0"
API_KEY_ENV = "PLUSAI_API_KEY"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

mcp = FastMCP(
    "Plus AI Presentation API",
    instructions=(
        "Create or inspect Plus AI presentation jobs. Presentation Agent sessions "
        "are asynchronous; use get_presentation_agent_session until status is DONE."
    ),
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
    json_response=True,
    # ToolHive's proxy reaches this backend through a ClusterIP Service. The
    # VirtualMCPServer is the existing cluster-internal trust boundary, so the
    # backend cannot use a stable Host header allowlist.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _api_key() -> str:
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise RuntimeError(f"{API_KEY_ENV} is not configured")
    return key


def _identifier(value: str, field_name: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} contains invalid characters")
    return value


async def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Accept": "application/json",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            response = await client.request(
                method,
                f"{API_BASE_URL}{path}",
                headers=headers,
                json=payload,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise RuntimeError(f"Plus AI API request failed with status {error.response.status_code}") from error
    except httpx.HTTPError as error:
        raise RuntimeError("Plus AI API request failed") from error

    try:
        body = response.json()
    except ValueError as error:
        raise RuntimeError("Plus AI API returned an invalid response") from error
    if not isinstance(body, dict):
        raise RuntimeError("Plus AI API returned an unexpected response")
    return body


@mcp.tool()
async def create_presentation(
    prompt: str,
    number_of_slides: int | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Start a template-based presentation job and return its polling URL."""
    payload: dict[str, Any] = {"prompt": prompt}
    if number_of_slides is not None:
        payload["numberOfSlides"] = number_of_slides
    if language is not None:
        payload["language"] = language
    return await _request("POST", "/presentation", payload)


@mcp.tool()
async def get_presentation(presentation_id: str) -> dict[str, Any]:
    """Get a template-based presentation job, including its PPTX URL when complete."""
    return await _request("GET", f"/presentation/{_identifier(presentation_id, 'presentation_id')}")


@mcp.tool()
async def create_presentation_with_agent(
    prompt: str,
    language: str | None = None,
    pptx_file_id: str | None = None,
) -> dict[str, Any]:
    """Start an asynchronous Presentation Agent session that can return PPTX and PDF URLs."""
    payload: dict[str, Any] = {"prompt": prompt}
    if language is not None:
        payload["language"] = language
    if pptx_file_id is not None:
        payload["pptxFileId"] = _identifier(pptx_file_id, "pptx_file_id")
    return await _request("POST", "/agent/sessions", payload)


@mcp.tool()
async def get_presentation_agent_session(session_id: str) -> dict[str, Any]:
    """Get Presentation Agent session state and result URLs when its status is DONE."""
    return await _request("GET", f"/agent/sessions/{_identifier(session_id, 'session_id')}")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
