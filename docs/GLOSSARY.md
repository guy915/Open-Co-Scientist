# Glossary

One line per domain term. Module, type and function names use these words with
these meanings. Persisted spellings (task types, node keys, table and column
names) are in backticks and never change without a migration.

## Science

| Term | Meaning |
|---|---|
| Research goal | The scientist's question a run works on; stored with its restated form and run title. |
| Hypothesis (idea) | One proposed answer to the goal. Rows in `hypotheses` are append-only; "idea" is the user-facing word. |
| Hypothesis state | The mutable projection of a hypothesis (Elo, wins, scores, status, cluster) in `hypothesis_state`. |
| Lineage | The parent links between hypotheses created by evolution; append-only. |
| Review | One assessment of a hypothesis (initial, full, comprehensive, meta-review, simulation); rows in `reviews`. |
| Reflection | The family of review nodes: initial reflection, review, comprehensive reflection, deep verification. |
| Deep verification | Probing questions that test a hypothesis's assumptions against evidence. |
| Evidence | A retrieved source (paper, abstract, page, attachment) stored in `evidence`; chunked into passages. |
| Passage | A chunk of evidence text a claim is assessed against. |
| Claim | An atomic statement extracted from a hypothesis. |
| Claim edge | A claim-to-evidence link with its entailment label, stored in `claim_evidence`. |
| Grounding | Assessing each claim against passages and storing the claim edges. |
| Citation | A hypothesis-to-source reference with a state: verified, partial, unsupported or unavailable. |
| Match | One pairwise tournament comparison between two hypotheses; rows in `matches`. |
| Tournament | The Elo ranking built from matches. |
| Elo | The ranking score each hypothesis carries; starts at the initial Elo constant. |
| Proximity | Similarity between hypotheses, used to cluster and deduplicate; `proximity_edges`. |
| Evolution | Producing new hypotheses from selected parents. |
| Meta-review | The cross-review synthesis that feeds evolution and the research overview. |
| Research overview | The terminal synthesis and roadmap that becomes the report's core. |
| Direction | A research direction proposed in the overview or by the scientist. |
| Supervisor | The agent that plans a run and decides the next task each cycle (the orchestrator node). |
| Agent | A science module that turns typed input into typed output with one or more model calls. |
| Research state | Everything above for one run: the world model. Domain `research_state` owns it. |
| Safety screen | Deterministic rules plus an optional model assessment; at intake, per hypothesis, and at the final gate. |
| Hold | A safety decision that withholds a hypothesis until it is resolved. |
| Report | The published result of a run: structured payload plus Markdown; append-only. |
| Gate | A check that decides whether something may be published (evidence gate, claim gate, final safety gate). |

## Runs and execution

| Term | Meaning |
|---|---|
| Run | One attempt at a research goal, from creation to a terminal status (`runs`). |
| Run status | `draft`, `queued`, `running`, `synthesizing`, `completed`, `cancelled`, `failed`, `blocked`, `paused`. |
| Tier | The run size: `express`, `standard`, `extended`, `ultra`; sets counts and call ceilings. |
| Focus | The evidence-versus-novelty preference: `prefer_evidence`, `balance`, `prefer_novelty`, `breakthrough`. |
| Run config | The resolved tier, focus, criteria and limits a run executes with. |
| Cycle | One pass of review, ranking and supervisor decision; a run repeats cycles until it terminates. |
| Node | One named step of the workflow (generate, review, ranking, ...). Node keys are persisted. |
| Workflow | The fixed pipeline of nodes and their successor routes (`workflow_topology`). |
| Workflow state | The in-memory research state a node reads and updates (`WorkflowState`); checkpointed as JSON. |
| Checkpoint | A saved workflow state after a committed task, the resume point (`checkpoints`). |
| Task | One durable, leased, idempotent unit of work in `scientific_tasks` (a node, a fan-out item, an aggregate, a match). |
| Task type | The persisted kind of a task: `engine.bootstrap`, `engine.node.<key>`, `engine.fanout.*`, `engine.ranking.*`, `engine.finalize`, `notification.email`. |
| Fan-out | Splitting one phase into item tasks plus an aggregate task that merges their results. |
| Portfolio | The tasks the supervisor schedules ahead within one cycle. |
| Durable runtime | The task queue, leases, retries, parking and recovery that make runs survive restarts. |
| Worker cohort | The per-run async worker that claims and runs that run's tasks inside the API process, on its own event loop. |
| Lease | A time-limited claim on a task; an expired lease returns the task for recovery. |
| Park | Deferring a task to a future due time, for example after a rate limit or daily cap. |
| Settlement | Moving a run to its terminal status after its last task. |
| Drain | Projecting a committed workflow state into the research-state tables. |
| Steering | Input the scientist sends to a running run; consumed on observation, one extra cycle allowed. |
| Finalize | The terminal task that grounds claims, screens, gates and publishes the report. |
| Event | An append-only row in `run_events`, the canonical timeline replayed over SSE. |
| Milestone | A chat message announcing a node's progress; not an event. |

## Chat and documents

| Term | Meaning |
|---|---|
| Chat | The scientist's conversation surface; owns interviews, Q&A, announcements and messages. |
| Interview | The scoping conversation before a run that fills its goal and setup. |
| Q&A | Run-scoped questions answered from the research state. |
| Announcement | The streamed message that opens a started run. |
| Example | A curated demo run copied into a visitor's own chat on open. |
| Attachment (staged document) | A file the scientist uploads; extracted, chunked and later admitted as evidence. |

## Models and access

| Term | Meaning |
|---|---|
| Gateway | The single interface through which every model call goes (`platform/llm`). |
| Model profile | Every fact about one model: capabilities, route, fallbacks, price (`ModelProfile`). |
| Route | The provider path a model call takes; a free route has zero price. |
| Fallback | The next route tried when one fails or is unavailable. |
| Admission | Deciding whether a call may be sent: budgets, zero-price rules, daily caps. |
| Budget | A ceiling on calls, tokens or reasoning for a run, a surface or one call. |
| Escalation | Retrying a call with a larger token or reasoning budget, within bounds. |
| Tool loop | A model call that may call tools repeatedly before answering, with a transcript budget. |
| Offline backend | The deterministic, schema-valid stand-in for real model calls. |
| Surface | A non-run caller of the gateway (chat, interview, Q&A) with its own call cap. |
| BYOK | Bring your own key: a scientist's provider credential, encrypted per run. |
| Free allowance | The daily number of free runs per owner, per host and in total. |
| Owner / principal | The client identity that owns runs and chats. |
| Retrieval | Literature and web search through the MCP server, fused and ranked. |
| MCP server | The separate literature-tool service; shares only its wire contract with the app. |
| Sandbox / workspace | The confined directory and command runner for model-written programs and skills. |
| Skill | A vendored science script the drafting agent may run inside the sandbox. |
