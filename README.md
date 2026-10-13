<h1 align="center">Open Co-Scientist</h1>

<p align="center">
  <strong>An open multi-agent research partner that turns a research goal into ranked, literature-grounded hypotheses.</strong>
</p>

<p align="center">
  <a href="https://open-coscientist.com"><img src="docs/assets/badges/try-it-outline.svg" alt="Try it at open-coscientist.com"/></a>
  <a href="#quick-start"><img src="docs/assets/badges/run-locally-outline.svg" alt="Run it locally"/></a>
  <a href="#how-a-run-works"><img src="docs/assets/badges/how-it-works-outline.svg" alt="How it works"/></a>
  <a href="CONTRIBUTING.md"><img src="docs/assets/badges/contribute-outline.svg" alt="Contribute"/></a>
  <a href="https://deepwiki.com/guy915/Open-Co-Scientist"><img src="docs/assets/badges/deepwiki-outline.svg" alt="DeepWiki"/></a>
</p>

<p align="center">
  <img src="docs/assets/workbench-light.png" alt="A finished Express run in the Open Co-Scientist workbench: ranked hypotheses with Elo ratings beside the selected idea's overview, description and lineage" width="1000"/>
</p>

Describe what you want to understand, such as a mechanism, a drug-repurposing
question or an unexplained observation. Open Co-Scientist searches the
literature, drafts competing hypotheses, critiques and fact-checks them, runs
an Elo tournament of scientific debates, and evolves the strongest ideas. You
get a report with ranked hypotheses, the mechanism behind each, a proposed
experiment, and the sources every claim rests on. You can steer the run while
it works.

It is an open, independent implementation of the agent design in Google's
[AI co-scientist](https://arxiv.org/abs/2502.18864). It is not affiliated with
or endorsed by Google.

<p align="center">
  <img src="docs/assets/express-workflow.gif" alt="An Express run from a typed goal through the setup questions to the ranked ideas, at double speed" width="1000"/>
  <br/>
  <sub>An illustrative Express workflow with scripted responses and sample results, shown at double speed.</sub>
</p>

## How a run works

<p align="center">
  <img src="docs/assets/co-scientist-architecture.png" alt="Open Co-Scientist architecture: a scientist supplies a research goal and feedback; a supervisor coordinates generation, reflection, ranking, proximity, meta-review and evolution agents through workers and context memory, returning a research overview with detailed hypotheses" width="1000"/>
</p>

Adapted in the Open Co-Scientist palette from Figure 2 of Gottweis et al.,
[*Towards an AI co-scientist*](https://arxiv.org/abs/2502.18864v1), page 9
(2025). [Original diagram](docs/assets/co-scientist-architecture-original.png).

The supervisor model advises on each next step, and a deterministic policy
enforces budgets, forced transitions and convergence from live statistics such
as pool growth, Elo stability and match coverage. Every step is a durable task,
so a run survives restarts and resumes where it stopped.

## Features

| Capability | What you get |
|---|---|
| **Cooperating agents** | Six agents under a supervisor: generation, reflection, ranking, evolution, proximity and meta-review, following the published design. |
| **Evidence you can check** | Hypotheses cite retrieved sources with `[C*]` keys, and retracted papers are flagged. For the ranked finalists featured in the report, each claim is checked against its passages for support or contradiction. |
| **Literature and database tools** | A bundled MCP server provides PubMed and PMC full text, OpenAlex, Europe PMC, arXiv and preprints, ChEMBL, UniProt, STRING, Reactome, Open Targets, Ensembl, gnomAD, the GWAS Catalog, ClinicalTrials.gov, and web search when a search key is configured. |
| **Steering and questions** | Add constraints or ask about the evidence while a run works, and attach your own documents. |
| **Safety screening** | Screening of the goal, each hypothesis and the final report. |
| **Bring your own key** | Use your preferred provider and larger run sizes; keys are encrypted at rest. |
| **Deterministic test backend** | Tests and browser suites exercise the whole pipeline without an API key using `COSCIENTIST_TEST_DOUBLE=deterministic`. |

## Honest limits

- **Free model routes.** The hosted service runs on free OpenRouter routes:
  Ling 3.1 Flash, with free Nemotron fallbacks. They are slower and weaker
  than frontier models, their zero price is the provider's decision and can
  end, and results depend heavily on the model.
- **Capacity.** Without your own key, each browser gets three Express runs a
  day by default, behind per-network and global daily caps. The free routes
  are rate-limited for the whole deployment, so a busy day can park runs until
  the daily reset. Larger run sizes need your own key. Runs take from tens of
  minutes to several hours, depending on size and provider speed.
- **One server.** The API is a single process on SQLite. It favors durability
  and simplicity over scale.
- **Hypotheses, not findings.** Passing tests shows the software works, not
  that its hypotheses are right. Treat every output as a starting point for
  expert review and experiment. The [evaluations](evaluations/README.md) say
  what has and has not been measured.
- **Free routes and your data.** Free model routes may keep the prompts they
  receive. Do not submit confidential or unpublished work without your own key
  and a provider whose terms you accept.
- **Private by obscurity.** Runs belong to a per-browser ID, not a verified
  account.

## Quick start

You need Python 3.12, Node.js 22.13+ and Bun 1.3.14 on macOS or Linux (on
Windows, use WSL2).

```bash
git clone https://github.com/guy915/Open-Co-Scientist.git
cd Open-Co-Scientist
make setup    # Python virtual environment and locked frontend dependencies
make start    # API on :8008, workbench on :5173, MCP server on :8888
```

`make start` opens [localhost:5173](http://localhost:5173) when it is ready,
and stops anything already listening on those three ports. A run needs a
model credential: without one, requests fail with "No model is available right
now". Set `OPENROUTER_API_KEY` in the root `.env` that `make setup` created; see
[`.env.example`](.env.example) and the
[full API configuration](app/.env.example). The MCP server reads its own
`engine/mcp_server/.env` ([template](engine/mcp_server/.env.example)).

`make check` runs the CI checks apart from the image builds: lint, types,
every test suite, the evaluation smoke test, the production build and the
browser tests. `make help` lists
every target, and [local setup](docs/RUNNING-LOCALLY.md) covers details and
troubleshooting.

## Architecture

```mermaid
flowchart TB
    ui[React workbench] -->|HTTP requests| api[FastAPI API]
    api -->|Progress via server-sent events| ui
    subgraph engine[Python package: co_scientist]
        api -->|Enqueue tasks| runtime[Durable task runtime]
        runtime --> agents[Scientific agents]
        agents --> models[Model gateway]
        agents --> retrieval[Retrieval]
        api --> store[(SQLite store)]
        runtime --> store
    end
    models --> providers[Model providers]
    retrieval --> mcp[MCP server]
    mcp --> sources[Literature and databases]

    classDef interface fill:#dcfce7,stroke:#15803d,color:#14532d
    classDef service fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef external fill:#f3e8ff,stroke:#9333ea,color:#581c87
    class ui interface
    class api,runtime,agents,models,retrieval,store service
    class providers,mcp,sources external
```

Open Co-Scientist is one Python package, `co_scientist`
(`engine/src/co_scientist/`), served by FastAPI. It is layered from the
composition root through the API, orchestration, the scientific agents and
domain logic down to the platform (models, retrieval, sandbox and the SQLite
store) and core, and import contracts enforce that layering in CI. A React
workbench (`app/frontend/`) streams progress over server-sent events, and a
reference MCP server (`engine/mcp_server/`) provides the literature and
database tools. Read the [architecture](docs/ARCHITECTURE.md), the
[engine architecture](engine/docs/ARCHITECTURE.md) and the
[documentation index](docs/README.md).

## Contributing

Scientists and developers are both welcome. You can report a bug, propose a
research question or an evaluation dataset, reproduce a result, or send code.
Start with [CONTRIBUTING.md](CONTRIBUTING.md), and ask questions in
[Discussions](https://github.com/guy915/Open-Co-Scientist/discussions).
Everyone follows the [Code of Conduct](CODE_OF_CONDUCT.md). Report
vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License and credit

First-party software is licensed under [Apache 2.0](LICENSE).
[NOTICE](NOTICE) records third-party software and data; vendored material,
cited papers and scientific data keep their own terms.

This project builds on Google's AI co-scientist research:

- Gottweis et al., [Towards an AI co-scientist](https://arxiv.org/abs/2502.18864), 2025.
- [Accelerating scientific discovery with Co-Scientist](https://doi.org/10.1038/s41586-026-10644-y), *Nature*, 2026.

It also draws on [Science Skills](https://github.com/google-deepmind/science-skills)
(vendored under `vendor/`), [Jataware's open-coscientist](https://github.com/jataware/open-coscientist),
[Sakana AI's AI Scientist](https://github.com/SakanaAI/AI-Scientist) and
[Pi Agent](https://github.com/Dicklesworthstone/pi_agent_rust).
