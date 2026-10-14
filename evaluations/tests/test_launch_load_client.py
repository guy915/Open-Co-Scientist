from __future__ import annotations

import contextlib
import socketserver
import threading
import unittest
from collections.abc import Iterator
from typing import cast

import requests

from evaluations.load.client import PeerAdapter


class DropIdleServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        self.attempts: list[tuple[str, str, int]] = []
        super().__init__(("127.0.0.1", 0), DropIdleHandler)


class DropIdleHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(5)
        number = 0
        while True:
            try:
                line = self.rfile.readline()
            except TimeoutError:
                return
            if not line:
                return
            number += 1
            method, path, _ = line.decode().split()
            length = 0
            while (header := self.rfile.readline()) not in (b"\r\n", b""):
                if header.lower().startswith(b"content-length:"):
                    length = int(header.split(b":", 1)[1])
            if length:
                self.rfile.read(length)
            cast(DropIdleServer, self.server).attempts.append((method, path, number))
            if path == "/reuse" and number == 2:
                return
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            self.wfile.flush()


@contextlib.contextmanager
def stack() -> Iterator[tuple[DropIdleServer, requests.Session, str]]:
    with DropIdleServer() as server, requests.Session() as session:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        session.trust_env = False
        session.mount("http://", PeerAdapter("127.0.0.1"))
        try:
            yield server, session, f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            thread.join()


class IdleConnectionTest(unittest.TestCase):
    def test_safe_get_recovers_once_from_a_reused_socket_closing_before_headers(self) -> None:
        with stack() as (server, session, base):
            session.get(base + "/warm", timeout=2).raise_for_status()
            response = session.get(base + "/reuse", timeout=2)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.raw.retries.history), 1)
            self.assertEqual(
                server.attempts,
                [("GET", "/warm", 1), ("GET", "/reuse", 2), ("GET", "/reuse", 1)],
            )

    def test_post_is_never_replayed_after_the_server_receives_its_body(self) -> None:
        with stack() as (server, session, base):
            session.get(base + "/warm", timeout=2).raise_for_status()
            with self.assertRaises(requests.ConnectionError):
                session.post(base + "/reuse", json={"run": "synthetic"}, timeout=2)
            self.assertEqual(server.attempts, [("GET", "/warm", 1), ("POST", "/reuse", 2)])


if __name__ == "__main__":
    unittest.main()
