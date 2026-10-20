"""A small HTTP client over ``urllib``, shared by the HTTP adapters, with failures classified.

Adapters take it as ``http=``. The call shape is
``http(method, url, *, headers=None, body=None, timeout=DEFAULT_TIMEOUT_S) -> Response``:
an HTTP error status comes back as a ``Response`` like any other, and only "could not reach
the server" raises, as a ``ChannelError`` of kind ``network`` that names the host and never
the URL (a URL path can carry a token). Tests pass a scripted function of the same shape.

>>> classify(429, "slow down", {"retry-after": "3"}).retry_after
3.0
>>> classify(404, "gone").kind
'not_found'
"""

from __future__ import annotations

import http.client
import json
import re
import secrets
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from correspond.errors import ChannelError

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "Response",
    "classify",
    "retry_after_seconds",
    "urllib_http",
]

DEFAULT_TIMEOUT_S = 30
USER_AGENT = "correspond (+https://github.com/thorwhalen/correspond)"


@dataclass(frozen=True)
class Response:
    """An HTTP response: status, lower-cased headers, raw body."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        """The body parsed as JSON, or ``None`` when it is not JSON."""
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


def _lowered(headers: Any) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()} if headers else {}


#: Request headers that carry a credential: never sent on to another host by a redirect.
CREDENTIAL_HEADERS = ("authorization", "proxy-authorization", "cookie")


class _CredentialSafeRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects as urllib does, but drop credentials when one leaves the host.

    urllib re-sends every request header to the redirect target, so a token in
    ``Authorization`` would reach whatever host a redirect names. A redirect to another
    host, port or scheme goes on without :data:`CREDENTIAL_HEADERS`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        following = super().redirect_request(req, fp, code, msg, headers, newurl)
        if following is not None and not _same_origin(req.full_url, following.full_url):
            for name in list(following.headers):
                if name.lower() in CREDENTIAL_HEADERS:
                    following.remove_header(name)
        return following


def _same_origin(url: str, other: str) -> bool:
    """Whether two URLs share scheme, host and port.

    >>> _same_origin("https://slack.com/api/a", "https://slack.com/api/b")
    True
    >>> _same_origin("https://slack.com/api/a", "https://example.org/a")
    False
    >>> _same_origin("https://slack.com/a", "http://slack.com/a")
    False
    """
    first, second = urlsplit(url), urlsplit(other)
    return (first.scheme, first.hostname, first.port) == (
        second.scheme,
        second.hostname,
        second.port,
    )


_OPENER = urllib.request.build_opener(_CredentialSafeRedirects)


def urllib_http(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Response:
    """One request with the standard library."""
    host = urlsplit(url).hostname
    try:
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
        )
        with _OPENER.open(request, timeout=timeout) as reply:
            return Response(reply.status, _lowered(reply.headers), reply.read())
    except urllib.error.HTTPError as error:
        try:
            payload = error.read()
        except OSError:
            payload = b""
        return Response(error.code, _lowered(error.headers), payload or b"")
    except (ValueError, http.client.InvalidURL):
        # A malformed URL: its error text would carry the whole URL (a webhook's secret
        # path, say), so only the host is named.
        raise ChannelError(
            f"the address for {host} is malformed", kind="validation"
        ) from None
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
    ) as error:
        reason = getattr(error, "reason", None) or type(error).__name__
        raise ChannelError(
            f"could not reach {host}: {reason}",
            kind="network",
            retryable=True,
        ) from None


#: A media type with no parameters: ``type/subtype`` in RFC 6838's restricted names.
MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$"
)


def file_name(name: str) -> str:
    """``name`` checked as a file's display name: not empty, no path, no control or format characters.

    Format characters include the bidirectional overrides that make ``invoice\u202efdp.exe``
    display as ``invoiceexe.pdf``.

    >>> file_name(" report.pdf ")
    'report.pdf'
    """
    text = str(name or "").strip()
    if (
        not text
        or "/" in text
        or "\\" in text
        or text in (".", "..")
        or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text)
    ):
        raise ChannelError(
            f"a file name is a name without a path, control or format characters, not {name!r}",
            kind="validation",
        )
    return text


def media_type(text: str) -> str:
    """``text`` checked as a bare ``type/subtype`` (no parameters), lower-cased.

    >>> media_type("Image/PNG")
    'image/png'
    """
    if not isinstance(text, str) or not MEDIA_TYPE_RE.match(text):
        raise ChannelError(
            f"a media type is type/subtype, such as application/pdf, not {text!r}",
            kind="validation",
        )
    return text.lower()


def multipart(
    fields: Mapping[str, str], files: Mapping[str, tuple[str, bytes, str]]
) -> tuple[str, bytes]:
    """A ``multipart/form-data`` body: ``(content type, body)`` for text ``fields`` and ``files`` (name, bytes, media type).

    A file name goes as UTF-8 with ``"``, CR and LF percent-escaped, as browsers send it
    (the HTML form-data rules), so no name can break the header.

    >>> kind, body = multipart({"chat_id": "1"}, {"document": ("a.txt", b"x", "text/plain")})
    >>> kind.startswith("multipart/form-data; boundary="), b'filename="a.txt"' in body
    (True, True)
    """
    boundary = "correspond-" + secrets.token_hex(16)
    out = []
    for key, text in fields.items():
        out += [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            str(text).encode("utf-8"),
            b"\r\n",
        ]
    for key, (name, data, media_type) in files.items():
        escaped = name.replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")
        disposition = f'form-data; name="{key}"; filename="{escaped}"'
        out += [
            f"--{boundary}\r\n".encode(),
            f"Content-Disposition: {disposition}\r\n".encode("utf-8"),
            f"Content-Type: {media_type}\r\n\r\n".encode(),
            data,
            b"\r\n",
        ]
    out.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(out)


def retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """``Retry-After`` in seconds (it may be a number or an HTTP date), or ``None``."""
    raw = (headers or {}).get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def classify(
    status: int, message: str, headers: Mapping[str, str] | None = None
) -> ChannelError:
    """The :class:`~correspond.errors.ChannelError` an HTTP error status means."""
    retry_after = retry_after_seconds(headers)
    if status == 429:
        return ChannelError(
            message, kind="rate_limited", retryable=True, retry_after=retry_after
        )
    if status == 401:
        return ChannelError(message, kind="auth")
    if status == 403:
        return ChannelError(message, kind="permission")
    if status in (404, 410):
        return ChannelError(message, kind="not_found")
    if status in (408, 504):
        return ChannelError(
            message, kind="network", retryable=True, retry_after=retry_after
        )
    if 400 <= status < 500:
        return ChannelError(message, kind="validation")
    return ChannelError(
        message, kind="unavailable", retryable=True, retry_after=retry_after
    )
