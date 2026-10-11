"""Frozen HTTP responses replay without a network fallback or recorded credentials."""

import base64
import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


def request_key(request: httpx.Request) -> str:
    url = urlsplit(str(request.url))
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(url.query)
            if key.lower() not in {"key", "apikey", "api_key", "token", "access_token"}
        ]
    )
    sanitized = urlunsplit((url.scheme, url.netloc, url.path, query, ""))
    # Request prompts and headers are never stored; only the digest is needed to match.
    return hashlib.sha256(request.method.encode() + sanitized.encode() + request.read()).hexdigest()


class RecordingStream(httpx.SyncByteStream):
    def __init__(self, stream, finish):
        self.stream, self.finish, self.chunks = stream, finish, []

    def __iter__(self):
        for chunk in self.stream:
            self.chunks.append(base64.b64encode(chunk).decode())
            yield chunk

    def close(self):
        self.stream.close()
        self.finish(self.chunks)


class FrozenTransport(httpx.BaseTransport):
    def __init__(self, path: Path, *, record: bool = False, transport=None):
        self.path, self.record = path, record
        self.transport = transport or (httpx.HTTPTransport(retries=0) if record else None)
        self.lock = threading.Lock()
        self.rows = [] if record else json.loads(path.read_text(encoding="utf-8"))

    def handle_request(self, request):
        key = request_key(request)
        if not self.record:
            with self.lock:
                index = next((n for n, row in enumerate(self.rows) if row["key"] == key), None)
                if index is None:
                    raise AssertionError("frozen response missing; network fallback forbidden")
                row = self.rows.pop(index)
            return httpx.Response(
                row["status"],
                content=b"".join(base64.b64decode(c) for c in row["chunks"]),
                headers=row["headers"],
                request=request,
            )
        assert self.transport is not None, "record mode requires a live transport"
        response = self.transport.handle_request(request)

        def finish(chunks):
            with self.lock:
                self.rows.append(
                    {
                        "key": key,
                        "status": response.status_code,
                        "headers": {"content-type": response.headers.get("content-type", "")},
                        "chunks": chunks,
                    }
                )
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.rows), encoding="utf-8")

        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=RecordingStream(response.stream, finish),
            request=request,
        )

    def close(self):
        if self.transport is not None:
            self.transport.close()
