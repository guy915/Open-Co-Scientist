# Production Python dependencies

The production images install these Python 3.12/Linux runtime closures with
`pip install --require-hashes`. The local engine is then installed with
`--no-deps`; its source remains the checkout being built.

| Lock | Inputs | Consumer |
| --- | --- | --- |
| `api.txt` | `engine/pyproject.toml` | `Dockerfile.api` |
| `mcp.txt` | `engine/mcp_server/pyproject.toml` | `Dockerfile.mcp` |
| `skills.txt` | `skills.in` | API's isolated science-skills interpreter |

Regenerate from the repository root with [uv](https://docs.astral.sh/uv/)
0.12.19 and review the version changes:

```bash
uv pip compile engine/pyproject.toml --python-version 3.12 --python-platform linux --generate-hashes --no-emit-index-url -o requirements/api.txt
uv pip compile engine/mcp_server/pyproject.toml --python-version 3.12 --python-platform linux --generate-hashes --no-emit-index-url -o requirements/mcp.txt
uv pip compile requirements/skills.in --python-version 3.12 --python-platform linux --generate-hashes --no-emit-index-url -o requirements/skills.txt
```

Existing locks retain their versions by default. Add `--upgrade-package NAME`
for an intentional update, or `--upgrade` for a reviewed full refresh.
Never edit hashes by hand. Changing runtime dependency metadata requires
regenerating its lock and rebuilding both production images with
`make docker-build`. Dev-only dependency changes do not require these locks.

These locks cover Python runtime packages. System packages and build tools
are separate inputs; this does not promise byte-identical image rebuilds.
Local development supports macOS and Linux and uses the package metadata
and Bun locks through `make setup`. The internal engine package requires
Python 3.12+; these deployment locks cover Python 3.12 on Linux only.
