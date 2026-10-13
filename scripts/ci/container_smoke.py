import argparse
import subprocess
import sys
import time
import uuid


def docker(
    *args: str, capture: bool = True, timeout: float = 30, check: bool = True
) -> str:
    result = subprocess.run(
        ["docker", *args],
        check=check,
        text=True,
        timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    return (result.stdout or "").strip()


def wait_ready(
    name: str, path: str, port: int, timeout: float, host: str = "127.0.0.1"
) -> None:
    deadline = time.monotonic() + timeout
    code = (
        "import json, urllib.request; "
        "opener = urllib.request.build_opener(urllib.request.ProxyHandler({})); "
        f"response = opener.open('http://{host}:{port}{path}', timeout=2); "
        "assert response.status == 200; "
        "payload = json.load(response); "
        "assert payload['status'] in ('healthy', 'degraded', 'running')"
    )
    try:
        while time.monotonic() < deadline:
            if (
                docker(
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    name,
                    timeout=min(3, max(0.1, deadline - time.monotonic())),
                )
                != "true"
            ):
                raise RuntimeError(f"{name}: lifespan/startup exited before readiness")
            try:
                docker(
                    "exec",
                    name,
                    "python",
                    "-c",
                    code,
                    timeout=min(3, max(0.1, deadline - time.monotonic())),
                )
                print(f"{name}: {path} ready")
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise RuntimeError(f"{name}: readiness did not pass within {timeout:g} s")
    except Exception:
        docker("logs", name, capture=False)
        raise


def launch_container(name: str, args: list[str]) -> None:
    try:
        docker(*args)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(error.stderr or str(error), file=sys.stderr)
        docker("logs", name, capture=False, check=False)
        raise


def smoke(api_image: str, mcp_image: str) -> None:
    prefix = f"coscientist-smoke-{uuid.uuid4().hex[:12]}"
    volume = f"{prefix}-data"
    names = [f"{prefix}-{variant}" for variant in ("api-root", "api-user", "mcp")]
    docker("volume", "create", volume)
    try:
        for index, name in enumerate(names):
            args = [
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                "none",
                "--env",
                "PYTHON_DOTENV_DISABLED=1",
            ]
            if index < 2:
                args += [
                    "--env",
                    "COSCIENTIST_TEST_DOUBLE=deterministic",
                    "--env",
                    "PORT=8008",
                    # Readiness has no visitor header even from a trusted peer.
                    "--env",
                    "COSCIENTIST_TRUSTED_PROXY_CIDRS=127.0.0.1/32",
                ]
                if index == 0:
                    # volume-nocopy preserves the root-owned empty mount Railway supplies.
                    args += [
                        "--user",
                        "0",
                        "--mount",
                        f"type=volume,src={volume},dst=/app/data,volume-nocopy",
                    ]
                args.append(api_image)
                launch_container(name, args)
                wait_ready(name, "/health", 8008, 60)
                # VFS runners copy a full image layer for each live container.
                docker("rm", "-f", name, check=False)
            else:
                args += [
                    "--env",
                    "COSCIENTIST_MCP_SHARED_SECRET=ci-smoke-test-placeholder",
                    mcp_image,
                ]
                launch_container(name, args)
                wait_ready(name, "/", 8888, 60, "[::1]")
    finally:
        for name in names:
            docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-image", default="coscientist-api-ci")
    parser.add_argument("--mcp-image", default="coscientist-mcp-ci")
    args = parser.parse_args()
    smoke(args.api_image, args.mcp_image)
