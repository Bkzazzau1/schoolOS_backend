"""How a connector talks to a provider over HTTPS.

One small seam so adapters are written once and tested without a network: a test installs a fake transport (see
`use_transport`) that answers exactly as the provider's documentation says the provider does. The real transport uses only the
standard library, sends only what the adapter gives it, and never logs a header, a body or a URL with a secret in it.

Timeouts and network failures raise `ProviderUnavailable`: the outcome of such a call is unknown, and the caller must treat it
that way.
"""

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from django.conf import settings

from .base import ProviderUnavailable

DEFAULT_TIMEOUT = 20
#: A provider's reply is never larger than this: a runaway body must not fill memory.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass
class HttpResult:
    status: int
    #: The JSON body, or None when it was empty or not JSON.
    data: Any = None
    #: The body as text, for a reply that is not JSON.
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport:
    def request(self, method: str, url: str, *, headers: dict | None = None, json_body: Any = None, params: dict | None = None,
                timeout: int = DEFAULT_TIMEOUT) -> HttpResult:
        raise NotImplementedError


class UrllibTransport(Transport):
    def request(self, method, url, *, headers=None, json_body=None, params=None, timeout=DEFAULT_TIMEOUT) -> HttpResult:
        if params:
            url = f"{url}?{urlparse.urlencode(params)}"
        data = None
        merged = {"Accept": "application/json", **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            merged.setdefault("Content-Type", "application/json")
        if not url.lower().startswith("https://") and not _allow_http():
            raise ProviderUnavailable("A provider is only ever called over HTTPS.")
        req = urlrequest.Request(url, data=data, headers=merged, method=method.upper())
        try:
            with urlrequest.urlopen(req, timeout=timeout) as response:  # noqa: S310 - the host is a provider's fixed address
                return _result(response.status, response.read(MAX_RESPONSE_BYTES))
        except urlerror.HTTPError as http_error:
            return _result(http_error.code, http_error.read(MAX_RESPONSE_BYTES))
        except (urlerror.URLError, TimeoutError, OSError):
            raise ProviderUnavailable() from None


def _allow_http() -> bool:
    return bool(getattr(settings, "COLLECTION_ALLOW_HTTP", False))


def _result(status: int, raw: bytes) -> HttpResult:
    text = raw.decode("utf-8", errors="replace") if raw else ""
    try:
        return HttpResult(status, json.loads(text) if text else None, text)
    except ValueError:
        return HttpResult(status, None, text)


_override: Transport | None = None


def get_transport() -> Transport:
    return _override or UrllibTransport()


@contextmanager
def use_transport(transport: Transport):
    """Install a transport for the duration of a block (tests, and any dry-run tool)."""
    global _override
    previous, _override = _override, transport
    try:
        yield transport
    finally:
        _override = previous
