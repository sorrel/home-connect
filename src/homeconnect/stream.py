"""Server-sent events: parsing, and reconnection timing.

The vendor's stream reports changes only — connecting to it sends nothing, which
was confirmed against the live API. Everything here is therefore about surviving
a long-lived connection: parsing what arrives, and deciding how long to wait when
it stops arriving.

This module opens no connection of its own. It is handed lines, and it yields
events, so nothing in it can reach the network.
"""

from __future__ import annotations

import json
import random
from typing import Any, Callable, Iterable, Iterator

EVENTS_PATH_SUFFIX = "/homeappliances/events"


def _decode(raw: str) -> dict | None:
    """Parse an event body, or return None if it will not parse.

    A malformed body must never end the stream: losing one event costs a
    transition, whereas raising here would cost every event after it.
    """
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_sse(lines: Iterable[str]) -> Iterator[dict]:
    """Yield `{"event": name, "data": body-or-None}` for each SSE event."""
    name: str | None = None
    body: list[str] = []

    def flush() -> dict | None:
        if name is None and not body:
            return None
        return {"event": name or "message", "data": _decode("".join(body))}

    for line in lines:
        line = line.rstrip("\n").rstrip("\r")
        if line.startswith(":"):
            continue  # an SSE comment, commonly used as a keep-alive
        if not line:
            event = flush()
            if event is not None:
                yield event
            name, body = None, []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            name = value
        elif field == "data":
            body.append(value)

    event = flush()
    if event is not None:
        yield event


def extract_changes(payload: dict | None) -> list[tuple[str, Any]]:
    """Pull `(key, value)` pairs out of an event body, tolerating odd shapes."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [
        (item["key"], item.get("value"))
        for item in items
        if isinstance(item, dict) and "key" in item
    ]


def backoff_delays(
    initial: float = 1.0,
    cap: float = 300.0,
    factor: float = 2.0,
    jitter: Callable[[float], float] | None = None,
) -> Iterator[float]:
    """Yield reconnection delays, growing geometrically to a ceiling.

    Jittered by default: without it every client that lost an outage reconnects
    in lockstep the instant the service returns, which is how an outage becomes
    a second outage.
    """
    if jitter is None:
        jitter = lambda delay: random.uniform(delay / 2, delay)  # noqa: E731

    delay = initial
    while True:
        yield jitter(delay)
        delay = min(delay * factor, cap)
