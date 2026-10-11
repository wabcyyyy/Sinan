"""Absolute deadlines apply to chunks, including streams without complete lines."""

import codecs
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

import httpx


class _Limits(Protocol):
    """HTTP 截止期所需的最小运行边界接口（结构化协议，由调用方注入具体实现）。"""

    def check(self, kind: str) -> None: ...

    def remaining(self) -> float: ...


@contextmanager
def response_deadline(response: httpx.Response, limits: _Limits | None):
    timer = None
    if limits is not None:
        limits.check("http")
        timer = threading.Timer(max(0.001, limits.remaining()), response.close)
        timer.daemon = True
        timer.start()
    try:
        yield
    finally:
        if timer is not None:
            timer.cancel()


def bounded_chunks(response: httpx.Response, limits: _Limits | None) -> Iterator[bytes]:
    with response_deadline(response, limits):
        for chunk in response.iter_bytes():
            if limits is not None:
                limits.check("http")
            yield chunk
        if limits is not None:
            limits.check("http")


def bounded_lines(response: httpx.Response, limits: _Limits | None) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    for chunk in bounded_chunks(response, limits):
        pending += decoder.decode(chunk)
        if len(pending) > 2 * 1024 * 1024:
            raise ValueError("HTTP stream line exceeded byte limit")
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            yield line.rstrip("\r")
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending.rstrip("\r")
