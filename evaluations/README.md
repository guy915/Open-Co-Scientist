# Evaluations

Reproducible evaluation harness. Everything runs **offline** (no model, no
network) unless a runner says otherwise. Runners write dated JSON artifacts to
`evaluations/results/`, which is created on demand and not tracked.

## Layout

- `_artifacts.py` — result writer. It stamps every artifact with a
  `provenance` block: source commit, branch and dirty state, Python and
  platform, a digest of the engine's prompt templates, and whatever the
  runner knows about the model, seed and cost. A result found later then
  carries what produced it.
- `citation_eval.py` — claim-entailment metrics (precision and recall per
  label, contradiction recall, abstention) over
  `datasets/citation_entailment_v1.json`. `--challenge` scores the
  adversarial `datasets/citation_entailment_challenge_v1.json` panel; with
  `--llm` it exits non-zero below the production gates (contradiction recall
  ≥ 0.80, accuracy ≥ 0.75). See [Citation panels](#citation-panels).
- `safety_eval.py` — per-hypothesis false-negative and false-positive rates
  for the deterministic reviewer (`co_scientist.domains.safety.rules.review_hypothesis_safety`),
  never the semantic escalation. See [Safety splits](#safety-splits).
- `claim_support_eval.py` — unsupported-claim rate over the verdicts a run
  already recorded. It never re-judges them: a second lexical opinion inside
  an eval is how a metric comes to disagree with the product (the Jaccard
  entry in [operations](../docs/OPERATIONS.md)). It reports the rate over
  claims and over ideas with nothing behind them, since a retrieval change
  can move one without the other. Offline, the deterministic backend answers
  every assessment the same way, so offline artifacts carry an
  `offline_disclaimer`: they prove the wiring, not quality.
- `citation_usefulness_eval.py` — does a retrieved span answer the question
  the search was serving? Unlike entailment, this separates a span that is on
  topic from one that answers the question. It scores
  `datasets/citation_usefulness_v1.json`. The default lexical judge is a floor
  (about 0.38 by construction); `--llm` scores a real model. The product does
  not judge its own retrievals this way, so the judge here is the eval's own.
- `_run_driver.py` — plumbing for `claim_support_eval.py --live`. It creates a
  run through `co_scientist.platform.db.runs`, enqueues it with
  `orchestration.task_worker`, drains the worker pool and reads back the
  artifacts.
- `_live_config.py` — sets the environment for live runs before any app
  import: an explicit `openrouter/` model for every role, free models
  required, dotenv off, and every other `*_API_KEY` removed.
- `_usage_evidence.py` — summarizes per-call model, token and price evidence.
  Missing telemetry proves neither free execution nor absent inference.
- `_identity.py` — run and panel controls, policy snapshots and provenance
  checks.
- `smoke.py` — the offline smoke suite with its regression tolerances.
- `attempt_envelope.py` — counts the physical HTTP attempts of one default
  Express run per provider arm, offline. See [Attempt envelope](#attempt-envelope).
- `datasets/` — synthetic, legally shareable labeled sets.
- `tests/` — unit tests for the runners plus the import-contract ratchet.

## Commands

From the repository root, after `make setup`:

```bash
make test-evaluations                                   # harness tests
make eval-smoke                                         # offline smoke suite
make attempt-envelope                                   # HTTP attempts per Express arm

.venv/bin/python -m evaluations.citation_eval                     # v1 panel, lexical assessor
.venv/bin/python -m evaluations.citation_eval --challenge --llm   # challenge panel, gated
.venv/bin/python -m evaluations.safety_eval                       # both safety arms
.venv/bin/python -m evaluations.claim_support_eval                # offline wiring check
.venv/bin/python -m evaluations.claim_support_eval --run <id>     # score a stored run
.venv/bin/python -m evaluations.citation_usefulness_eval [--llm --model openrouter/...]
```

Live panels (`--llm`, `--live`) configure the environment before importing
model code, so run each in a fresh process. Set an explicit OpenRouter
`MODEL_NAME` (or `--model` for citation usefulness) and `OPENROUTER_API_KEY`.
Every role is pinned to that model and transport admission checks that its
current price is zero; no paid or default model is chosen implicitly. A panel
score alone is not evidence of scientific quality.

## Citation panels

- **`citation_entailment_v1`** (20 items) — the offline regression panel. The
  lexical assessor handles it, and `smoke.py` gates it at contradiction
  recall ≥ 0.80.
- **`citation_entailment_challenge_v1`** (30 items) — the production-gate
  panel. Passages share the claim's vocabulary but must be rejected or
  inverted by a semantic reader: numeric and direction conflicts without
  negation, species and model mismatches, temporal mismatches, passages that
  only mention the topic, retracted sources, and low-overlap paraphrase.

| Assessor | Panel | n | Accuracy | Contradiction recall | Gates |
|---|---|---|---|---|---|
| lexical | v1 | 20 | 1.00 | 1.00 | pass |
| lexical | challenge | 30 | 0.03 | 0.00 | fail, as intended |

The lexical assessor's collapse on the challenge panel is the point: token
overlap is a retrieval feature, not proof, so it is only an offline fallback.
Re-run the semantic path with `--challenge --llm` on the current free route
before quoting a live number. The gates are this project's own; Google's
production thresholds are not published, and the panel is synthetic, so a
human-audited sample and calibrated thresholds remain open.

## Safety splits

Each safety item carries a `difficulty`. `easy` is the literal-trigger floor
the classifier was written against; `smoke.py` gates it at zero false
negatives. `hard` is adversarial: paraphrase and synonym evasion, padding past
the regex window, a word-boundary spacing trick, vocabulary the classifier has
no pattern for (nuclear, explosive), and legitimate research that uses a
trigger phrase ("mass casualty" disaster response, "nerve agent" detection
assays). The hard split is measured, never gated.

The runner reports two arms, because the shipped layer is two layers:

- **Floor** — the regex alone. This is also what ships whenever the
  contextual assessor cannot give a clean allow: disabled, offline,
  uncredentialed or erroring. A held verdict then stays held.
- **Permissive ceiling** — the same measurement with an assessor that clears
  every hold put to it, whether weak, captured or persuaded by the text. The
  eligibility gate is the real one, so this arm clears only what the shipped
  code would ask an assessor about.

| Arm | Split | n | False-negative rate | False-positive rate |
|---|---|---|---|---|
| floor | easy | 31 | 0.00 | 0.00 |
| floor | hard | 18 | 0.333 | 0.833 |
| ceiling | easy | 31 | 0.143 | 0.00 |
| ceiling | hard | 18 | 0.333 | 0.00 |

Read the arms together. At the floor the layer withholds 5 of 6 legitimate
near-boundary items; an assessor resolving holds takes that to zero. The worst
case costs one easy adversarial item whose danger is a genuine context call
("test the compound on patients without informed consent"). Acquisition,
yield improvement or a synthesis procedure named against a weapon class are
deterministic blocks no assessor can reach, so the ceiling's hard-split
false-negative rate does not move. The remaining hard misses are paraphrases
with no trigger token, which neither layer closes. Neither arm measures a real
assessor's judgment; together they bound it in each direction.

## Quality benchmark

The manual `Benchmark` workflow (`.github/workflows/benchmark.yml`) runs
`quality_benchmark.py --live --tier express|standard` on a selected fixed goal
and git ref with the free default route. It reports the baseline measurements
below and the descriptive unsupported-claim rate with its claim count and a
95% Wilson interval, and keeps the database, snapshot and receipt as artifacts.
The run stops after 120 minutes (Express) or 240 (Standard), and the job fails
when no rate was written. It needs the `OPENROUTER_API_KEY` repository secret; its MCP server
also needs `OPENALEX_API_KEY` and `TAVILY_API_KEY`, without which there is no
web search and literature retrieval is keyless.

- **Noise floor.** Express runs on unchanged code have ranged from 0.65 to 1.0
  unsupported-claim rate at about 25 claims. Claims cluster within ideas, so
  the run-to-run spread is wider than the interval. Report that rate; never
  gate changes on it. Use the paired check below to inspect quality alongside
  efficiency, and state any loss and uncertainty explicitly.
- **Ration live runs.** Set the shared account daily request budget before
  collection and subtract actual attempts from the remainder before each
  dispatch. Do not benchmark changes that cannot alter model output
  (caching, backend, frontend, CI); measure those offline. Batch prompt,
  reasoning-budget and retry changes into a three-goal paired Express cohort;
  reuse recorded main baselines only while the comparison controls match.
- **Token admission keeps the default 16M client ceiling.** Each call reserves
  its input-byte bound and full output cap before dispatch. Successful calls
  with reported usage settle to real prompt plus completion tokens. Failed
  calls, missing usage and interrupted streams keep the full reservation.
- Extended and Ultra get no live runs; check their call envelopes offline.

### Paired quality check

The fixed v1 goals in `quality_goals.py` span cell biology (mitochondrial
quality control and senescence), battery materials (aqueous zinc-ion capacity
fade), and urban hydrology (stormwater interventions under extreme rainfall).
Use one Express run per goal on each ref. Pin the exact source commits, model
roles, retrieval configuration and free-route policy; changed model or tool
controls invalidate a pair. Config and routing snapshots remain in the JSON
so intentional efficiency changes can be reviewed. Keep run databases and
report packets private, outside commits.

Download the six saved `snapshot.db` files, which include independent dispatch
receipts, and create `pairs.json` using paths relative to that manifest.
Keep any accompanying `snapshot.db-wal`; legacy metadata may still be there.
It contains exactly one entry per fixed goal:

```json
{
  "pairs": [
    {
      "goal_id": "cell-biology",
      "main": {"db": "biology-main.db", "run_id": "saved-run-id", "source_commit": "40-character-source-SHA"},
      "branch": {"db": "biology-branch.db", "run_id": "saved-run-id", "source_commit": "40-character-source-SHA"}
    }
  ]
}
```

Include equivalent entries for `battery-materials` and `urban-hydrology`.
The supplied source SHA identifies the producing checkout, not the checkout
that later analyzes it. Use the benchmark receipt to verify it.

```bash
# One comparison command; only this explicit mode makes free-route judge calls:
MODEL_NAME=openrouter/inclusionai/ling-3.1-flash \
  .venv/bin/python -m evaluations.benchmark_transport compare pairs.json --live-judge --output results/paired

# Hermetic replay of saved judgments (no provider imports or network):
.venv/bin/python -m evaluations.paired_quality pairs.json --judgments judgments.json --output results/paired
```

The command writes one Markdown table and JSON. It counts full numbered idea
entries in the saved final report, separating featured ideas from entries
marked "screened, not deep-verified"; title mentions elsewhere do not count.
JSON also includes generated ideas, delivered IDs, all/delivered verification
mixes, claim verdicts, distinct supported claims (`supports` or `partial`),
task-span wall time, physical calls and prompt/completion tokens. Missing
usage is unknown, never zero. Wall time spans the earliest task start to the
latest task completion, including waits; it is not summed call latency.
When a versioned HTTP attempt receipt is present, its physical calls
take precedence over task telemetry (failed tasks can lose usage records).
Missing token records then make total tokens unknown. Legacy invocation receipts
make live physical counts unknown. Databases without a receipt expose
`recorded_telemetry_only` as the call-count basis, not a claim of complete dispatch coverage.

The judge sees only the fixed goal, rubric and two final reports, with provider
and prepared-date lines removed. Both orders run independently; a winner must
agree after mapping positions back to refs, otherwise the result is a tie.
Invalid or missing judgment fails the check rather than inventing a tie.
Results retain exact packet hashes and both rationales. To replay live output,
collect each order's `packet_sha256`, `winner` and `rationale` into a JSON object
keyed by the packet hash. `--export-packets --output packets.json` exports the
exact blind inputs for an independent recorded judge.

The sample is **three goal pairs, six research runs and six judge orders**.
Two orders are one judgment, not two independent samples. Even three consistent
wins give two-sided sign-test p=0.25: differences remain inside this sample's
noise and neither equivalence nor a small quality loss is established. A
completion failure, fewer delivered ideas, fewer delivered supported claims,
a consistent main preference or missing efficiency telemetry requires review
(exit 2). Otherwise the result is inconclusive (exit 0), never certified quality
parity. Offline runs always require review as wiring evidence only. Live request
budget must be set before collection; judge calls count toward it.
The live judge has a hard six-physical-request budget across all orders,
including retries. A larger request budget can be set explicitly with
`--max-judge-calls`; exhaustion fails instead of inventing missing judgments.
`make test-evaluations` exercises the entire replay on fake recorded SQLite
databases with network connections forbidden and checks that inputs are unchanged.

### Fixed-goal collection

Collect one main baseline per fixed goal on Express, after setting the shared daily
physical-request budget. Use a clean checkout, an explicit zero-priced
OpenRouter `MODEL_NAME` and `OPENROUTER_API_KEY`, with retrieval configured
identically for both refs. No Azure or paid fallback is admitted.

```bash
.venv/bin/python -m evaluations.benchmark_transport collect --goal-id cell-biology \
  --live --max-calls 150 --output /tmp/biology-main
```

Repeat for `battery-materials` and `urban-hydrology`, within the shared daily request
budget; collection never repeats automatically. An output directory with
an existing run database is refused. Each fresh process saves `snapshot.db`,
`receipt.json`, `baseline.md` and `claim-support-live.json`. The receipt records
the exact source SHA, resolved controls, dispatch count and ceiling. Use the
snapshot and that source SHA in the paired manifest. `run.db` is also retained
for local inspection; the snapshot includes WAL contents and the evaluation
dispatch receipt.

The live measurement launcher enforces `--max-calls` across concurrent HTTP attempts and retries
(1–450 requests; default 150), independently of task admission counters. Set a
larger bound only within the remaining daily request budget; main Express may
need more than 150 attempts. Track aggregate use across goals and judge calls.
Use `python -m evaluations.benchmark_transport collect` in place of the direct
collector command for live runs, and `python -m evaluations.benchmark_transport
compare` in place of the direct paired command for live judging. The launcher
counts HTTP transport attempts before sending, including SDK connection replays
and redirects. It disables hidden connect retries and records the counter's
code/SDK identity digest in the snapshot. Zero SDK retry options alone do not disable
OpenRouter's separate connection replay. All such attempts consume the request budget.
Legacy receipts record backend invocations; their live HTTP count and token
total remain unknown and require review. The artifact-based live dispatch
rejects legacy counters before judging. An exhausted or
failed run stays incomplete, exits 2 and retains its database and receipts;
do not treat it as a successful baseline. `claim-support-live.json` retains
the existing rate, sample size and Wilson interval as descriptive measurements.
Default collection is offline; omit `--live` to exercise persistence without
provider credentials. The offline artifact and receipt explicitly disclaim
scientific quality, and the collector's tests deny socket connections.

Artifacts contain checked databases, receipts and reports. Raw MCP logs and
downloaded source ZIPs are excluded: console secret masking does not redact
artifact bytes, and historical MCP refs can log credential-bearing URLs.
Before publishing, the trusted harness checks outputs for configured provider
credentials in literal, URL and JSON forms; unsafe files block the entire upload
without altering scientific databases. The comparison checks prepared inputs
before judging and outputs before publishing. A failed or missing check cannot
authorize an upload. Keep local raw diagnostics private. If an older artifact
exposed a key, contain that artifact and have its owner revoke/rotate the key;
this check cannot revoke copies already obtained.

The manual **Benchmark** workflow accepts `goal_id`, `benchmark_ref`, `tier`,
`max_calls` and `label`. Dispatch one fixed goal on an exact source SHA:

```bash
gh workflow run benchmark.yml --ref main -f goal_id=cell-biology \
  -f benchmark_ref=SOURCE_SHA -f tier=express -f max_calls=150 -f label=biology-main
```

Its summary includes the baseline row and descriptive claim sample/interval;
the uploaded artifact retains the checked database, snapshot and receipt even
when the run step fails, provided the credential check passes. Dispatch the other
goals only within the remaining daily request budget. The workflow never runs on pull requests.
Collection installs the selected research engine in an isolated clean worktree
and uses the dispatched measurement launcher outside it. This instruments older
research refs without editing them; receipts retain the actual research SHA and
measurement-counter digest. New snapshots checkpoint their private copy before
analysis; the artifact importer also preserves any legacy WAL. Both arms must use the same measurement counter
and SDK versions.

After collecting the three main and three branch runs, compare their artifacts
with one dispatch (the IDs can be in any goal order):

```bash
gh workflow run benchmark.yml --ref main -f operation=compare \
  -f main_runs=MAIN_BIOLOGY_ID,MAIN_BATTERY_ID,MAIN_HYDROLOGY_ID \
  -f branch_runs=BRANCH_BIOLOGY_ID,BRANCH_BATTERY_ID,BRANCH_HYDROLOGY_ID \
  -f max_judge_calls=6 -f label=efficiency-batch
```

`benchmark_ref` applies to collection only. Comparison uses the dispatched
harness revision, validates the six saved Express goal/source/identity and
independent request receipts, then judges the reports through the existing
free-route secret. It emits one table and JSON with both order judgments,
sample size and uncertainty. Review-required results exit 2 and retain their
artifacts; failed validation spends no judge requests. Allow for all judge
attempts in the shared daily request budget, and conservatively charge its ceiling if
interruption prevents a final usage receipt. Download artifacts before their
14-day expiry if they will be reused.

For hermetic replay, `python -m evaluations.paired_artifacts --archives
archives.json --output replay` prepares the same manifest from recorded ZIPs.
The JSON maps `main` and `branch` to three ZIP paths each, relative to that
file. Then run the recorded-judgment command above against
`replay/prepared/pairs.json`. No network is used unless `--download` or
`--live-judge` is explicitly selected.

### Native Anthropic cohort

The shared monetary budget must cover the run: the HTTP ceiling
alone does not enforce a shared money ledger. Resolve
the money reservation and billing controls before provider-backed collection.
The `operation=preflight` workflow checks native configuration with sockets
denied and makes no provider request; it is not authentication or science.

An explicitly authorized paid cohort can use `model=anthropic/MODEL_ID` on the
same workflow. Azure is refused: its credit is admitted only by the production
API's recorded allowance ([Azure setup](../docs/azure-setup.md)), and a runner's
empty ledger would bypass it. The default remains the free
OpenRouter route. Select one provider/model for all six research runs; switching
providers after an incomplete run does not make an incompatible pair valid.
The comparison requires the same provider policy, endpoint digest and configured
model roles across all six archives. Paid results are a separate cohort, not a
free-route quality certification.

Claude requires `ANTHROPIC_API_KEY`. Set it in the execution environment or an
existing repository secret, never in workflow inputs or committed files. The paid
path uses the credential owner's explicit key scope. Missing configuration refuses
execution before provider calls. Other provider credentials and dotenv loading
are disabled. Azure key names stay in the artifact credential guard.

```bash
gh workflow run benchmark.yml --ref main -f operation=collect -f tier=express \
  -f goal_id=cell-biology -f benchmark_ref=SOURCE_SHA -f max_calls=160 \
  -f model=anthropic/claude-sonnet-5-5 -f label=paid-biology-main
```

The local equivalent uses the trusted launcher outside the clean research
checkout, without editing its source:

```bash
python /path/to/harness/evaluations/benchmark_paid.py collect \
  --provider anthropic --model anthropic/claude-sonnet-5-5 --live --tier express \
  --goal-id cell-biology --max-calls 160 --output /private/new-biology-output
```

The counter includes native token-count preflights, SDK attempts and redirects
to the declared provider. An ambiguous connection replay and undeclared provider
or model are refused. The counter identity includes the paid adapter and SDK
versions; all attempts consume the declared ceiling. Report unknown billing and
missing token totals explicitly. Do not overwrite or rerun outcomes for score.

Use the same `model` input for the six-run comparison dispatch above. For
recorded replay, add `--provider anthropic` or `--provider azure` to
`evaluations.paired_artifacts`; without this explicit option, paid archives are
rejected by the original free path. Recorded judging remains hermetic.

## Attempt envelope

`make attempt-envelope` (after `make setup` and `make test-mcp`) runs one
default Express workflow per arm, Anthropic (Haiku with `count_tokens`) and Azure
(Luna), through the real API, durable worker, LLM gateway, MCP client
and MCP server subprocess. Only the bottom of each transport is fake: the
httpx transports and LiteLLM's aiohttp transport in both processes, and the
urllib opener Biopython's Entrez sends through. Every attempt they see counts,
including retries and failures, by category: LLM completion, `count_tokens`,
PubMed/NCBI, OpenAlex, arXiv, Europe PMC, Brave/Tavily, `read_url` and the
engine's citation probes. Engine-to-MCP traffic on loopback is not counted.

Answers come from the offline filler with the call meter's realism rules
(`app/tests/_envelope_fakes.py`), so the run follows its first-success path:
no upstream fails, throttles or times out, and none is cancelled. Treat the
total as a floor for a live run, not a bound. Hypothesis IDs seed the answers,
so counts move by a few percent between runs; compare ranges, not single runs.
The table adds the rest of a full live check sequence (a cache wave of 8
attempts and up to 20 for judging) and compares it with the 270-attempt
operational limit; it reports, and never adjusts, a total above it.
`ENGINE_DIR=<checkout>/engine` measures another checkout for a before/after
pair. No credential is read; the run removes any from its environment.

## External gaps

These need data, credentials, expert panels or wet labs that are not
available here:

- Google's private 1,200-goal safety benchmark and 203-goal scaling corpus are
  request-only.
- Wet-lab validation (AML, liver fibrosis, antimicrobial resistance) is out of
  scope.

`citation_eval.py` and `safety_eval.py` record their gap in each artifact's
`external_gap` field.
