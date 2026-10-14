# mypy: disable-error-code="import-untyped,import-not-found,untyped-decorator,misc"
from __future__ import annotations

from typing import Any

import gevent
from gevent.event import Event
from gevent.server import StreamServer
from locust.env import Environment
from locust.event import Events
from locustfile import RunPage


def broken_stream(sock: Any, address: tuple[str, int]) -> None:
    request = b""
    while b"\r\n\r\n" not in request:
        chunk = sock.recv(4096)
        if not chunk:
            return
        request += chunk
    sock.sendall(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
        b"Content-Type: text/event-stream\r\n\r\n5\r\nx"
    )
    sock.close()


def main() -> None:
    server = StreamServer(("127.0.0.1", 0), broken_stream)
    server.start()
    seen: list[dict[str, Any]] = []
    finished = Event()
    environment = Environment(host=f"http://127.0.0.1:{server.server_port}", events=Events())

    def record(**kwargs: Any) -> None:
        seen.append(kwargs)
        finished.set()

    environment.events.request.add_listener(record)
    RunPage.host = environment.host
    user = RunPage(environment)
    user.client.trust_env = False
    user.run = "synthetic"
    watcher = gevent.spawn(user.watch)
    try:
        finished.wait(timeout=1)
        assert len(seen) == 1, f"Body failure was not counted: {len(seen)}"
        assert "SSE body failed" in str(seen[0]["exception"])
        assert not watcher.ready(), "Watcher died instead of entering reconnect backoff"
        print("Truncated SSE counted once; watcher remains alive for bounded reconnect")
    finally:
        watcher.kill(block=True)
        user.client.close()
        server.stop()


if __name__ == "__main__":
    main()
