"""
Cloudflare API client for tunnel and access data.

Provides async functions to fetch tunnel status, public hostnames, local port
mappings, access policies summary, and last reconnect timestamp from the
Cloudflare API v4.

Uses environment variables for configuration:
  CF_API_TOKEN   — Cloudflare API token (required)
  CF_ACCOUNT_ID  — Cloudflare account ID (required)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_TIMEOUT = 30.0


# ── error types ──────────────────────────────────────────────────────────────

class CloudflareAPIError(Exception):
    """Base exception for all Cloudflare API errors."""


class AuthError(CloudflareAPIError):
    """Invalid or missing API token."""


class NotFoundError(CloudflareAPIError):
    """Requested resource (tunnel, account) does not exist."""


class NetworkError(CloudflareAPIError):
    """Network-level failure (DNS, connection refused, timeout)."""


class APIRequestError(CloudflareAPIError):
    """Generic API error (4xx/5xx) with a message from Cloudflare."""


# ── data models ──────────────────────────────────────────────────────────────

@dataclass
class TunnelStatus:
    tunnel_id: str
    tunnel_name: str
    is_up: bool
    connections: list[ConnectionInfo] = field(default_factory=list)

    @classmethod
    def from_api_response(cls, tunnel: dict, connections: dict) -> TunnelStatus:
        conn_list = [
            ConnectionInfo(
                connection_id=conn.get("id", ""),
                client_id=conn.get("client_id", ""),
                arch=conn.get("arch", ""),
                version=conn.get("run_tunnel_version", ""),
                origin_ip=conn.get("origin_ip", ""),
                opened_at=conn.get("opened_at", ""),
            )
            for conn in connections.get("result", [])
        ]
        is_up = len(conn_list) > 0
        return cls(
            tunnel_id=tunnel["id"],
            tunnel_name=tunnel.get("name", ""),
            is_up=is_up,
            connections=conn_list,
        )


@dataclass
class ConnectionInfo:
    connection_id: str
    client_id: str
    arch: str
    version: str
    origin_ip: str
    opened_at: str


@dataclass
class PublicHostname:
    hostname: str
    service: str
    origin_request: Optional[dict] = None

    @classmethod
    def from_ingress_rule(cls, rule: dict) -> PublicHostname:
        return cls(
            hostname=rule.get("hostname", ""),
            service=rule.get("service", ""),
            origin_request=rule.get("originRequest"),
        )


@dataclass
class TunnelConfig:
    tunnel_id: str
    hostnames: list[PublicHostname] = field(default_factory=list)
    port_mappings: list[PortMapping] = field(default_factory=list)

    @classmethod
    def from_api_response(cls, tunnel_id: str, config_data: dict) -> TunnelConfig:
        config = config_data.get("result", {}).get("config", {})
        ingress_rules = config.get("ingress", [])
        hostnames = []
        port_mappings = []
        for rule in ingress_rules:
            h = rule.get("hostname", "")
            s = rule.get("service", "")
            if h and s:
                hostnames.append(PublicHostname.from_ingress_rule(rule))
                # Detect port mappings: services like "localhost:8080", "tcp://localhost:22"
                if "://" in s:
                    # tcp:// or ssh:// — capture protocol + host:port
                    pm = PortMapping.from_service(s)
                    if pm:
                        port_mappings.append(pm)
                elif ":" in s:
                    # localhost:PORT or just HOST:PORT
                    pm = PortMapping.from_service(s)
                    if pm:
                        port_mappings.append(pm)
        return cls(
            tunnel_id=tunnel_id,
            hostnames=hostnames,
            port_mappings=port_mappings,
        )


@dataclass
class PortMapping:
    protocol: str  # "tcp", "ssh", "rdp", "http", or "unknown"
    host: str      # e.g. "localhost"
    port: int

    @classmethod
    def from_service(cls, service: str) -> Optional[PortMapping]:
        """Parse a Cloudflare tunnel service string like 'tcp://localhost:22'."""
        if not service:
            return None
        proto = "unknown"
        rest = service
        if "://" in service:
            proto, rest = service.split("://", 1)
        if ":" not in rest:
            return None
        host, _, port_str = rest.rpartition(":")
        try:
            port = int(port_str)
        except ValueError:
            return None
        return cls(protocol=proto, host=host or "localhost", port=port)


@dataclass
class AccessPolicyInfo:
    policy_id: str
    name: str
    decision: str      # "allow", "deny", "non_identity", "bypass"
    include_count: int
    exclude_count: int
    require_count: int


@dataclass
class AccessPoliciesSummary:
    total_policies: int
    policies: list[AccessPolicyInfo] = field(default_factory=list)
    types_breakdown: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_api_response(cls, policies_data: list[dict]) -> AccessPoliciesSummary:
        policies = []
        types_breakdown: dict[str, int] = {}
        for p in policies_data:
            decision = p.get("decision", "unknown")
            types_breakdown[decision] = types_breakdown.get(decision, 0) + 1
            include = p.get("include", [])
            exclude = p.get("exclude", [])
            require = p.get("require", [])
            policies.append(
                AccessPolicyInfo(
                    policy_id=p.get("id", ""),
                    name=p.get("name", ""),
                    decision=decision,
                    include_count=len(include) if include else 0,
                    exclude_count=len(exclude) if exclude else 0,
                    require_count=len(require) if require else 0,
                )
            )
        return cls(
            total_policies=len(policies),
            policies=policies,
            types_breakdown=types_breakdown,
        )


@dataclass
class ReconnectInfo:
    tunnel_id: str
    last_reconnect_at: Optional[str]  # ISO 8601 or None if never connected
    connection_count: int

    @classmethod
    def from_api_response(
        cls, tunnel_id: str, connections_data: dict
    ) -> ReconnectInfo:
        conns = connections_data.get("result", [])
        if not conns:
            return cls(tunnel_id=tunnel_id, last_reconnect_at=None, connection_count=0)
        # The most recent connection's opened_at is the last (re)connect time
        latest: Optional[str] = None
        for c in conns:
            opened = c.get("opened_at", "")
            if opened and (latest is None or opened > latest):
                latest = opened
        return cls(
            tunnel_id=tunnel_id,
            last_reconnect_at=latest,
            connection_count=len(conns),
        )


# ── internal helpers ─────────────────────────────────────────────────────────

def _auth_headers(api_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    api_token: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Make an authenticated request to Cloudflare API and handle errors."""
    url = f"{API_BASE}{path}"
    try:
        response = await client.request(
            method, url, headers=_auth_headers(api_token), timeout=timeout
        )
    except httpx.TimeoutException:
        raise NetworkError(f"Request timed out after {timeout}s: {method} {url}")
    except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        raise NetworkError(f"Network error reaching Cloudflare API: {exc}")

    # Auth errors return 401 or 403 with specific messages
    if response.status_code in (401, 403):
        body = response.json()
        errors = body.get("errors", [{}])
        msg = errors[0].get("message", "Invalid or missing API token")
        raise AuthError(f"Authentication failed: {msg}")

    if response.status_code == 404:
        body = response.json()
        errors = body.get("errors", [{}])
        msg = errors[0].get("message", "Resource not found")
        raise NotFoundError(f"Not found: {msg}")

    if response.status_code >= 400:
        body = response.json()
        errors = body.get("errors", [{}])
        msg = errors[0].get("message", f"HTTP {response.status_code}")
        raise APIRequestError(f"API error: {msg}")

    return response.json()


# ── public API ───────────────────────────────────────────────────────────────

async def get_tunnel_status(
    *,
    api_token: str | None = None,
    account_id: str | None = None,
    tunnel_id: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> TunnelStatus:
    """Get tunnel connection status (up/down) and active connections.

    Returns a TunnelStatus indicating whether the tunnel has at least one
    active connection.  If *tunnel_id* is omitted, returns the first tunnel
    found on the account (useful for single-tunnel setups).

    Raises:
        AuthError: if the API token is invalid.
        NotFoundError: if the tunnel or account doesn't exist.
        NetworkError: on DNS, connection, or timeout failures.
        APIRequestError: on other 4xx/5xx responses.
    """
    api_token = api_token or os.environ["CF_API_TOKEN"]
    account_id = account_id or os.environ["CF_ACCOUNT_ID"]
    _own_client = client is None
    client = client or httpx.AsyncClient()

    try:
        if tunnel_id:
            path = f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}"
            tunnel_data = await _request(client, "GET", path, api_token, timeout)
            tunnel = tunnel_data["result"]
        else:
            path = f"/accounts/{account_id}/cfd_tunnel"
            data = await _request(client, "GET", path, api_token, timeout)
            tunnels = data.get("result", [])
            if not tunnels:
                raise NotFoundError("No tunnels found on this account")
            tunnel = tunnels[0]

        tid = tunnel["id"]
        conn_path = f"/accounts/{account_id}/cfd_tunnel/{tid}/connections"
        conn_data = await _request(client, "GET", conn_path, api_token, timeout)
        return TunnelStatus.from_api_response(tunnel, conn_data)
    finally:
        if _own_client:
            await client.aclose()


async def get_public_hostnames(
    *,
    api_token: str | None = None,
    account_id: str | None = None,
    tunnel_id: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> TunnelConfig:
    """Get public hostnames and their origin services for a tunnel.

    Returns a TunnelConfig containing all hostname→origin mappings plus any
    detected port mappings.

    Raises:
        AuthError: if the API token is invalid.
        NotFoundError: if the tunnel or account doesn't exist.
        NetworkError: on DNS, connection, or timeout failures.
        APIRequestError: on other 4xx/5xx responses.
        ValueError: if *tunnel_id* is empty.
    """
    if not tunnel_id:
        raise ValueError("tunnel_id is required")

    api_token = api_token or os.environ["CF_API_TOKEN"]
    account_id = account_id or os.environ["CF_ACCOUNT_ID"]
    _own_client = client is None
    client = client or httpx.AsyncClient()

    try:
        path = f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"
        config_data = await _request(client, "GET", path, api_token, timeout)
        return TunnelConfig.from_api_response(tunnel_id, config_data)
    finally:
        if _own_client:
            await client.aclose()


async def get_local_port_mappings(
    *,
    api_token: str | None = None,
    account_id: str | None = None,
    tunnel_id: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[PortMapping]:
    """Get local port mappings from a tunnel's ingress configuration.

    Parses the tunnel configuration's ingress rules to extract non-HTTP
    services (tcp://, ssh://, rdp://) and returns them as PortMapping objects.

    Raises:
        AuthError: if the API token is invalid.
        NotFoundError: if the tunnel or account doesn't exist.
        NetworkError: on DNS, connection, or timeout failures.
        APIRequestError: on other 4xx/5xx responses.
        ValueError: if *tunnel_id* is empty.
    """
    if not tunnel_id:
        raise ValueError("tunnel_id is required")

    api_token = api_token or os.environ["CF_API_TOKEN"]
    account_id = account_id or os.environ["CF_ACCOUNT_ID"]
    _own_client = client is None
    client = client or httpx.AsyncClient()

    try:
        path = f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"
        config_data = await _request(client, "GET", path, api_token, timeout)
        config_obj = TunnelConfig.from_api_response(tunnel_id, config_data)
        return config_obj.port_mappings
    finally:
        if _own_client:
            await client.aclose()


async def get_access_policies_summary(
    *,
    api_token: str | None = None,
    account_id: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> AccessPoliciesSummary:
    """Get a summary of all Zero Trust access policies on the account.

    Returns an AccessPoliciesSummary with total count, per-policy details,
    and a breakdown by decision type (allow/deny/non_identity/bypass).

    Raises:
        AuthError: if the API token is invalid.
        NotFoundError: if the account doesn't exist.
        NetworkError: on DNS, connection, or timeout failures.
        APIRequestError: on other 4xx/5xx responses.
    """
    api_token = api_token or os.environ["CF_API_TOKEN"]
    account_id = account_id or os.environ["CF_ACCOUNT_ID"]
    _own_client = client is None
    client = client or httpx.AsyncClient()

    try:
        # The Zero Trust Access policies endpoint
        path = (
            f"/accounts/{account_id}/access/policies"
        )
        data = await _request(client, "GET", path, api_token, timeout)
        return AccessPoliciesSummary.from_api_response(data.get("result", []))
    finally:
        if _own_client:
            await client.aclose()


async def get_last_reconnect_timestamp(
    *,
    api_token: str | None = None,
    account_id: str | None = None,
    tunnel_id: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> ReconnectInfo:
    """Get the last reconnect timestamp for a tunnel.

    Examines all active connections for the tunnel and returns the most recent
    ``opened_at`` timestamp, which represents when cloudflared last (re)connected.

    Raises:
        AuthError: if the API token is invalid.
        NotFoundError: if the tunnel or account doesn't exist.
        NetworkError: on DNS, connection, or timeout failures.
        APIRequestError: on other 4xx/5xx responses.
        ValueError: if *tunnel_id* is empty.
    """
    if not tunnel_id:
        raise ValueError("tunnel_id is required")

    api_token = api_token or os.environ["CF_API_TOKEN"]
    account_id = account_id or os.environ["CF_ACCOUNT_ID"]
    _own_client = client is None
    client = client or httpx.AsyncClient()

    try:
        path = f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/connections"
        conn_data = await _request(client, "GET", path, api_token, timeout)
        return ReconnectInfo.from_api_response(tunnel_id, conn_data)
    finally:
        if _own_client:
            await client.aclose()


# ── convenience: fetch all data in one call ──────────────────────────────────

@dataclass
class TunnelFullReport:
    tunnel_id: str
    status: TunnelStatus
    config: TunnelConfig
    port_mappings: list[PortMapping]
    access_policies: AccessPoliciesSummary
    reconnect: ReconnectInfo


async def get_full_report(
    *,
    api_token: str | None = None,
    account_id: str | None = None,
    tunnel_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> TunnelFullReport:
    """Fetch all five data points in a single async call using a shared client.

    This is the most efficient way to get everything at once — all requests
    share a single httpx connection pool.

    If *tunnel_id* is omitted, the first tunnel on the account is used.
    """
    api_token = api_token or os.environ["CF_API_TOKEN"]
    account_id = account_id or os.environ["CF_ACCOUNT_ID"]

    async with httpx.AsyncClient() as client:
        status = await get_tunnel_status(
            api_token=api_token,
            account_id=account_id,
            tunnel_id=tunnel_id,
            client=client,
            timeout=timeout,
        )
        tid = status.tunnel_id

        config = await get_public_hostnames(
            api_token=api_token,
            account_id=account_id,
            tunnel_id=tid,
            client=client,
            timeout=timeout,
        )
        # Reuse port mappings from config (no extra API call)
        port_mappings = config.port_mappings

        access = await get_access_policies_summary(
            api_token=api_token,
            account_id=account_id,
            client=client,
            timeout=timeout,
        )

        reconnect = await get_last_reconnect_timestamp(
            api_token=api_token,
            account_id=account_id,
            tunnel_id=tid,
            client=client,
            timeout=timeout,
        )

    return TunnelFullReport(
        tunnel_id=tid,
        status=status,
        config=config,
        port_mappings=port_mappings,
        access_policies=access,
        reconnect=reconnect,
    )
