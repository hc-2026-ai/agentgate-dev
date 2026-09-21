"""Secret-safe diagnostic helpers for customer-environment troubleshooting."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import parse_qsl, urlsplit, urlunsplit


def safe_url(url: str) -> str:
    """Keep host/path and query names while dropping query values/fragments."""

    parsed = urlsplit(url)
    hostname = parsed.hostname or "<invalid-host>"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    query_names = sorted({name for name, _ in parse_qsl(parsed.query)})
    query = "&".join(f"{name}=<redacted>" for name in query_names)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def mapping_shape(value: Mapping | None) -> str:
    """Describe JSON structure without logging values."""

    if value is None:
        return "none"
    top = sorted(str(key) for key in value)
    data = value.get("data")
    if isinstance(data, Mapping):
        nested = sorted(str(key) for key in data)
        return f"keys={top!r} data_keys={nested!r}"
    return f"keys={top!r}"


def business_summary(value: Mapping) -> str:
    """Return only non-secret status/code fields from a customer response."""

    parts: list[str] = []
    for key in ("code", "resCode", "status"):
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            if key in value:
                parts.append(f"{key}={item!r}")
    data = value.get("data")
    if isinstance(data, Mapping):
        status = data.get("status")
        if isinstance(status, (str, int, float, bool)) or status is None:
            if "status" in data:
                parts.append(f"data.status={status!r}")
    return " ".join(parts) or "no_business_status"
