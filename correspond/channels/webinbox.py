"""The web inbox: a small ASGI collector web pages post reports to, and the channel that reads them.

**The collector** (:func:`mk_app`; or ``uvicorn --factory correspond.channels.webinbox:app_from_env``)
accepts ``POST /<site>/reports`` with a JSON body::

    {"text": "The export drops the last row.",
     "name": "Ada", "email": "ada@example.org",
     "page": "https://app.example.org/export",
     "context": {"viewport": "1280x800"},
     "identity": {"user": "u-42", "name": "Ada", "email": null, "issued_at": 1757592000, "sig": "<hex>"},
     "attachments": [{"name": "screen.png", "media_type": "image/png", "data": "<base64>"}]}

Only ``text`` is required.

- **Identity.** Without ``identity`` a report is ``claimed`` (``name`` and ``email`` are
  whatever was typed). With one, the host application's *server* has signed its logged-in
  user with the site's secret (:func:`sign_identity`); a valid, fresh signature makes the
  report ``bound``. An invalid or expired signature is refused with 401 and nothing is
  stored.
- **Origins.** A browser's ``Origin`` must be on the site's allowlist (403 otherwise); a
  request without one is refused unless the site allows server-to-server posts. CORS
  preflight is answered for allowed origins only. An origin check stops other pages from
  posting through a visitor's browser; it authenticates no one.
- **Limits.** A per-client token bucket (429 with ``Retry-After``), the body size (413),
  caps on text, context and attachments, and a media-type allowlist.
- **Storage.** A report is JSON in ``store`` under ``<site>/<sortable id>.json``;
  attachments are bytes in ``blobs`` under their SHA-256, referenced and never inlined.

The app binds nothing: run it on localhost behind your own server. The limiter lives in
memory, per process.

**The channel** (:class:`WebInbox`): ``webinbox:<site>`` reads and listens to what the
collector stored. It has no writer: a reply to a reporter goes out on another channel.

The identity payload is plain lines, so any server language can sign it:

>>> identity_payload("example-site", "u-42", 1757592000, name="Ada")
b'v1\\nexample-site\\n1757592000\\nu-42\\nAda\\n'
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from correspond.errors import InvalidRef, MissingRequirement
from correspond.model import (
    Attachment,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Event,
    Grade,
    HistoryDepth,
    Message,
    Support,
    format_time,
    parse_time,
)
from correspond.ops import window, with_final_cursor
from correspond.registry import value

__all__ = [
    "MEDIA_TYPES",
    "Site",
    "TokenBucket",
    "WebInbox",
    "app_from_env",
    "identity_payload",
    "mk_app",
    "sign_identity",
    "verify_identity",
]

NAME = "webinbox"
BLOBS_KIND = "webinbox-blobs"
SITE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
IDENTITY_VERSION = "v1"
MAX_TEXT_CHARS = 20_000
MAX_NAME_CHARS = 200
MAX_EMAIL_CHARS = 320
MAX_PAGE_CHARS = 2_000
MAX_CONTEXT_BYTES = 64_000
MAX_ATTACHMENTS = 5
MEDIA_TYPES = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "text/plain",
    "application/json",
)
CLOCK_SKEW_S = 300
MAX_TRACKED_CLIENTS = 10_000
BLOB_PREFIX = "blob:"


def _csv(text: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (text or "").split(",") if part.strip())


def _origin(text: str) -> str:
    return text.strip().rstrip("/").lower()


@dataclass(frozen=True, kw_only=True)
class Site:
    """One inbox the collector accepts reports for: who may post from where, and the key signed identities are checked with."""

    name: str
    origins: tuple[str, ...] = ()
    secret: bytes | None = None
    allow_no_origin: bool = False
    max_age_s: int = 86_400

    def __post_init__(self):
        if not SITE_RE.match(self.name):
            raise ValueError(
                f"site names are lowercase letters, digits and '-', not {self.name!r}"
            )
        object.__setattr__(self, "origins", tuple(_origin(o) for o in self.origins))
        if isinstance(self.secret, str):
            object.__setattr__(self, "secret", self.secret.encode("utf-8"))


# -------------------------------------------------------------------------- identity


def identity_payload(
    site: str,
    user: str,
    issued_at: int,
    *,
    name: str | None = None,
    email: str | None = None,
) -> bytes:
    """The bytes a host application signs for its logged-in user: ``v1``, site, issued-at, user, name, email, one per line."""
    fields = (
        IDENTITY_VERSION,
        site,
        str(int(issued_at)),
        str(user),
        name or "",
        email or "",
    )
    if any("\n" in f or "\r" in f for f in fields):
        raise ValueError("identity fields cannot contain line breaks")
    return "\n".join(fields).encode("utf-8")


def sign_identity(
    secret: str | bytes,
    site: str,
    user: str,
    *,
    name: str | None = None,
    email: str | None = None,
    issued_at: float | None = None,
) -> dict:
    """What a host application's server gives its page for the logged-in user; the page sends it as ``identity``."""
    issued = int(time.time() if issued_at is None else issued_at)
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    payload = identity_payload(site, user, issued, name=name, email=email)
    return {
        "user": str(user),
        "name": name,
        "email": email,
        "issued_at": issued,
        "sig": hmac.new(key, payload, hashlib.sha256).hexdigest(),
    }


def _forged(reason: str) -> Authenticity:
    return Authenticity(grade=Grade.FORGED, evidence={"reason": reason})


def verify_identity(
    identity: Mapping[str, Any], *, site: Site, now: float
) -> Authenticity:
    """``bound`` for a valid, fresh signature from the site's host application; ``forged`` with the reason otherwise."""
    if site.secret is None:
        return _forged("this inbox has no secret, so it cannot check a signed identity")
    try:
        user, received = identity["user"], identity["sig"]
        name, email = identity.get("name"), identity.get("email")
        if not isinstance(user, str) or not user or not isinstance(received, str):
            raise TypeError
        if not all(v is None or isinstance(v, str) for v in (name, email)):
            raise TypeError
        issued_at = int(identity["issued_at"])
        payload = identity_payload(site.name, user, issued_at, name=name, email=email)
    except (KeyError, TypeError, ValueError, OverflowError):
        return _forged("the identity is malformed")
    expected = hmac.new(site.secret, payload, hashlib.sha256).hexdigest()
    if not received.isascii() or not hmac.compare_digest(received.lower(), expected):
        return _forged("the signature does not match")
    age = now - issued_at
    if age > site.max_age_s:
        return _forged("the signed identity has expired")
    if age < -CLOCK_SKEW_S:
        return _forged("the signed identity is dated in the future")
    return Authenticity(
        grade=Grade.BOUND,
        evidence={"method": "hmac-sha256", "user": user, "issued_at": issued_at},
    )


# ------------------------------------------------------------------------ collector


class TokenBucket:
    """Per-key token buckets: ``rate_per_minute`` sustained, ``burst`` at once. In memory, one process.

    >>> bucket = TokenBucket(rate_per_minute=60, burst=1, clock=lambda: 0.0)
    >>> bucket.take("client"), bucket.take("client")
    (None, 1.0)
    """

    def __init__(
        self,
        *,
        rate_per_minute: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate = rate_per_minute / 60.0
        self.burst = max(1, int(burst))
        self.clock = clock
        self._buckets: dict[Any, tuple[float, float]] = {}

    def take(self, key: Any) -> float | None:
        """``None`` when a token was available (and is now spent); otherwise the seconds until one is."""
        now = self.clock()
        tokens, updated = self._buckets.get(key, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - updated) * self.rate)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            if len(self._buckets) > MAX_TRACKED_CLIENTS:
                self._forget_full(now)
            return None
        self._buckets[key] = (tokens, now)
        return (1 - tokens) / self.rate

    def _forget_full(self, now: float) -> None:
        for key, (tokens, updated) in list(self._buckets.items()):
            if tokens + (now - updated) * self.rate >= self.burst:
                del self._buckets[key]


class _Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _optional_text(payload: Mapping[str, Any], key: str, limit: int) -> str | None:
    found = payload.get(key)
    if found is None or found == "":
        return None
    if not isinstance(found, str):
        raise _Refused(400, f"{key} must be a string")
    if len(found) > limit:
        raise _Refused(400, f"{key} is longer than {limit} characters")
    return found


def _report(
    site: Site, payload: Mapping[str, Any], *, now: float
) -> tuple[dict, dict[str, bytes]]:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _Refused(400, "text is required")
    if len(text) > MAX_TEXT_CHARS:
        raise _Refused(400, f"text is longer than {MAX_TEXT_CHARS} characters")
    page = _optional_text(payload, "page", MAX_PAGE_CHARS)
    identity = payload.get("identity")
    if identity is not None:
        if not isinstance(identity, dict):
            raise _Refused(400, "identity must be an object")
        authenticity = verify_identity(identity, site=site, now=now)
        if authenticity.grade is Grade.FORGED:
            raise _Refused(
                401, f"identity not accepted: {authenticity.evidence['reason']}"
            )
        name, email = identity.get("name"), identity.get("email")
        author = ChannelIdentity(
            channel=NAME, native_id=identity["user"], display_name=name or None
        )
        contact = {"name": name, "email": email, "signed": True}
    else:
        name = _optional_text(payload, "name", MAX_NAME_CHARS)
        email = _optional_text(payload, "email", MAX_EMAIL_CHARS)
        authenticity = Authenticity(
            grade=Grade.CLAIMED, evidence={"reason": "no signed identity"}
        )
        author = ChannelIdentity(channel=NAME, native_id="", display_name=name)
        contact = {"name": name, "email": email, "signed": False}
    context = payload.get("context")
    if context is not None and len(json.dumps(context)) > MAX_CONTEXT_BYTES:
        raise _Refused(400, f"context is larger than {MAX_CONTEXT_BYTES} bytes")
    items = payload.get("attachments") or []
    if not isinstance(items, list) or len(items) > MAX_ATTACHMENTS:
        raise _Refused(400, f"attachments must be a list of at most {MAX_ATTACHMENTS}")
    attachments, files = [], {}
    for item in items:
        if not isinstance(item, dict):
            raise _Refused(400, "each attachment must be an object")
        media_type = str(item.get("media_type") or "").lower()
        if media_type not in MEDIA_TYPES:
            raise _Refused(415, f"attachments may be {', '.join(MEDIA_TYPES)}")
        try:
            data = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            raise _Refused(400, "attachment data must be base64") from None
        if not data:
            raise _Refused(400, "an attachment is empty")
        digest = hashlib.sha256(data).hexdigest()
        files[digest] = data
        attachments.append(
            {
                "ref": BLOB_PREFIX + digest,
                "media_type": media_type,
                "name": _optional_text(item, "name", MAX_NAME_CHARS),
                "size": len(data),
                "sha256": digest,
            }
        )
    received = datetime.fromtimestamp(now, tz=timezone.utc)
    record = {
        "id": f"{received:%Y%m%dT%H%M%S%fZ}-{secrets.token_hex(4)}",
        "site": site.name,
        "received_at": format_time(received),
        "text": text,
        "page": page,
        "context": context,
        "contact": contact,
        "author": author.to_dict(),
        "authenticity": authenticity.to_dict(),
        "attachments": attachments,
    }
    return record, files


async def _respond(
    send, status: int, payload: Any, headers: Mapping[str, str] | None = None
) -> None:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    raw = [(b"content-type", b"application/json")] if payload is not None else []
    raw += [
        (k.encode("latin-1"), str(v).encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    raw.append((b"content-length", str(len(body)).encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


_TOO_LARGE = object()


async def _read_body(receive, limit: int) -> Any:
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body", b"") or b""
        size += len(chunk)
        if size > limit:
            return _TOO_LARGE
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _client(
    scope: Mapping[str, Any], headers: Mapping[str, str], trust_forwarded: bool
) -> str:
    if trust_forwarded and headers.get("x-forwarded-for"):
        return headers["x-forwarded-for"].split(",")[0].strip()
    client = scope.get("client") or ("unknown", 0)
    return str(client[0])


def mk_app(
    sites: Iterable[Site],
    *,
    store: MutableMapping[str, Any] | None = None,
    blobs: MutableMapping[str, bytes] | None = None,
    rate_per_minute: float = 10,
    burst: int = 5,
    max_body_bytes: int = 5_000_000,
    trust_forwarded: bool = False,
    clock: Callable[[], float] = time.time,
) -> Callable[..., Awaitable[None]]:
    """The collector as an ASGI app, storing reports in ``store`` and attachments in ``blobs`` (files under the data root by default)."""
    by_name = {site.name: site for site in sites}
    if not by_name:
        raise ValueError("the collector needs at least one site")
    if store is None or blobs is None:
        from correspond.stores import bytes_store, json_store

        store = json_store(NAME) if store is None else store
        blobs = bytes_store(BLOBS_KIND) if blobs is None else blobs
    limiter = TokenBucket(rate_per_minute=rate_per_minute, burst=burst)

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers") or ()
        }
        parts = [p for p in str(scope.get("path", "")).split("/") if p]
        site = (
            by_name.get(parts[0]) if len(parts) == 2 and parts[1] == "reports" else None
        )
        if site is None:
            return await _respond(send, 404, {"error": "no such inbox"})
        origin = headers.get("origin")
        allowed = (
            (_origin(origin) in site.origins)
            if origin is not None
            else site.allow_no_origin
        )
        cors = (
            {"access-control-allow-origin": origin, "vary": "Origin"}
            if origin is not None and allowed
            else {}
        )
        method = scope.get("method", "GET")
        if method == "OPTIONS":
            if not cors:
                return await _respond(send, 403, {"error": "origin not allowed"})
            return await _respond(
                send,
                204,
                None,
                {
                    **cors,
                    "access-control-allow-methods": "POST, OPTIONS",
                    "access-control-allow-headers": "content-type",
                    "access-control-max-age": "600",
                },
            )
        if method != "POST":
            return await _respond(
                send, 405, {"error": "use POST"}, {**cors, "allow": "POST, OPTIONS"}
            )
        if not allowed:
            reason = (
                "origin not allowed"
                if origin is not None
                else "reports must come from an allowed page"
            )
            return await _respond(send, 403, {"error": reason})
        wait = limiter.take((site.name, _client(scope, headers, trust_forwarded)))
        if wait is not None:
            return await _respond(
                send,
                429,
                {"error": "too many reports; try again later"},
                {**cors, "retry-after": str(max(1, math.ceil(wait)))},
            )
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_body_bytes:
            return await _respond(
                send,
                413,
                {"error": f"reports are limited to {max_body_bytes} bytes"},
                cors,
            )
        if (
            headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            return await _respond(
                send, 415, {"error": "send JSON (content-type: application/json)"}, cors
            )
        body = await _read_body(receive, max_body_bytes)
        if body is None:
            return None
        if body is _TOO_LARGE:
            return await _respond(
                send,
                413,
                {"error": f"reports are limited to {max_body_bytes} bytes"},
                cors,
            )
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return await _respond(send, 400, {"error": "the body is not JSON"}, cors)
        if not isinstance(payload, dict):
            return await _respond(
                send, 400, {"error": "the body must be a JSON object"}, cors
            )
        try:
            record, files = _report(site, payload, now=clock())
        except _Refused as refusal:
            return await _respond(send, refusal.status, {"error": refusal.message}, cors)
        for digest, data in files.items():
            blobs[digest] = data
        store[f"{site.name}/{record['id']}.json"] = record
        return await _respond(send, 201, {"id": record["id"]}, cors)

    return app


def app_from_env() -> Callable[..., Awaitable[None]]:
    """The collector configured from the environment and the ``[webinbox]`` config table (for ``uvicorn --factory``)."""
    names = _csv(value(NAME, "sites"))
    if not names:
        raise MissingRequirement(
            NAME,
            "no sites are configured",
            fix="set CORRESPOND_WEBINBOX_SITES (comma-separated site names), or sites under [webinbox] in the config file",
        )
    secret = value(NAME, "secret")
    sites = [
        Site(
            name=name,
            origins=_csv(value(NAME, "origins")),
            secret=secret or None,
            max_age_s=int(value(NAME, "max_age_s") or 86_400),
        )
        for name in names
    ]
    return mk_app(
        sites,
        rate_per_minute=float(value(NAME, "rate_per_minute") or 10),
        burst=int(value(NAME, "burst") or 5),
        max_body_bytes=int(value(NAME, "max_body_bytes") or 5_000_000),
        trust_forwarded=(value(NAME, "trust_forwarded") or "").lower()
        in ("1", "true", "yes", "on"),
    )


# -------------------------------------------------------------------------- channel


class WebInbox:
    """Reports the collector stored, as a channel: read and listen per site."""

    name = NAME

    def __init__(
        self,
        *,
        store: MutableMapping[str, Any] | None = None,
        blobs: MutableMapping[str, bytes] | None = None,
    ):
        self._store = store
        self._blobs = blobs

    @property
    def store(self) -> MutableMapping[str, Any]:
        """The report store (JSON files under the data root by default)."""
        if self._store is None:
            from correspond.stores import json_store

            self._store = json_store(NAME)
        return self._store

    @property
    def blobs(self) -> MutableMapping[str, bytes]:
        """The attachment store, keyed by SHA-256."""
        if self._blobs is None:
            from correspond.stores import bytes_store

            self._blobs = bytes_store(BLOBS_KIND)
        return self._blobs

    @property
    def capabilities(self) -> Capabilities:
        """Read and listen; no writer."""
        return Capabilities(
            channel=NAME,
            read=Support.FULL,
            listen=Support.FULL,
            history_depth=HistoryDepth.FULL,
            listen_modes=("poll",),
            grades=(Grade.CLAIMED, Grade.BOUND),
            formats=("plain",),
            notes=(
                "no writer: reply to a reporter on another channel (the email or handle they gave, when signed)",
                "reports live where the collector runs; read them there, or point store= at a shared store",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """A site name."""
        if not SITE_RE.match(id or ""):
            raise InvalidRef(
                f"webinbox references name a site, webinbox:<site>, not webinbox:{id}"
            )
        return ConversationRef(channel=NAME, id=id, kind="inbox")

    def _keys(self, site: str) -> list[str]:
        prefix = f"{site}/"
        return sorted(
            k for k in self.store if k.startswith(prefix) and k.endswith(".json")
        )

    def _message(self, ref: ConversationRef, record: Mapping[str, Any]) -> Message:
        attachments = tuple(
            Attachment(
                ref=a["ref"],
                media_type=a.get("media_type") or "application/octet-stream",
                name=a.get("name"),
                size=a.get("size"),
                sha256=a.get("sha256"),
                loader=functools.partial(self.blobs.__getitem__, a.get("sha256")),
            )
            for a in record.get("attachments") or ()
        )
        return Message(
            id=record["id"],
            conversation=ref,
            author=ChannelIdentity.from_dict(record["author"]),
            authenticity=Authenticity.from_dict(record["authenticity"]),
            sent_at=parse_time(record["received_at"]),
            text=record.get("text") or "",
            attachments=attachments,
            native={
                "page": record.get("page"),
                "context": record.get("context"),
                "contact": record.get("contact"),
            },
        )

    def read(self, ref: ConversationRef, *, since=None, limit=None) -> list[Message]:
        """The site's reports, oldest first."""
        return window(
            (self._message(ref, self.store[k]) for k in self._keys(ref.id)),
            since=since,
            limit=limit,
        )

    def poll(
        self, ref: ConversationRef, *, cursor: str | None = None, limit: int | None = None
    ):
        """Reports stored after the one ``cursor`` names (a report id)."""
        keys = [
            k
            for k in self._keys(ref.id)
            if cursor is None or k.rsplit("/", 1)[1][: -len(".json")] > cursor
        ]
        if limit:
            keys = keys[:limit]
        events = []
        for key in keys:
            report_id = key.rsplit("/", 1)[1][: -len(".json")]
            events.append(
                Event(
                    kind="message.created",
                    channel=NAME,
                    delivery_id=f"webinbox:{ref.id}:{report_id}",
                    cursor=report_id,
                    message=self._message(ref, self.store[key]),
                )
            )
        return with_final_cursor(events, None)
