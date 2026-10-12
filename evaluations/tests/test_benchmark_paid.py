from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from evaluations import benchmark_paid, quality_benchmark


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access in paid benchmark tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def test_paid_counter_fingerprints_the_native_azure_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    original = vars(benchmark_paid.trusted_transport())["COUNTER_SHA256"]
    monkeypatch.setattr(benchmark_paid, "version", lambda name: "synthetic-other-sdk-version")
    changed = vars(benchmark_paid.trusted_transport())["COUNTER_SHA256"]
    assert changed != original


def test_paid_configuration_is_explicit_isolated_and_fresh(tmp_path: Path) -> None:
    script = """
import os
from evaluations.benchmark_paid import configure
configure('anthropic', 'anthropic/fixture-model')
assert os.environ['COSCIENTIST_REQUIRE_FREE_MODELS']=='0'
assert os.environ['PYTHON_DOTENV_DISABLED']=='1'
assert os.environ['LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS']=='True'
assert os.environ['ANTHROPIC_API_KEY']=='synthetic-key'
assert 'OPENROUTER_API_KEY' not in os.environ
assert 'AZURE_API_KEY' not in os.environ
assert os.environ['CLAIM_VERIFIER_MODEL']=='anthropic/fixture-model'
from co_scientist.core.config import settings
assert settings.model_name=='anthropic/fixture-model'
try:
 configure('anthropic','anthropic/fixture-model')
except ValueError:
 pass
else:
 raise AssertionError('fresh-process guard missing')
"""
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "ANTHROPIC_API_KEY": "synthetic-key",
        "OPENROUTER_API_KEY": "synthetic-router",
        "AZURE_API_KEY": "synthetic-azure",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr


def test_paid_cli_preserves_declared_identity_and_key_scope_without_network(tmp_path: Path) -> None:
    script = """
import asyncio,os,socket,sys
import httpx
from evaluations import benchmark_paid,quality_benchmark
def deny(*args,**kwargs):raise AssertionError('network access')
socket.socket.connect=deny
configured=benchmark_paid.configure
class Fake:
 def supports_json_schema(self,model):return False
 async def complete(self,**kwargs):
  transport=httpx.MockTransport(lambda _:httpx.Response(200))
  async with httpx.AsyncClient(transport=transport) as client:
   return await client.post('https://api.anthropic.com/messages')
def configure(provider,model):
 result=configured(provider,model)
 from co_scientist.platform.llm.request.backend import install_backend
 install_backend(Fake())
 return result
benchmark_paid.configure=configure
def main():
 from co_scientist.platform.llm.admission.free_policy import current_api_key
 from co_scientist.platform.llm.request.backend import active_backend
 from evaluations._identity import arm_identity,identity_digest
 from evaluations._run_driver import configure_environment
 configure_environment('/private/synthetic-only.db',live=True)
 assert current_api_key()=='synthetic-key'
 identity=arm_identity('fixture goal',{'tier':'express'},'real')
 assert identity['configured_models']['worker']=='anthropic/fixture-model'
 environment=identity['execution_environment']
 assert environment['COSCIENTIST_BENCHMARK_PROVIDER']=='anthropic'
 assert environment['COSCIENTIST_BENCHMARK_POLICY_SHA256']==benchmark_paid.POLICY_SHA256
 assert 'synthetic-key' not in str(identity)
 assert identity['digest']==identity_digest({k:v for k,v in identity.items() if k!='digest'})
 bounded=quality_benchmark.BoundedBackend(active_backend(),1)
 asyncio.run(bounded.complete(model='anthropic/fixture-model'))
 assert bounded.calls==1
 return 0
quality_benchmark.main=main
sys.argv=['paid','collect','--provider','anthropic','--model','anthropic/fixture-model','--live']
assert benchmark_paid.main()==0
"""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(root),
            "ANTHROPIC_API_KEY": "synthetic-key",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "provider,model,env",
    [
        ("anthropic", "anthropic/model", {}),
        ("anthropic", "openrouter/model", {"ANTHROPIC_API_KEY": "synthetic"}),
        (
            "anthropic",
            "anthropic/model",
            {"ANTHROPIC_API_KEY": "synthetic", "COSCIENTIST_TEST_DOUBLE": "deterministic"},
        ),
        (
            "azure",
            "azure/deployment",
            {
                "AZURE_API_KEY": "synthetic",
                "AZURE_API_BASE": "http://localhost:8008",
                "AZURE_API_VERSION": "2025-01-01",
            },
        ),
        (
            "azure",
            "azure/deployment",
            {
                "AZURE_API_KEY": "synthetic",
                "AZURE_API_BASE": "https://resource.openai.azure.com/?key=synthetic",
                "AZURE_API_VERSION": "2025-01-01",
            },
        ),
        (
            "azure",
            "azure/deployment",
            {
                "AZURE_API_KEY": "synthetic",
                "AZURE_API_BASE": "https://resource.cognitiveservices.azure.com/openai/deployments/x",
                "AZURE_API_VERSION": "2025-01-01",
            },
        ),
        (
            "azure",
            "azure/deployment",
            {
                "AZURE_API_KEY": "synthetic",
                "AZURE_API_BASE": "https://resource.azure-api.net",
                "AZURE_API_VERSION": "2025-01-01",
            },
        ),
    ],
)
def test_invalid_paid_configuration_refuses_before_any_engine_or_provider_call(
    tmp_path: Path,
    provider: str,
    model: str,
    env: dict[str, str],
) -> None:
    script = f"""
import socket,sys
def deny(*args,**kwargs):raise AssertionError('provider request attempted')
socket.socket.connect=deny
from evaluations.benchmark_paid import configure
try:
 configure({provider!r},{model!r})
except ValueError:
 assert 'co_scientist.core.config' not in sys.modules
else:
 raise AssertionError('invalid configuration admitted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr


def test_azure_is_refused_even_with_complete_configuration(tmp_path: Path) -> None:
    script = """
import sys
from evaluations.benchmark_paid import configure
try:
 configure('azure','azure/deployment')
except ValueError as error:
 assert 'Azure' in str(error)
 assert 'co_scientist.core.config' not in sys.modules
else:
 raise AssertionError('Azure benchmark admitted outside the production ledger')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "AZURE_OPENAI_API_KEY": "synthetic",
            "AZURE_API_BASE": "https://resource.openai.azure.com",
            "AZURE_API_VERSION": "2025-01-01-preview",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr


class FakeBackend:
    def __init__(self, client: httpx.AsyncClient, host: str) -> None:
        self.client, self.host = client, host

    def supports_json_schema(self, model_name: str) -> bool:
        return False

    async def complete(self, **completion_args: Any) -> Any:
        return await self.client.post(f"https://{self.host}/complete")


@pytest.mark.parametrize("host", ["api.anthropic.com", "resource.openai.azure.com"])
def test_native_preflight_replays_and_redirects_share_one_physical_ceiling(host: str) -> None:
    sends: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        sends.append(request.url.path)
        if request.url.path == "/complete":
            return httpx.Response(307, headers={"location": "/final"})
        return httpx.Response(200)

    async def run() -> None:
        from co_scientist.core.exceptions import LLMCallBudgetExceededError

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(send), follow_redirects=True
        ) as client:
            with benchmark_paid.count_paid_attempts(host):
                bounded = quality_benchmark.BoundedBackend(FakeBackend(client, host), 3)
                await client.post(f"https://{host}/count_tokens")
                await bounded.complete(model="fixture")
                with pytest.raises(LLMCallBudgetExceededError):
                    await bounded.complete(model="fixture")
                assert bounded.calls == 3

    asyncio.run(run())
    assert sends == ["/count_tokens", "/complete", "/final"]


def test_ambiguous_transport_failure_keeps_its_charge() -> None:
    def send(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("synthetic disconnect")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
            with benchmark_paid.count_paid_attempts("api.anthropic.com"):
                bounded = quality_benchmark.BoundedBackend(
                    FakeBackend(client, "api.anthropic.com"), 2
                )
                with pytest.raises(httpx.ReadError):
                    await bounded.complete()
                assert bounded.calls == 1

    asyncio.run(run())


def test_undeclared_provider_or_redirect_never_dispatches() -> None:
    sends = 0

    def send(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(307, headers={"location": "https://other.invalid/"})

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(send), follow_redirects=True
        ) as client:
            provider = benchmark_paid.ProviderBackend(
                FakeBackend(client, "api.anthropic.com"), "anthropic/model", "api.anthropic.com"
            )
            with benchmark_paid.count_paid_attempts("api.anthropic.com"):
                bounded = quality_benchmark.BoundedBackend(provider, 3)
                with pytest.raises(ValueError, match="undeclared fallback"):
                    await bounded.complete(model="azure/other")
                assert sends == 0 and bounded.calls == 0
                with pytest.raises(ValueError, match="redirects"):
                    await bounded.complete(model="anthropic/model")
                assert sends == 1 and bounded.calls == 1

    asyncio.run(run())


def test_credential_endpoint_is_rejected_before_a_counter_exists() -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        ) as client:
            with benchmark_paid.count_paid_attempts("api.anthropic.com"):
                with pytest.raises(ValueError, match="no active physical counter"):
                    await client.post("https://api.anthropic.com/count_tokens")

    asyncio.run(run())


def test_ambiguous_sdk_replay_is_refused_at_the_actual_sdk_seam() -> None:
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    class ReplayBackend:
        def supports_json_schema(self, model_name: str) -> bool:
            return False

        async def complete(self, **completion_args: Any) -> Any:
            handler = object.__new__(AsyncHTTPHandler)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200))
            ) as client:
                return await handler.single_connection_post_request(
                    "https://api.anthropic.com/messages", client, data="{}"
                )

    async def run() -> None:
        with benchmark_paid.count_paid_attempts("api.anthropic.com"):
            provider = benchmark_paid.ProviderBackend(
                ReplayBackend(), "anthropic/model", "api.anthropic.com"
            )
            bounded = quality_benchmark.BoundedBackend(provider, 2)
            with pytest.raises(ValueError, match="ambiguous SDK connection replay"):
                await bounded.complete(model="anthropic/model")
            assert bounded.calls == 0

    asyncio.run(run())
