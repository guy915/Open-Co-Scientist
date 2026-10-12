# Project documentation

| Guide | Contents |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | Layers, package map, persistence and request path |
| [Engine architecture](../engine/docs/ARCHITECTURE.md) | Agents, durable workflow and orchestration |
| [Glossary](GLOSSARY.md) | Domain terms used in module and type names |
| [Architecture decisions](adr/) | Module map, layering, durable runtime, LLM gateway, tracing |
| [Configuration](CONFIGURATION.md) | Environment controls, defaults and tooling |
| [Local setup](RUNNING-LOCALLY.md) | Toolchain, services and worktrees |
| [CI](CI.md) | Hermetic gates and local equivalents |
| [Deployment](DEPLOYMENT.md) | Hosting, networking and configuration |
| [Operations](OPERATIONS.md) | Persistence, provider and scientific safeguards |
| [Monitoring](MONITORING.md) | Uptime checks, error tracking and tracing |
| [Release readiness](LAUNCH.md) | Checks for each release, backups and repository settings |
| [Trusted proxy](TRUSTED-PROXY.md) | Visitor addresses behind the Railway edge |
| [Launch control](LAUNCH-CONTROL.md) | Operator pause, drain and visitor notice |
| [Incidents](INCIDENTS.md) | Incident playbooks |
| [Restore drill](RESTORE-DRILL.md) | Local Litestream backup and restore check |
| [Operator model credit setup](azure-setup.md) | Provider configuration, Azure allowance and spending cutoff |
| [Anthropic credit](anthropic-credit.md) | Subscriber credit, monthly admission and settlement |
| [Model call roles](llm-roles.md) | Reasoning policy, output bounds and provider dispatch |
| [Prompt caching](llm-caching.md) | Cache boundaries, provider accounting and measurement limits |
| [Evaluations](../evaluations/README.md) | Offline harness and live benchmark |
| [Dependencies](../requirements/README.md) | Hash-pinned runtime locks and audits |
| [Engine package](../engine/README.md) | Engine install and development |
| [LLM roles](llm-roles.md) | Model-call roles, defaults and effort overrides |
| [MCP server](../engine/mcp_server/README.md) | Reference literature and database tools |
| [Frontend](../app/frontend/README.md) | Workbench commands and source layout |
| [Contributor guidance](../AGENTS.md) | Repository rules and operating invariants |

Runtime prompt templates live beside the engine source in
`engine/src/co_scientist/science/prompts/templates/`.
