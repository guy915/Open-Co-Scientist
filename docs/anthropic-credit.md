# Subscriber API credit

The operator slot uses `anthropic/claude-haiku-5-5` at the direct Messages API.
It is capacity between free OpenRouter routes and Azure. BYOK stays separate.
Routing admits the slot and separately admits each Azure fallback.
Operator runs stay Express; Standard and larger runs require BYOK.

Set `ANTHROPIC_API_KEY` on the API service only. Claim the provider credits
and link the Console organization **Open Co-Scientist**. The linked
organization cannot be changed in self-service. Create the production key there.
All keys and workspaces in that organization share the provider balance.

`ANTHROPIC_MONTHLY_CREDIT_USD` defaults to `100`.
`ANTHROPIC_BILLING_RESET_DAY` defaults to `7`; boundaries use UTC calendar dates.
Unused credit does not roll over. The application's remaining allowance is its own ledger value,
not a live Console balance. Other organization usage can exhaust credit sooner.

Admission includes real spend plus all outstanding reservations and stops at
95% of the configured credit. The ledger shares admission's short durable transaction
with global, client, host and app limits. Those limits are unchanged. Input uses
the planned model's free `count_tokens` endpoint and the existing byte bound.
The metadata request has a separate provider rate limit; include it in the live
check's request allocation. No writer covers a count or Messages request.

Reject a counted prompt above 100,000 tokens before sending Messages; Haiku
bills it at five times the input rate. Operator routing then sends that one call
to Luna, whose long tier starts at 272,000 tokens, and the next call returns to
Haiku. Without an admitted Azure slot the call has no provider. Counts are
provider estimates with small possible differences from billed usage. Reserve
at the higher price tier, with the full cache-write rate and output allowance;
settle the actual tier and cache buckets using saved prices. Inclusive prompt
usage contains ordinary input, cache reads and cache writes. Price each once.
Thinking is already part of output usage, not a second charge.
Settlement also records whether the provider refused the call, on both the
credit and Azure ledgers, for every physical call role; `NULL` means not measured.
The private spend view counts them per role. No prompt, header or error body is kept.

Every call uses effort low. A call reasons (adaptive thinking) where its role
reasons, and sends disabled thinking otherwise. Sampling parameters are omitted.
Per-call-role output ceilings are 8,192, 16,384 or 32,768 tokens. Short app text
calls (titles, restatements, announcements) ask for the thinking floor, so their
ceiling is the role's; an answer-sized budget ends in `output_cap`. An attempt cannot
raise these ceilings or select another model through an SDK alias or fallback.
The five-minute cache rules are in [Prompt caching](llm-caching.md).

The native low-credit error, HTTP 402 or a provider billing error disables only
the operator slot until the next reset. Do not retry it at that slot. Caller BYOK
errors cannot disable shared credit. Failed calls, missing priced usage and
interrupted streams keep their full reservations across restart and reset.
Settlement is atomic and idempotent. A settlement failure blocks further credit
calls rather than release unconfirmed spend. No credit row is erased with a run.

A refusal is billed from any returned usage and counted by call type. Routing
separately admits Azure for that call, records the switch and stops after Azure.
There is no server-side fallback. No measured refusal rate or cache saving is
available from hermetic tests. Effort changes only after paired quality
results by call type. Message Batches are not implemented. The provider offers
a 50% discount for offline batches; live runs never use them.

Sources checked 8 October 2026:

- [Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview)
- [Own token counts](https://platform.claude.com/docs/en/build-with-claude/token-counting)
- [Subscriber API credit](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)
- [Refusals](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback)
- [Message Batches](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
