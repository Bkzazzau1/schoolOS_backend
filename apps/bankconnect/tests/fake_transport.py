"""A provider's server for the tests: it answers exactly as the provider's own documentation says the provider does, and remembers every
call so a test can check what SchoolOS actually sent (the path, the headers, the body) and that no secret went anywhere it should not."""

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlparse

from ..providers.transport import HttpResult, Transport


@dataclass
class Call:
    method: str
    url: str
    headers: dict
    json: object
    params: dict

    @property
    def path(self) -> str:
        return urlparse(self.url).path

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc

    @property
    def query(self) -> dict:
        return {**dict(parse_qsl(urlparse(self.url).query)), **(self.params or {})}


@dataclass
class FakeTransport(Transport):
    """`on(method, path, answer)`: an answer is an `HttpResult`, a callable taking the `Call`, an exception to raise, or a list of these
    used one after the other (the last one repeats)."""

    routes: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def on(self, method: str, path: str, answer) -> "FakeTransport":
        self.routes[(method.upper(), path)] = answer
        return self

    def request(self, method, url, *, headers=None, json_body=None, params=None, timeout=20):
        call = Call(method.upper(), url, dict(headers or {}), json_body, dict(params or {}))
        self.calls.append(call)
        answer = self.routes.get((call.method, call.path))
        if answer is None:
            raise AssertionError(f"The provider was called somewhere unscripted: {call.method} {call.path}")
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer(call) if callable(answer) else answer

    def to(self, method: str, path: str) -> list:
        return [c for c in self.calls if c.method == method.upper() and c.path == path]

    def called(self, method: str, path: str) -> int:
        return len(self.to(method, path))


def ok(data, status: int = 200, text: str = "") -> HttpResult:
    return HttpResult(status, data, text)
