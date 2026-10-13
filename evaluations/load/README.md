# Local launch load

Requires Docker Engine and Compose on a Linux Docker host. All runtime services
use an internal Docker network; the harness accepts only `http://api:8008`.
No production URL, credential, dotenv file or production database is used.
`run.sh` recreates only this project's disposable `load-db` volume and removes
its orphaned sampler after interrupted trials. Static assets are excluded
because the CDN serves them.

Build from the repository root:

```sh
docker build -f Dockerfile.api -t coscientist-launch-load:local .
docker build -f evaluations/load/Dockerfile.locust -t coscientist-locust:local .
evaluations/load/run.sh
```

The API uses the root production image, one Uvicorn process, one embedded
worker (`WORKER_POOL_SIZE=1`), SQLite WAL on a volume, and 2 CPU / 2 GiB limits.
HTTP keepalive is fifteen seconds, matching the production entrypoint. A mounted
adapter wraps the production app and replaces only process credential/offline
detection and physical completions. Free-run, storage and provider admission
remain enabled. Responses use the deterministic schema backend with 100 ms
I/O and streamed text. No provider keys are present; outbound runtime networking
is blocked. Real model/MCP throughput and production ingress are unmeasured.

One Locust worker adds forty source-address aliases inside its container, using
NET_ADMIN only on the isolated `172.29.251.0/24` network. Requests bind real
source addresses; forwarded headers never supply identity. This keeps the
production eight-stream connecting-host guard in force without forty separate
Python containers. `LOAD_PEERS` supports 4–128 addresses. Reserve this subnet
from other local projects, and run only one instance of this named project.
The generator, master and sampler are outside the API's resource limits.

Owned draft fixtures are seeded after startup recovery, with one event and no
worker tasks. Draft SSE closes after thirty idle seconds and reconnects; real
start traffic enters the durable queue. Report/chat use a private copy of a
packaged example. Only fixture input-admission counters are cleared before
measurement, never by a serving transaction or polling tick.

The default five-minute mixed workload is:

- 500 landing visits/minute: ten paced users, each visit with a new browser
  identity, call `/status`, `/api/free-usage`, `/api/runs`, `/api/runs/demo`,
  `/api/interviews` and the scoped `/api/logs` warning probe.
- 200 owned run pages: snapshot, SSE including idle reconnection, and history
  plus run-detail refresh every ten seconds. These extra periodic reads are a
  conservative workload; the frontend normally refreshes in response to events.
  SSE latency measures header establishment, not the stream's lifetime.
- 50 distinct browser identities attempt Express creation/start six seconds
  apart from one connecting host. Most must be clearly refused by the unchanged
  six-per-host daily free limit. Accepted starts can subsequently hit their
  physical provider allowance; they are not counted as completed runs.
- 20 report visits/minute, all seven collections and Markdown download.
- 10 streamed Q&A turns/minute against a private completed report; a done frame
  is required and error frames count as failures.

Cross-origin OPTIONS preflights are included by default, cached per browser and
route, and reset for each new landing/start identity. Every 403/409/429 refusal
must have a nonempty human-readable detail; raw 500s count as failures. SSE
refusals honor Retry-After with at least thirty seconds between attempts. A
single GET/HEAD/OPTIONS retry recovers an idle reused socket closing before
headers, matching the verified Chromium behavior. Connect failures, POSTs,
HTTP statuses and streaming bodies are never retried by this adapter. Recovered
idle retries are counted separately and their recovery time remains in latency.

The socket regression is part of `make test-evaluations` and proves both safe
GET recovery and no replay after a POST body was received:

```sh
.venv/bin/python -m pytest evaluations/tests/test_launch_load_client.py -q
```

A truncated SSE body must count as a failure and leave its watcher alive for
bounded reconnection. Check this with the optional generator image, without
starting the API:

```sh
docker run --rm --network none -v "$PWD/evaluations/load:/load:ro" \
  --entrypoint python coscientist-locust:local /load/probe_stream_body.py
```

Repeat or change the offered workload:

```sh
LOAD_RESULTS=./results/mixed evaluations/load/run.sh
SSE_PAGES=300 VISITORS_PER_MINUTE=1000 START_ATTEMPTS=0 LOAD_DURATION=120 \
  LOAD_RESULTS=./results/over-cap evaluations/load/run.sh
```

Additional local knobs are `LOAD_PEERS`, `LOAD_MODEL_DELAY`, `LOAD_API_IMAGE`,
`LOAD_GENERATOR_IMAGE`, `LOAD_HTTP_KEEPALIVE` (for a 5/15-second comparison), `LOAD_PREFLIGHT` (0 disables),
`SSE_MAX_CONNECTIONS` and `MAX_CONCURRENT_RUNS`. Do not raise free/provider limits
to make a trial pass. Check actual counts: closed-loop pacing can miss its
requested rate under saturation. Requested rate is not achieved throughput.

The runner saves image IDs, source revision, resolved compose configuration,
API/Locust logs, CSV/HTML percentiles and failure tables, scenario counts and
one-second RSS/high-water/FD/active-SSE samples. JSON summaries separate expected
refusals from unexpected errors and record every process exit. Fixture/control
requests are excluded from workload counts. An unhealthy sampler makes capacity
unmeasured; do not accept a run with missing telemetry.

The adapter times SQLite BEGIN IMMEDIATE acquisition (lock waiting plus syscall
cost), counts busy/locked exceptions and samples event-loop scheduling lag every
100 ms. It retains bounded 100,000-value timing windows; counts beyond that
size report the most recent window. SQL statement timing includes measurement
overhead. FD sampling can miss short peaks; VmHWM records process lifetime peak.
Measurement percentiles come from the last healthy sample, including the short
end-of-test idle tail; final cleanup metrics are separate. Stop the load stack
before full backend/browser gates:

```sh
docker compose -f evaluations/load/compose.yml down --volumes --remove-orphans
```

Capacity and spike steps are in [OPERATIONS.md](../../docs/OPERATIONS.md#load-and-capacity).
Keep generated measurements, raw logs, reports and disposable data outside
the published source tree. Record the tested image and harness identity in
your local receipt.

If the workspace intercepts TLS, add its trusted CA only to a temporary build
layer with certificate verification enabled. Do not commit that CA or change
the production Dockerfile. Locust is optional, installed only in the generator
image and never in the API dependency closure.
