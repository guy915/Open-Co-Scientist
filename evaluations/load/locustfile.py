# mypy: disable-error-code="import-untyped,import-not-found,untyped-decorator,misc"
from __future__ import annotations

import json
import os
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import gevent
from client import PeerAdapter
from locust import HttpUser, LoadTestShape, constant_pacing, events, task
from locust.exception import StopUser
from requests.exceptions import RequestException

PAGES = int(os.getenv("SSE_PAGES", "200"))
VISITORS = int(os.getenv("VISITORS_PER_MINUTE", "500"))
DURATION = int(os.getenv("LOAD_DURATION", "300"))
ATTEMPTS = int(os.getenv("START_ATTEMPTS", "50"))
PREFLIGHT = os.getenv("LOAD_PREFLIGHT", "1") == "1"
_counts: Counter[str] = Counter()


@events.init.add_listener
def check_local(environment: Any, **kwargs: Any) -> None:
    if environment.host != "http://api:8008":
        raise RuntimeError("This harness only permits its isolated compose API")


class LocalUser(HttpUser):
    abstract = True

    def on_start(self) -> None:
        self.client.headers["X-Client-ID"] = "load-" + str(uuid.uuid4())
        self.fixture = self.client.get("/__load__/fixture", name="fixture").json()
        peer = {"Starter": 0, "Chat": 1, "Landing": 2, "Report": 3}.get(type(self).__name__, 0)
        self.use_peer(peer)
        self.preflight_paths: set[tuple[str, str]] = set()
        if PREFLIGHT:
            self.client.headers["Origin"] = "http://localhost:5173"

    def use_peer(self, index: int) -> None:
        peers = self.fixture["peers"]
        self.client.adapters["http://"].close()
        self.client.mount("http://", PeerAdapter(peers[index % len(peers)]))

    def preflight(self, method: str, path: str, name: str) -> None:
        if not PREFLIGHT or (method, path) in self.preflight_paths:
            return
        self.request(
            "OPTIONS",
            path,
            "preflight " + name,
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "x-client-id,content-type",
            },
        )
        self.preflight_paths.add((method, path))

    def request(self, method: str, path: str, name: str, **kwargs: Any) -> Any:
        if method != "OPTIONS":
            self.preflight(method, path, name)
        with self.client.request(
            method,
            path,
            name=name,
            catch_response=True,
            allow_redirects=False,
            timeout=30,
            **kwargs,
        ) as response:
            if response.raw is not None:
                _counts["recovered_idle_retries"] += len(response.raw.retries.history)
            if method == "POST" and response.status_code in (403, 409, 429):
                try:
                    detail = response.json().get("detail")
                except ValueError:
                    detail = None
                if isinstance(detail, str) and detail:
                    _counts[f"refused:{name}:{detail}"] += 1
                    response.success()
                else:
                    response.failure("Refusal has no clear detail")
            elif response.status_code != 200:
                response.failure(f"Unexpected HTTP {response.status_code}: {response.error!r}")
            return response


class Landing(LocalUser):
    fixed_count = 10 if VISITORS else 0
    wait_time = constant_pacing(600 / max(1, VISITORS))

    @task
    def visit(self) -> None:
        self.client.headers["X-Client-ID"] = "load-landing-" + str(uuid.uuid4())
        self.preflight_paths.clear()
        for path in (
            "/status",
            "/api/free-usage",
            "/api/runs",
            "/api/runs/demo",
            "/api/interviews",
            "/api/logs?after_id=0&limit=1&min_level=WARNING",
        ):
            self.request("GET", path, "landing " + path)
        _counts["landing_visits"] += 1


class RunPage(LocalUser):
    fixed_count = PAGES
    wait_time = constant_pacing(10)

    def on_start(self) -> None:
        super().on_start()
        viewer = self.client.get("/__load__/viewer", name="viewer fixture").json()
        self.run = viewer["run"]
        self.client.headers["X-Client-ID"] = viewer["owner"]
        self.use_peer(int(viewer["owner"].rsplit("-", 1)[1]))
        self.preflight("GET", f"/api/runs/{self.run}/events", "SSE connect")
        self.stream_task = gevent.spawn(self.watch)
        self.request("GET", f"/api/runs/{self.run}/events?stream=false", "page event snapshot")

    def on_stop(self) -> None:
        self.stream_task.kill(block=True)

    def watch(self) -> None:
        failures = 0
        while True:
            retry_delay = None
            start = time.perf_counter()
            with self.client.get(
                f"/api/runs/{self.run}/events",
                name="SSE connect",
                stream=True,
                catch_response=True,
                timeout=(30, 40),
                allow_redirects=False,
            ) as response:
                if response.raw is not None:
                    _counts["recovered_idle_retries"] += len(response.raw.retries.history)
                if response.status_code == 200:
                    _counts["sse_opened"] += 1
                    # Locust's HTTP measurement is header establishment, not
                    # the lifetime of the long-lived stream.
                    response.request_meta["response_time"] = (time.perf_counter() - start) * 1000
                    try:
                        for line in response.iter_lines():
                            if line.startswith(b"data:"):
                                _counts["sse_frames"] += 1
                    except RequestException as error:
                        response.failure(f"SSE body failed: {type(error).__name__}")
                    else:
                        failures = 0
                elif response.status_code == 429 and response.json().get("detail"):
                    _counts["sse_refused"] += 1
                    retry_after = response.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        retry_delay = max(30, min(300, int(retry_after)))
                    response.success()
                else:
                    response.failure(
                        f"Unexpected SSE HTTP {response.status_code}: {response.error!r}"
                    )
            failures += 1
            gevent.sleep(retry_delay or min(30, 2**failures))

    @task
    def poll(self) -> None:
        self.request("GET", "/api/runs", "page run history")
        self.request("GET", f"/api/runs/{self.run}", "page run details")


class Report(LocalUser):
    fixed_count = 2
    wait_time = constant_pacing(6)

    @task
    def read(self) -> None:
        run = self.fixture["demo"]
        for suffix in (
            "",
            "/hypotheses",
            "/evidence",
            "/matches",
            "/reviews",
            "/claim-evidence",
            "/safety",
            "/report",
            "/report.md",
        ):
            self.request("GET", f"/api/runs/{run}{suffix}", "report " + (suffix or "/run"))
        _counts["report_visits"] += 1


class Chat(LocalUser):
    fixed_count = 2
    wait_time = constant_pacing(12)

    @task
    def ask(self) -> None:
        self.client.headers["X-Client-ID"] = "load-report"
        response = self.request(
            "POST",
            f"/api/runs/{self.fixture['report']}/messages/ask",
            "chat ask",
            json={"question": "What are the main uncertainties?"},
        )
        if response.status_code == 200 and (
            '"type": "done"' not in response.text or '"type": "error"' in response.text
        ):
            self.environment.events.request.fire(
                request_type="CHECK",
                name="chat SSE answer",
                response_time=0,
                response_length=0,
                exception=RuntimeError("No SSE response"),
            )
        _counts["chat_turns"] += 1


class Starter(LocalUser):
    fixed_count = 1 if ATTEMPTS else 0
    wait_time = constant_pacing(DURATION / max(1, ATTEMPTS))

    def on_start(self) -> None:
        super().on_start()
        self.attempts = 0

    @task
    def attempt_start(self) -> None:
        if self.attempts >= ATTEMPTS:
            raise StopUser()
        # 50 identities; the real global/host free and storage ledgers stay on.
        self.client.headers["X-Client-ID"] = "load-starter-" + str(uuid.uuid4())
        self.preflight_paths.clear()
        self.attempts += 1
        response = self.request(
            "POST",
            "/api/runs",
            "run create",
            json={"research_goal": "Investigate low-energy water purification", "tier": "express"},
        )
        _counts["start_attempts"] += 1
        if response.status_code == 200:
            started = self.request(
                "POST", f"/api/runs/{response.json()['id']}/start", "run start", json={}
            )
            if started.status_code == 200:
                _counts["runs_admitted"] += 1


class LaunchShape(LoadTestShape):
    def tick(self) -> tuple[int, float, list[type[HttpUser]]] | None:
        if self.get_run_time() >= DURATION:
            return None
        classes: list[type[HttpUser]] = [Report, Chat]
        if PAGES:
            classes.append(RunPage)
        if VISITORS:
            classes.append(Landing)
        if ATTEMPTS:
            classes.append(Starter)
        return (PAGES + (10 if VISITORS else 0) + 4 + (1 if ATTEMPTS else 0), 100, classes)


@events.report_to_master.add_listener
def report_counts(client_id: str, data: dict[str, Any], **kwargs: Any) -> None:
    data["load_counts"] = dict(_counts)
    _counts.clear()


@events.worker_report.add_listener
def receive_counts(client_id: str, data: dict[str, Any], **kwargs: Any) -> None:
    _counts.update(data.get("load_counts", {}))


@events.quitting.add_listener
def save_counts(environment: Any, **kwargs: Any) -> None:
    if Path("/results").is_dir():
        Path("/results/counts.json").write_text(json.dumps(_counts, indent=2) + "\n")
