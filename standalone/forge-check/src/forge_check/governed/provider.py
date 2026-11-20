"""Reference transport for the explicit model and ticket bindings."""

from __future__ import annotations

import json
from contextlib import contextmanager
from time import monotonic
from typing import TYPE_CHECKING, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if TYPE_CHECKING:
    from collections.abc import Iterator
    from http.client import HTTPResponse

_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_FRAME_BYTES = 64 * 1024


class TransportError(RuntimeError):
    """The transport could not establish a valid provider observation."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, hdrs: object, newurl: str
    ) -> None:
        return None


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise TransportError("Duplicate provider response field")
        document[key] = value
    return document


def _bad_constant(_value: str) -> None:
    raise TransportError("Invalid provider response number")


def _document(raw: bytes) -> dict[str, object]:
    try:
        parsed: object = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_bad_constant
        )
    except (ValueError, RecursionError) as exc:
        raise TransportError("Invalid provider response JSON") from exc
    if not isinstance(parsed, dict):
        raise TransportError("Expected a provider response object")
    return cast("dict[str, object]", parsed)


class HttpProvider:
    """One operator-configured origin with direct requests and bounded responses."""

    def __init__(self, origin: str, *, token: str | None = None, timeout: float = 5.0) -> None:
        endpoint = urlsplit(origin)
        if (
            not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
            or (
                endpoint.scheme != "https"
                and (
                    endpoint.scheme != "http"
                    or endpoint.hostname not in {"127.0.0.1", "::1", "localhost"}
                )
            )
        ):
            raise ValueError("Configure an HTTPS origin or an HTTP loopback origin")
        if endpoint.port is not None and endpoint.port <= 0:
            raise ValueError("Invalid provider port")
        if token is not None and (
            not token.isascii() or any(character.isspace() for character in token)
        ):
            raise ValueError("Invalid provider credential format")
        if not 0 < timeout <= 60:
            raise ValueError("Provider timeout must be between zero and 60 seconds")
        self._origin = origin.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @contextmanager
    def _open(
        self,
        path: str,
        accept: str,
        body: dict[str, object] | None = None,
        *,
        attempt_id: str | None = None,
    ) -> Iterator[HTTPResponse]:
        headers = {"Accept": accept}
        if attempt_id is not None:
            headers["X-Forge-Attempt"] = attempt_id
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if body is not None:
            try:
                data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, RecursionError) as exc:
                raise TransportError("Could not serialize provider request") from exc
            if len(data) > _MAX_DOCUMENT_BYTES:
                raise TransportError("Provider request exceeds the configured limit")
            headers["Content-Type"] = "application/json"
        request = Request(self._origin + path, data=data, headers=headers)
        try:
            response = self._opener.open(request, timeout=self._timeout)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise TransportError(f"Unexpected provider HTTP status {code}") from None
        except (OSError, ValueError) as exc:
            raise TransportError("Provider request ended without a confirmed response") from exc
        with response:
            if response.status != 200 or response.headers.get_content_type() != accept:
                raise TransportError("Unexpected provider response format")
            yield cast("HTTPResponse", response)

    def _json(
        self,
        path: str,
        body: dict[str, object] | None = None,
        *,
        attempt_id: str | None = None,
    ) -> dict[str, object]:
        with self._open(path, "application/json", body, attempt_id=attempt_id) as response:
            try:
                raw = response.read(_MAX_DOCUMENT_BYTES + 1)
            except (OSError, ValueError) as exc:
                raise TransportError("Provider response was interrupted") from exc
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise TransportError("Provider response exceeds the configured limit")
        return _document(raw)

    def dispatch(self, body: dict[str, object]) -> dict[str, object]:
        return self._json("/dispatch", body)

    @contextmanager
    def streaming(self, body: dict[str, object]) -> Iterator[Iterator[dict[str, object]]]:
        with self._open("/dispatch", "application/x-ndjson", body) as response:
            yield self._frames(response)

    def _frames(self, response: HTTPResponse) -> Iterator[dict[str, object]]:
        bytes_seen = 0
        deadline = monotonic() + self._timeout
        while monotonic() < deadline:
            try:
                frame = response.readline(_MAX_FRAME_BYTES + 1)
            except (OSError, ValueError) as exc:
                raise TransportError("Provider stream was interrupted") from exc
            if not frame:
                return
            bytes_seen += len(frame)
            if (
                len(frame) > _MAX_FRAME_BYTES
                or bytes_seen > _MAX_DOCUMENT_BYTES
                or not frame.endswith(b"\n")
            ):
                raise TransportError("Incomplete or oversized provider stream frame")
            yield _document(frame)
        raise TransportError("Provider stream exceeded its deadline")

    def lookup(self, operation_key: str, attempt_id: str) -> dict[str, object]:
        if len(operation_key) != 64 or any(
            character not in "0123456789abcdef" for character in operation_key
        ):
            raise ValueError("Invalid provider operation key")
        return self._json(f"/operations/{operation_key}", attempt_id=attempt_id)
