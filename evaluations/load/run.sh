#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${LOAD_PEERS:=40}" "${LOAD_DURATION:=300}" "${LOAD_RESULTS:=./results/$(date -u +%Y%m%dT%H%M%SZ)}"
export LOAD_PEERS LOAD_DURATION LOAD_RESULTS
mkdir -p "$LOAD_RESULTS"
git rev-parse HEAD > "$LOAD_RESULTS/source-revision.txt"
sha256sum compose.yml client.py locustfile.py test_api.py worker.py sample.py summarize.py run.sh > "$LOAD_RESULTS/harness-sha256.txt"
docker image inspect "${LOAD_API_IMAGE:-coscientist-launch-load:local}" --format '{{.Id}}' > "$LOAD_RESULTS/api-image.txt"
docker image inspect "${LOAD_GENERATOR_IMAGE:-coscientist-locust:local}" --format '{{.Id}}' > "$LOAD_RESULTS/generator-image.txt"
docker compose -f compose.yml config > "$LOAD_RESULTS/compose.yml"
docker compose -f compose.yml down --volumes --remove-orphans > "$LOAD_RESULTS/stack.log" 2>&1
docker compose -f compose.yml up -d api >> "$LOAD_RESULTS/stack.log" 2>&1
api_id=$(docker compose -f compose.yml ps -q api)
for ((attempt=0; attempt<60; attempt++)); do
  if docker exec "$api_id" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/__load__/fixture')" >/dev/null 2>&1; then break; fi
  if ((attempt == 59)); then docker logs "$api_id"; exit 1; fi
  sleep 1
done
docker compose -f compose.yml run --no-deps -d --name coscientist-load-sampler sampler /load/sample.py /results/telemetry.jsonl --seconds "$((LOAD_DURATION + 30))" >> "$LOAD_RESULTS/stack.log" 2>&1
docker compose -f compose.yml up -d master worker >> "$LOAD_RESULTS/stack.log" 2>&1
master_id=$(docker compose -f compose.yml ps -q master)
docker wait "$master_id" > "$LOAD_RESULTS/master-exit.txt"
docker logs "$master_id" > "$LOAD_RESULTS/locust.log" 2>&1
docker logs "$api_id" > "$LOAD_RESULTS/api.log" 2>&1
docker wait coscientist-load-sampler > "$LOAD_RESULTS/sampler-exit.txt"
docker rm coscientist-load-sampler >/dev/null
docker inspect $(docker compose -f compose.yml ps -a -q worker) --format '{{.State.ExitCode}}' > "$LOAD_RESULTS/worker-exits.txt"
docker exec "$api_id" python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8008/__load__/metrics').read().decode())" > "$LOAD_RESULTS/final-metrics.json"
docker compose -f compose.yml stop api >> "$LOAD_RESULTS/stack.log" 2>&1
python summarize.py "$LOAD_RESULTS"
