"""Count the physical HTTP attempts of one default Express run, offline.

The run goes through the real API, durable worker, LLM gateway, MCP client,
MCP server and source tools. Only the bottom of each transport is fake: the
httpx transports (and LiteLLM's aiohttp transport) in both processes, and
the urllib opener Biopython's Entrez uses in the MCP server. Every attempt
those see is counted by category, including retries and failures, so the
total is what a live run would send on its first-success path.

    python -m evaluations.attempt_envelope            # both arms, table
    python -m evaluations.attempt_envelope --arm anthropic --result out.json

`--engine-dir` measures another checkout's engine (for a before/after pair);
the MCP server needs the interpreter from `make test-mcp` (`.venv-mcp`).
No provider key is read or needed; any credential in the environment is
removed before the engine is imported.
"""

from __future__ import annotations

import argparse
import contextvars
import importlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent

GOAL = (
    "Identify mechanisms by which macromolecular crowding changes the folding pathway "
    "of intrinsically disordered proteins in bacterial cytoplasm."
)
# The rest of a live check sequence: a paired cache wave of eight calls and up to
# ten judged comparisons with one count_tokens each (20 HTTP attempts).
CACHE_WAVE_ATTEMPTS = 8
JUDGING_ATTEMPTS = 20
OPERATIONAL_LIMIT = 270

_FAKE_KEY = "offline-envelope-not-a-key"
_AZURE_ENDPOINT = "https://offline-envelope.openai.azure.com"
_ARM_ENV = {
    "anthropic": {
        "ANTHROPIC_API_KEY": _FAKE_KEY,
        "ANTHROPIC_MONTHLY_CREDIT_USD": "1000",
    },
    "azure": {
        "AZURE_OPENAI_API_KEY": _FAKE_KEY,
        "AZURE_OPENAI_ENDPOINT": _AZURE_ENDPOINT,
        "AZURE_OPENAI_DEPLOYMENT": "offline-luna",
        "LLM_AZURE_ENABLED": "true",
        "LLM_TOTAL_BUDGET_EUR": "1000",
        "LLM_AZURE_EXPIRES_AT": "2099-01-04T00:00:00+00:00",
        "LLM_AZURE_CUTOFF_HOURS": "48",
        "LLM_USD_TO_EUR": "0.88",
    },
}
_MCP_ENV = {
    "BRAVE_API_KEY": _FAKE_KEY,
    "TAVILY_API_KEY": _FAKE_KEY,
    "ENTREZ_EMAIL": "offline-envelope@example.invalid",
}


def _scrub_environment() -> None:
    sensitive = ("_API_KEY", "_TOKEN", "_SECRET", "_SECRET_KEY", "_KEY_ID", "_DSN")
    for name in list(os.environ):
        if name.endswith(sensitive) or name.startswith(("AZURE_", "LLM_", "LITESTREAM_")):
            del os.environ[name]
    for name in ("COSCIENTIST_TEST_DOUBLE", "MODEL_NAME"):
        os.environ.pop(name, None)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_mcp(
    mcp_python: str, engine_dir: Path, workdir: Path, secret: str
) -> tuple[subprocess.Popen[bytes], int, Path]:
    port = _free_port()
    counts = workdir / "mcp-attempts.json"
    env = {
        **os.environ,
        **_MCP_ENV,
        "COSCIENTIST_MCP_SHARED_SECRET": secret,
        "COSCIENTIST_LIT_REVIEW_DIR": str(workdir / "literature"),
        "PYTHON_DOTENV_DISABLED": "1",
    }
    process = subprocess.Popen(
        [
            mcp_python,
            str(_ROOT / "evaluations" / "_attempt_mcp.py"),
            f"--engine-dir={engine_dir}",
            f"--port={port}",
            f"--counts={counts}",
        ],
        cwd=workdir,
        env=env,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("the MCP server exited during startup")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2):
                return process, port, counts
        except OSError:
            time.sleep(0.2)
    process.terminate()
    raise SystemExit("the MCP server did not start within 60 s")


_LOGICAL: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "attempt_envelope_request", default=None
)


class _CapturingBackend:
    """Remembers the logical request so a fake provider can answer it. The
    physical request still goes through the wrapped backend unchanged."""

    operator_routing = True

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def complete(self, **completion_args: Any) -> Any:
        if _LOGICAL.get() is not None:
            return await self._inner.complete(**completion_args)
        token = _LOGICAL.set(dict(completion_args))
        try:
            return await self._inner.complete(**completion_args)
        finally:
            _LOGICAL.reset(token)

    def supports_json_schema(self, model_name: str) -> bool:
        return bool(self._inner.supports_json_schema(model_name))


# Prompts carrying a schema the model cannot enforce, by their final text.
_INJECTED: dict[str, dict[str, Any]] = {}


def _capture_schemas() -> None:
    from co_scientist.platform.llm.request import completion

    inject = completion._inject_schema_into_prompt

    def remember(prompt: str, json_schema: dict[str, Any]) -> str:
        shimmed = inject(prompt, json_schema)
        # Callers pass either a named response format or a bare schema.
        named = json_schema if "schema" in json_schema else {"name": "", "schema": json_schema}
        _INJECTED[shimmed] = named
        return shimmed

    completion._inject_schema_into_prompt = remember


def _capture_before_routing() -> None:
    """Routing moves a schema the model cannot enforce into the prompt; the
    request before routing still names its schema."""
    from co_scientist.platform.llm.request import transport

    routed = vars(transport)["routed_completion"]

    async def capture(request: dict[str, Any], original: str, **kwargs: Any) -> Any:
        token = _LOGICAL.set(dict(request))
        try:
            return await routed(request, original, **kwargs)
        finally:
            _LOGICAL.reset(token)

    vars(transport)["routed_completion"] = capture


def _answer_text(logical: dict[str, Any]) -> str:
    """Schema-valid content from the offline filler, made realistic the way the
    offline call meter does (`app/tests/_envelope_fakes.py`)."""
    from co_scientist.platform.llm.offline import llm as offline

    # The offline call meter's realism rules, shared rather than copied.
    fakes = importlib.import_module("tests._envelope_fakes")

    model = str(logical.get("model") or "")
    prompt = offline._prompt_text(logical)
    response_format = logical.get("response_format") or {}
    json_schema = (
        response_format["json_schema"]
        if response_format.get("type") == "json_schema"
        else _INJECTED.get(prompt)
    )
    schema_name = ""
    if json_schema is not None:
        schema_name = str(json_schema.get("name") or "")
        content = offline._schema_response(model, prompt, json_schema).choices[0].message.content
    elif response_format.get("type") == "json_object":
        content = "{}"
    else:
        import random

        rng = random.Random(offline._seed_for(model, prompt, ""))
        content = offline.leaf_text(rng, 1, "", offline.subject_terms(prompt))
    answer = fakes.realistic_answer(prompt, str(content), schema_name)
    if '"supporting": {}' in answer or '"contradicting": {}' in answer:
        answer = json.dumps(fakes._cite_as_lists(json.loads(answer)))
    if json_schema is not None and "offensive_score" in str(json_schema.get("schema")):
        # A real screen allows this benign goal; filler scores would hold it.
        decision = json.loads(answer)
        decision.update(category="allowed", offensive_score=0, risk_domains=[])
        decision.update(
            is_personal_medical_recommendation=False, is_personal_finance_recommendation=False
        )
        answer = json.dumps(decision)
    return str(answer)


def _anthropic(request: Any, body: dict[str, Any]) -> Any:
    import httpx

    if request.url.path.endswith("/count_tokens"):
        return httpx.Response(200, json={"input_tokens": min(len(request.content) // 4, 90_000)})
    if body.get("stream"):
        raise RuntimeError("streamed Anthropic requests are not served offline")
    logical = _LOGICAL.get()
    if logical is None:
        raise RuntimeError("an Anthropic request bypassed the LLM gateway")
    text = _answer_text(logical)
    forced = any(tool.get("name") == "json_tool_call" for tool in body.get("tools") or [])
    block: dict[str, Any] = (
        {"type": "tool_use", "id": "toolu_offline", "name": "json_tool_call"}
        | {"input": json.loads(text)}
        if forced
        else {"type": "text", "text": text}
    )
    return httpx.Response(
        200,
        json={
            "id": "msg_offline",
            "type": "message",
            "role": "assistant",
            "model": body.get("model"),
            "content": [block],
            "stop_reason": "tool_use" if forced else "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": len(request.content) // 4,
                "output_tokens": max(1, len(text) // 4),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    )


def _azure(request: Any, body: dict[str, Any]) -> Any:
    import httpx

    if body.get("stream"):
        raise RuntimeError("streamed Azure requests are not served offline")
    logical = _LOGICAL.get()
    if logical is None:
        raise RuntimeError("an Azure request bypassed the LLM gateway")
    text = _answer_text(logical)
    return httpx.Response(
        200,
        json={
            "id": "resp_offline",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": body.get("model"),
            "output": [
                {
                    "type": "message",
                    "id": "msg_offline",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ],
            "usage": {
                "input_tokens": len(request.content) // 4,
                "output_tokens": max(1, len(text) // 4),
                "total_tokens": len(request.content) // 4 + max(1, len(text) // 4),
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    )


def _engine_response(request: Any, kind: str) -> Any:
    from evaluations import _attempt_wire as wire

    if kind in wire.LLM_CATEGORIES:
        body = json.loads(request.content or b"{}")
        if request.url.host == "api.anthropic.com":
            return _anthropic(request, body)
        return _azure(request, body)
    return wire.source_response(request, kind)


def _install_engine_transports(counter: Any) -> None:
    from co_scientist.platform.llm.request import backend
    from litellm.llms.custom_httpx.aiohttp_transport import LiteLLMAiohttpTransport

    from evaluations import _attempt_wire as wire

    wire.pin_dns()
    # The engine's pinned requests are citation probes; read_url is the MCP's.
    wire.install_httpx(counter, _engine_response, pinned_kind="citation_probe")
    # LiteLLM sends through aiohttp by default; answer it at the same depth.
    handler = wire.httpx_transport_handler(counter, _engine_response)
    LiteLLMAiohttpTransport.handle_async_request = handler  # type: ignore[assignment,method-assign]
    backend.install_backend(_CapturingBackend(backend.active_backend()))
    _capture_before_routing()
    _capture_schemas()


def _run_arm(arm: str, engine_dir: Path, mcp_python: str, workdir: Path) -> dict[str, Any]:
    _scrub_environment()
    secret = secrets.token_hex(16)
    os.environ.update(
        {
            **_ARM_ENV[arm],
            "LLM_ENABLED": "true",
            "COSCIENTIST_DB_PATH": str(workdir / "coscientist.db"),
            "COSCIENTIST_MCP_SHARED_SECRET": secret,
            "PYTHON_DOTENV_DISABLED": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "True",
        }
    )
    sys.path[:0] = [str(engine_dir / "src"), str(_ROOT), str(_ROOT / "app")]
    os.chdir(workdir)
    mcp, port, mcp_counts = _start_mcp(mcp_python, engine_dir, workdir, secret)
    os.environ["MCP_SERVER_URL"] = f"http://127.0.0.1:{port}/mcp"
    try:
        return _drive(arm, engine_dir, workdir, mcp_counts)
    finally:
        mcp.terminate()
        mcp.wait(timeout=30)


def _drive(arm: str, engine_dir: Path, workdir: Path, mcp_counts: Path) -> dict[str, Any]:
    import co_scientist

    served = Path(co_scientist.__file__).resolve().parent
    if served != (engine_dir / "src" / "co_scientist").resolve():
        raise SystemExit(f"measuring the wrong engine: {served}")
    from evaluations import _attempt_wire as wire

    counter = wire.AttemptCounter()
    _install_engine_transports(counter)
    if arm == "azure":
        from datetime import datetime
        from decimal import Decimal

        from co_scientist.platform.llm.admission.spend import establish_allowance

        establish_allowance(
            os.environ["COSCIENTIST_DB_PATH"],
            grant_eur=Decimal(1001),
            prior_usage_eur=Decimal(0),
            buffer_eur=Decimal(1),
            expires_at=datetime.fromisoformat(os.environ["LLM_AZURE_EXPIRES_AT"]),
            cutoff_hours=Decimal(48),
            usd_to_eur=Decimal("0.88"),
        )

    from co_scientist.domains.report import repository as reports
    from co_scientist.main import app
    from co_scientist.platform.db import runs
    from fastapi.testclient import TestClient

    client = TestClient(app, headers={"X-Client-ID": f"attempt-envelope-{arm}"})
    created = client.post("/api/runs", json={"research_goal": GOAL})
    if created.status_code != 200:
        raise SystemExit(f"run creation failed: HTTP {created.status_code} {created.text}")
    run_id = created.json()["id"]
    started = time.monotonic()
    # The embedded worker runs as this request's background task, so the call
    # returns when the run stops.
    launched = client.post(f"/api/runs/{run_id}/start", json={})
    if launched.status_code != 200:
        raise SystemExit(f"run start failed: HTTP {launched.status_code} {launched.text}")
    run = runs.get_run(run_id)
    report = reports.get_latest_report(run_id)
    mcp = json.loads(mcp_counts.read_text()) if mcp_counts.exists() else {"counts": {}, "hosts": {}}
    counts = {
        name: counter.counts.get(name, 0) + mcp["counts"].get(name, 0) for name in wire.CATEGORIES
    }
    return {
        "arm": arm,
        "engine": str(engine_dir),
        "run_status": run.status if run else None,
        "final_report": report is not None,
        "tier": run.profile if run else None,
        "seconds": round(time.monotonic() - started, 1),
        "attempts": counts,
        "llm_total": sum(counts[name] for name in wire.LLM_CATEGORIES),
        "source_total": sum(v for k, v in counts.items() if k not in wire.LLM_CATEGORIES),
        "total": sum(counts.values()),
        "hosts": {**counter.hosts, **{f"mcp:{k}": v for k, v in mcp["hosts"].items()}},
    }


def _sequence(arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    llm = sum(arm["llm_total"] for arm in arms.values()) + CACHE_WAVE_ATTEMPTS
    llm += JUDGING_ATTEMPTS
    source = sum(arm["source_total"] for arm in arms.values())
    total = llm + source
    return {
        "llm_total": llm,
        "source_total": source,
        "total": total,
        "operational_limit": OPERATIONAL_LIMIT,
        "within_limit": total <= OPERATIONAL_LIMIT,
        "both_final_reports": all(arm["final_report"] for arm in arms.values()),
    }


def _table(arms: dict[str, dict[str, Any]], sequence: dict[str, Any]) -> str:
    from evaluations._attempt_wire import CATEGORIES

    names = list(arms)
    lines = ["| Category | " + " | ".join(names) + " |", "|---" * (len(names) + 1) + "|"]
    for name in (*CATEGORIES, "llm_total", "source_total", "total"):
        values = [str(arms[arm]["attempts"].get(name, arms[arm].get(name, 0))) for arm in names]
        lines.append(f"| {name} | " + " | ".join(values) + " |")
    lines.append(
        "| final report | " + " | ".join(str(arms[a]["final_report"]) for a in names) + " |"
    )
    lines.append(
        f"\nLive check sequence: {sequence['total']} attempts (LLM {sequence['llm_total']}, "
        f"sources {sequence['source_total']}, including cache wave {CACHE_WAVE_ATTEMPTS} "
        f"and judging {JUDGING_ATTEMPTS}); limit {OPERATIONAL_LIMIT}: "
        f"{'within' if sequence['within_limit'] else 'ABOVE'}"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--arm", choices=("anthropic", "azure", "both"), default="both")
    parser.add_argument("--engine-dir", type=Path, default=_ROOT / "engine")
    parser.add_argument("--mcp-python", default=str(_ROOT / ".venv-mcp" / "bin" / "python"))
    parser.add_argument("--result", type=Path, help="write one arm's result here as JSON")
    args = parser.parse_args()
    engine_dir = args.engine_dir.resolve()
    if args.arm != "both":
        with tempfile.TemporaryDirectory(prefix="attempt-envelope-") as workdir:
            result = _run_arm(args.arm, engine_dir, args.mcp_python, Path(workdir))
        if args.result is not None:
            args.result.write_text(json.dumps(result))
        print(json.dumps(result, indent=1))
        if not result["final_report"]:
            raise SystemExit(1)
        return
    arms: dict[str, dict[str, Any]] = {}
    results = Path(tempfile.mkdtemp(prefix="attempt-envelope-results-"))
    for arm in ("anthropic", "azure"):
        # One process per arm: each configures its environment before import.
        output = subprocess.run(
            [
                sys.executable,
                "-m",
                "evaluations.attempt_envelope",
                f"--arm={arm}",
                f"--engine-dir={engine_dir}",
                f"--mcp-python={args.mcp_python}",
                f"--result={results / arm}.json",
            ],
            cwd=_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if output.returncode != 0:
            sys.stderr.write((output.stdout + output.stderr)[-4000:])
            raise SystemExit(f"{arm} arm failed with exit {output.returncode}")
        arms[arm] = json.loads((results / f"{arm}.json").read_text())
    sequence = _sequence(arms)
    print(_table(arms, sequence))
    print(json.dumps({"arms": arms, "w4_sequence": sequence}))
    if not sequence["both_final_reports"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
