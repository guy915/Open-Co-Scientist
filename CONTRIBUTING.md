# Contributing to Open Co-Scientist

Welcome, scientists and developers. Open Co-Scientist is an open-science
community project at [open-coscientist.com](https://open-coscientist.com).
Help can be a bug report, a research question, an evaluation dataset, a
reproduction, a review, documentation or code. You do not need to write code
to contribute.

Follow the [Code of Conduct](CODE_OF_CONDUCT.md). Ask questions in
[GitHub Discussions](https://github.com/guy915/Open-Co-Scientist/discussions).
Report vulnerabilities privately through [SECURITY.md](SECURITY.md).

## Pick an issue or propose a feature

Search [open issues](https://github.com/guy915/Open-Co-Scientist/issues) before
starting. The project labels are defined in [.github/labels.yml](.github/labels.yml).
Start with `good first issue` for bounded work with context, or `help wanted`
for work the maintainer has opened to contributors. Filter by area:
`area: engine`, `area: api`, `area: frontend`, `area: mcp`,
`area: evaluations`, `area: e2e`, `area: ci` or `area: docs`.
Types include `type: bug`, `type: feature`, `type: docs`, `type: maintenance`
and `type: question`. Priority labels range from `priority: critical` to
`priority: low`. Size labels estimate effort, from `size: XS` (under an
hour) to `size: XL` (split into sub-issues first). An issue marked
`needs triage` still needs its scope, reproduction, ownership or priority
reviewed.

Choose a small issue that matches your interests. Comment with your proposed
approach before starting. If the scope
is unclear, ask in the issue. This helps avoid overlapping work.

For a feature, use the feature request form from the
[issue chooser](https://github.com/guy915/Open-Co-Scientist/issues/new/choose).
Describe the problem, your proposal and alternatives. For a research change,
include the evidence, how to reproduce it and how success will be measured.
Discuss large changes before implementing them. Use Discussions for early
ideas that do not yet have a concrete proposal.

## Set up and run

Use Python 3.12, Node.js 22.13 or newer, and Bun 1.3.14.

```bash
git clone https://github.com/guy915/Open-Co-Scientist.git
cd Open-Co-Scientist
make setup
make start
```

`make start` runs the API on port 8008, the UI on 5173 and MCP on 8888.
It stops existing listeners on those ports. With no usable provider key, the
app returns a no-model error. Set `COSCIENTIST_TEST_DOUBLE=deterministic`
to select it explicitly. See [local setup](docs/RUNNING-LOCALLY.md) for details.
Keep credentials, local environment files and research outputs out of commits.

## Code layout

| Path | Contents | Area label |
| --- | --- | --- |
| `engine/src/co_scientist/` | The Python package: API server, durable runtime, agents and platform | `area: engine`, `area: api` |
| `engine/mcp_server/` | Reference MCP server for literature and database tools | `area: mcp` |
| `app/frontend/` | React workbench | `area: frontend` |
| `app/tests/` | API test suite | `area: api` |
| `evaluations/` | Offline evaluation harness, datasets and benchmarks | `area: evaluations` |
| `e2e/` | Playwright browser tests | `area: e2e` |
| `docs/` | Architecture, operations and decision records | `area: docs` |
| `.github/` | CI workflows, issue forms and repository settings | `area: ci` |
| `requirements/` | Hash-pinned runtime dependency locks | `area: ci` |
| `vendor/` | Third-party code shipped unmodified; never reformat it | — |

The package is layered `main` > `api` > `orchestration` > `science` >
`domains` > `platform` > `core`, and a layer imports only those below it;
`make arch` enforces this. Start with the [architecture](docs/ARCHITECTURE.md),
then read [AGENTS.md](AGENTS.md) and the guide beside the code you change
(`engine/AGENTS.md` or `app/AGENTS.md`).

## Checks and evidence

For code changes, run `make check` before requesting review. It covers lint,
types, backend and frontend suites, evaluation smoke, the frontend build and
both browser suites. CI tests must not need network access or provider keys.

For documentation changes, run `make lint` and
`.venv/bin/python -m pytest evaluations/tests -q` explicitly. Record each
command's exit status in the pull request. State any checks you could not run.
For repository-wide code or configuration changes, also run `make docker-build`.
Changes to model behavior should include benchmark scores before and after;
see the [quality benchmark](evaluations/README.md#quality-benchmark).

Follow the documentation policy in [AGENTS.md](AGENTS.md). Comments explain
hidden reasons or invariants, not what the code already says. Preserve
third-party notices and do not reformat vendored sources.

## Commits, branches and pull requests

Use commit messages in the form `<type>(<scope>): <subject>`, such as
`docs(community): explain how to contribute`. Types are `feat`, `fix`, `docs`,
`refactor`, `test` and `chore`. Branches use `<type>/<description>`, such as
`docs/community-contributing`.

Make one focused pull request against `main`. Use a short, standalone,
imperative title without a type prefix, such as `Explain how to contribute`.
The body states the problem, what changed, why and how it was tested. Link the
related issue and follow the pull request template. Include tradeoffs only
when they help review. Keep commits and pull requests free of tool attribution
and tool co-author trailers, as required by [AGENTS.md](AGENTS.md).

Preserve others' work and use new commits for follow-up changes. Wait for
review and green CI before merging. Accepted pull requests are squash-merged.

## License and decisions

Contributions use the [Apache License 2.0](LICENSE), including its contribution
terms in section 5. There is no separate contributor license agreement.
See [GOVERNANCE.md](GOVERNANCE.md) for project decisions and maintainer roles,
and [SUPPORT.md](SUPPORT.md) for help channels.
