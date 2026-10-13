# app

The workbench and the API's packaging. The server code itself is the
`co_scientist` package in [`engine/src/`](../engine/src/co_scientist/); its
composition root is `co_scientist.main`.

| Path | Contents |
| --- | --- |
| `frontend/` | React workbench; see [its README](frontend/README.md) |
| `tests/` | API test suite (`make test-app`) |
| `dev/` | Operator scripts: `backup_db.py` (consistent SQLite backups, see [launch](../docs/LAUNCH.md#backup-and-restore)) and `refresh_retractions.py` (builds an optional local retraction DOI list) |
| `pyproject.toml` | Ruff, mypy and pytest settings for `tests/`; runtime dependencies live in `engine/pyproject.toml` |
| `.env.example` | Every API setting with its default |
| `docker-compose.yml`, `docker/` | Development containers for the API and MCP server |
| `Makefile` | Shortcuts for working from this folder |

Use the root `Makefile` for everyday work: `make setup`, `make start` and
`make check` (see [local setup](../docs/RUNNING-LOCALLY.md)). Production
images are built from the root `Dockerfile.api`; see
[deployment](../docs/DEPLOYMENT.md).

## Optional independent retraction list

Source-provided retraction flags remain active. No independent retraction dataset
is distributed. An operator who has established permission to use a feed can run
`python app/dev/refresh_retractions.py --output outputs/retractions.txt.gz`, then
set `COSCIENTIST_RETRACTIONS_PATH` to that local gzip file. Keep it outside the
source distribution and outside the SQLite data volume. Without this option,
DOI reachability alone does not establish that a paper has not been retracted.
