# Prompt caching

Transport uses the existing gateway. BYOK uses caller keys. Express operator
order is free OpenRouter, Haiku 5.5 Console credit, then Azure. Durable credit
cooldown and slot selection are in place. Per-user limits stay.
Azure remains off unless the operator enables it, sets a positive total EUR
allowance and sets an unexpired credit date.

The prompt layer supplies a `CacheablePrompt` from `core/prompt_cache.py`: final rendered text and two
character offsets, `run_end` and `item_end`. Appending a schema preserves these offsets.
Templates and handwritten chat, supervisor, safety and claim builders place fixed
instructions first, shared run evidence next, then current item data and the question.
Changing evidence remains visible in the current item. Shared evidence baselines are
bounded to 512 entries of at most 16,000 characters each; committed run/data deletion
purges them. Hashed erased-run markers block late memoization; filling their 512-entry bound
purges all baselines and disables memoization until restart.
The gateway sends five-minute breakpoints at both boundaries on Haiku 5.5. Tool loops
and chat also use top-level automatic caching. A request may have at most four cache
breakpoints, including the automatic one. No delimiter from user text is interpreted.

Haiku 5.5 always sends low effort. It sends adaptive thinking, or disabled
thinking for the roles that do not reason. An engine recovery attempt keeps the
call's thinking mode, so the cached prefix stays valid; a chat retry after an
answer-less reasoning stream turns thinking off. Effort changes only after
paired quality results by call type. Temperature is omitted; top_p and
top_k are removed from both request arguments and extra body. Thinking shares max_tokens
with answer text. Read only text content blocks, including when thinking appears first.
The verified thinking parameter is allowed explicitly when the bundled SDK catalogue lags.
Signed thinking blocks survive engine and Q&A tool continuations. Stream fragments use
the pinned SDK assembler and stay in scoped memory.
Its USD/M rates through 100K input are input 0.10, output 0.50, read 0.01 and write 0.125.
Above 100K input they are 0.50, 2.50, 0.05 and 0.625. The gateway uses five-minute writes;
one-hour writes are not selected. Raw Claude input, read and write buckets are separate.
LiteLLM adds the latter two to prompt tokens; cost removes them before pricing each once.

Azure receives `prompt_cache_key` from run context and call role. A rolling 60-second
window starts a stable numbered partition after 15 requests per key. State uses a thread
lock, at most 4096 run/role entries and 256 partitions per entry; under larger bursts,
cache hits are best effort. No lock covers provider I/O. Calls without run context use
provider automatic caching. The Azure key API is covered by the installed SDK test;
cache-hit behavior still needs a live check.

Azure writes are an additional input charge. The durable EUR ledger already reserves
and records them. Missing priced write usage keeps the full reservation. The single total
cap, request-time expiry and kill switches still apply. General usage now records writes
and uses each model's published read/write rates. The ledger prices Luna at its short rates up to the published
272,000-token boundary and at its long rates above it. USD usage estimates do not replace that credit guard.

Private spans export numeric input, read and write counts plus a stable numeric call-type
ID and refusal flag. Cache keys and prompt text are excluded. Usage aggregates include
write and refusal counts. The SDK maps native refusal to content_filter; refused HTTP 200
responses are counted and rejected even when they contain text. Streams retain the verdict
across a later usage-only chunk. Routing (`platform/llm/routing.py`) separately admits Azure fallback and
records the switch. A refusal uses Azure for that call; exhausted operator
credit persists a cooldown until reset. Unknown stream outcomes retain their
charge and do not replay at another provider.

Console credit resets monthly. The low-credit error marks the operator slot unavailable
until reset and selects Azure. A caller's exhausted BYOK key must not disable the shared
slot. No measured refusal rate is available yet. The operator key goes in
ANTHROPIC_API_KEY on the API service only; see [Anthropic credit](anthropic-credit.md).

The cache becomes readable after the first response begins. A cold parallel wave may
miss on every call. Reuse a byte-identical prefix, let the first response begin, then send
the remaining calls without an extra warm-up request.

Boundary integration and the hermetic per-call-type input/prefix meter are implemented.
Hermetic SDK tests prove payload and accounting behavior; they do not measure a real
cache hit or claim live savings. No live measurement is recorded yet.

Sources checked 8 October 2026:

- [Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Claude thinking](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost)
- [Azure caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching?view=foundry-classic)
- [Azure prices](https://azure.microsoft.com/en-us/pricing/details/azure-openai/)
- [Cache-write billing](https://community.openai.com/t/how-are-reasoning-tokens-cached-tokens-input-tokens-and-output-tokens-counted-for-billing/1386849/3)
