# Operator model credit setup

Order: BYOK → free OpenRouter → direct Haiku subscriber credit → Azure →
"No model is available right now". Operator runs are Express only; Standard and
higher tiers require BYOK. No Mistral slot remains.

## Portal steps

1. Claim the subscriber credit on claude.ai and link Console organization
   **Open Co-Scientist**. Confirm the organization before linking; self-service
   cannot change that link. Create workspace **Open Co-Scientist** and an API key
   there. Set `ANTHROPIC_API_KEY` on the API service only. Every key and workspace
   in the organization shares its credit.
2. In Azure AI Foundry, confirm the Global Standard deployment in Sweden Central:
   `coscientist-supervisor-luna`, gpt-6-luna `2026-09-22`. Every Azure role uses
   it. Use the resource HTTPS endpoint, not a deployment URL. Set the variables
   below on the API only.
3. Establish the spending cutoff ([below](#spending-cutoff)): read the credit
   lot's exact expiry and the usage already charged against it, then record the
   allowance. Azure has no hard cap for this offer; excess spend is billed to the
   subscription.
4. Size TPM/RPM for the capped requests and Express concurrency within that
   total. Azure counts each request's input plus its requested output cap. A
   preflight run measured an Express peak of about 264K TPM and 36 RPM across
   all roles, so give `coscientist-supervisor-luna` at least 300K TPM. TPM is a
   rate limit, not a cost; spend is bounded by the recorded allowance. Confirm
   quota before enabling Azure.
5. Open `/operations/spend` with the logs-admin token and test the kill switches
   and refusal path. Enable Azure only after the bounded live checks pass.

## API settings

| Setting | Value or default |
|---|---|
| `LLM_ENABLED` | `true`; false stops all model dispatch |
| `LLM_AZURE_ENABLED` | `false` until the operator enables it |
| `LLM_TOTAL_BUDGET_EUR` | unset = Azure off; never above the recorded allowance |
| `LLM_AZURE_EXPIRES_AT` | credit lot expiry as an ISO 8601 instant with offset |
| `LLM_AZURE_CUTOFF_HOURS` | `48`; stop this long before expiry, at least 1 |
| `LLM_USD_TO_EUR` | required, no default; never below the rate recorded with the allowance |
| `AZURE_OPENAI_API_KEY` | secret, API service only |
| `AZURE_OPENAI_ENDPOINT` | resource HTTPS origin `https://<resource>` + `.openai.azure.com`, `.services.ai.azure.com` or `.cognitiveservices.azure.com`; no path, port or query |
| `AZURE_OPENAI_DEPLOYMENT` | `coscientist-supervisor-luna` |
| `ANTHROPIC_MONTHLY_CREDIT_USD` | `100` |
| `ANTHROPIC_BILLING_RESET_DAY` | `7`; UTC boundary |
| `LLM_OPENROUTER_CALLS_PER_DAY` | `1000`; free route requests only |
| `LOGS_ADMIN_TOKEN` | secret for private logs, control and spend views |

There is no daily/monthly EUR budget, even spread or carry-forward. New funding
needs a new allowance version and a matching `LLM_TOTAL_BUDGET_EUR`; no code
change. Expiry still applies. `LLM_AZURE_UNTIL` is retired and ignored.

Both kill switches are read at each dispatch. Never raise per-user, host,
global, app or concurrency limits to make a model run finish.

The shared physical-call default is 1024/day; the free-route default is
1000/day. After 1000 free calls, only 24 shared calls remain for credit slots.
The Express forecast allows 57 calls before fallbacks. Choose a lower
`LLM_OPENROUTER_CALLS_PER_DAY` if credit slots need more headroom. Keep the shared ceiling and per-user limits unchanged.

A run estimate must fit the remaining total before Azure is offered. The
forecast is held, converted into actual call reservations, and released when
work ends. Real priced usage settles once after the reply. Missing usage,
failed calls and unknown outcomes keep the full reservation across restart.
No database writer spans network I/O. Known cache reads/writes use saved USD
prices and FX; reasoning is already part of output tokens.

The private view shows today/week UTC spend, total, unknown call holds, run
forecasts, remaining capacity, seven-day EUR/day and days at that rate. It also
shows subscriber USD spend, credit and the usable 95% allowance. These are local
ledger figures, not provider-balance queries. Free routes add no money spend.
There is no spend email or alert. Quota/reset failures go to the next slot;
a Haiku refusal goes to Azure for that call. Nothing follows Azure.

Keep `COSCIENTIST_TEST_DOUBLE` unset on public services. Deterministic responses
are available only through that explicit private test adapter. Missing provider
keys return an honest error.

## Spending cutoff

The API admits Azure only within one lifetime allowance recorded in its store
(`llm_azure_allowance`), shared by every Azure role, user, chat and engine
runs. Nothing resets it: not a month boundary, a restart, a redeploy or a raised
setting.

- **Allowance.** `grant − prior usage − buffer`, recorded by an operator. Prior
  usage is the actual cost already charged against the lot in Cost Management
  (including unbilled usage), not the invoice credit balance, which lags. Read it
  after Azure has been idle for at least 48 hours. The buffer absorbs ledger
  error, Cost Management lag and charges for anything else on the subscription
  until expiry. Every ledger row counts, including rows from before the record,
  so a later version can only be stricter about past spend. For a later
  version, enter as prior usage only cost the ledger does not already hold
  (Cost Management total minus `ledger_charged_and_reserved_eur`, if positive).
- **Effective limits.** The smaller of the setting and the recorded allowance,
  and the earlier of the two cutoffs. Lowering either takes effect at the next
  request. A version that raises the allowance, extends expiry or clears holds
  must name the active version in `supersedes_version`, and raising also needs
  the setting, so no single change reopens spend.
- **A store without a record refuses Azure.** A lost volume without a Litestream
  replica, a benchmark runner, or a laptop holding production keys starts with an
  empty ledger and no allowance, so it cannot spend.
- **Restores hold Azure.** A replica or backup can trail the ledger while
  keeping the allowance. The entrypoint records a hold whenever Litestream
  restores a database; after a manual restore, insert one yourself
  (`python -m co_scientist.platform.db.spend <db path>`). Re-baseline from Cost
  Management before acknowledging it.
- **Pricing.** The record stores the code's rates and the exchange rate the
  operator supplies as `usd_to_eur`; check the returned rates against the Azure
  price page. A request whose code price or `LLM_USD_TO_EUR` is lower than the
  recorded value is refused, and a record older than 183 days admits nothing
  until prices are rechecked and a new version is recorded. Microsoft bills this credit in euros from its own
  price list, which is not the USD price times the credit's displayed exchange
  rate; set `LLM_USD_TO_EUR` from the euro price sheet for these meters, or `1.00`
  if unverified.
- **Per request.** Each physical request, including every retry, reserves its
  upper cost before dispatch: input bytes of the actual Responses body plus
  framing allowances, the full output allowance (reasoning is within it), and the
  cache-write allowance, at Luna's short rates when that bound is at most
  272,000 tokens and at its long rates otherwise. Each replayed reasoning item adds
  the request's output cap, since its encrypted bytes do not bound the tokens
  it bills. Only text content, local function
  tools and replayed message, reasoning and function-call items are accepted. The
  native client sends only under a one-use permit for a stored reservation that
  covers that exact body and has not passed its cutoff; LiteLLM never reaches an
  Azure route.
- **Settlement.** Complete usage settles once. Usage outside the reserved bounds,
  or reasoning above output, keeps the charge and sets a durable hold that stops
  all Azure calls. Missing usage, timeouts, interrupted streams, provider
  rejections and crashes keep the full reservation. The ledger, holds and
  allowance history reject deletes, and a settled row cannot change.
- **Clearing a hold.** Holds are never deleted. Find the cause in the logs,
  read actual cost from Cost Management, then record a version with fresh prior
  usage, `"acknowledge_holds": true` and `supersedes_version`. Later holds stop
  Azure again.

### Recording the allowance

The operator token (`LOGS_ADMIN_TOKEN`) is required. `GET` shows the active
version and the ledger total; `POST` appends a version. Use the resource's real
API host.

```bash
curl -sS -X POST "https://$API_HOST/api/spend/azure-allowance" \
  -H "X-Logs-Token: $LOGS_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"grant_eur":"175.99","prior_usage_eur":"<from Cost Management>",
       "buffer_eur":"25","expires_at":"<lot expiry, e.g. 2027-01-04T00:00:00Z>",
       "cutoff_hours":"48","usd_to_eur":"1.00","note":"sponsorship lot"}'
```

Read the lot's expiry instant from the billing API (read-only); the portal shows
only a date. If only the date is known, assume the earliest instant it could
mean, 10:00 UTC on the previous day (the date's start at UTC+14).

```bash
az rest --method get --url "https://management.azure.com/providers/Microsoft.Billing/billingAccounts/<account>/billingProfiles/<profile>/providers/Microsoft.Consumption/lots?api-version=2023-03-01"
```

### What this does not guarantee

This is application protection. It limits what this API spends; it is not an
Azure-wide spending limit, which this offer does not support. Anything holding a
working credential, or the subscription owner using the portal or playground,
spends outside the ledger. Narrow those paths on the Azure side:

1. Keep the key only on the Railway `api` service. Delete Azure secrets from
   GitHub, local `.env` files and password managers you do not need, then
   regenerate both keys and set the new key 1 on the API. Regenerate key 2 again
   so no copy of it works.
2. Restrict the resource's network access to the API's egress addresses
   (Railway static outbound IPs, Pro plan). This blocks the playground and any
   leaked key from elsewhere. It needs a redeploy after enabling static IPs.
3. Lower each deployment's TPM to what Express needs. Quota limits the rate of
   any bypass, not its total.
4. Optionally disable key authentication (`disableLocalAuth`) and remove your
   own data-plane role; that blocks playground use but needs Entra ID tokens in
   the API, which it does not implement.
5. Keep the €150 budget alert as a lagging second signal; it does not stop
   spend.

## Azure runtime behavior

Operator Azure calls use the native `/openai/v1/responses` client with SDK
retries disabled. JSON output, streaming, local function results and encrypted
reasoning items preserve the gateway's completion contract; provider-hosted
paid tools are refused. Unknown paid outcomes are terminal, and Azure is the
last provider slot. Provider switches are recorded in provenance.

See [model call roles](llm-roles.md) for effort, output bounds and price tiers,
[prompt caching](llm-caching.md) for cache-write accounting, and
[operations](OPERATIONS.md#cost-accounting) for durable reservations and
dispatch checks.

## Validation status

Hermetic tests cover the native request contract, spending guard, admission
and settlement. Live quality and full-run cost on Azure are not yet measured.
Passing those tests does not establish deployment configuration, quota or live
model quality; confirm them for the intended release.

