# Exception Keeper model stage

This stage is implemented and verified with scripted model responses and a socket-free
HTTP transport double. The loopback HTTP server tests are included but **could not run
in this sandbox**: binding `127.0.0.1` failed with `Operation not permitted`. No remote
API, real model, browser, installation, or git command was used.

Observed final suite result with `.venv/bin/python -m pytest -q -rs`:

```text
133 passed, 18 skipped in 0.93s
SKIPPED [18]: Loopback bind failed: [Errno 1] Operation not permitted
```

All original 48 tests are included in that result. The three requested mock CLI
commands completed with no stopped reason and saved mock reports under
`samples/precomputed/`.

## Design and operation

`sheetaudit.review.review(path, client, budget, ...)` keeps deterministic detection
separate from model judgment. Nano proposes structure, Super reviews each candidate,
and Ultra orders the resulting findings. `impact`, saved values, and all other engine
evidence are copied from the engine; no model-supplied impact is accepted. Every
stage's prompt says not to compute numbers.

- `llm/models.py`: exact catalog identifiers, URL, and USD prices per million tokens.
- `llm/client.py`: interchangeable scripted and standard-library HTTP clients.
  Credentials are read at call time, never retained on the client, and never included
  in request records. Redirects are disabled. Only 429 and 5xx are retried. Retry
  waits support seconds or HTTP dates, are capped at two seconds, and share the
  total `timeout_s` time budget. Provider error bodies are discarded.
- `llm/jsonout.py`: `schema_style="openai"` by default, overridden by
  `EK_SCHEMA_STYLE` or an explicit argument. `nebius_doc`, `openai`, `json_object`,
  and `none` are supported. The schema is also in the prompt for modes without a
  wire schema. Closed think blocks are removed, unfinished think text is discarded,
  and the separate `reasoning_content` field is ignored. Parsing accepts JSON objects
  inside fences or prose. Validation errors get one repair call; a second failure
  becomes a report record rather than terminating the run.
- `review/structure.py`: at most 6,000 characters of sheet dimensions, initial text,
  sampled A-column labels, notes headers and aggregate-row addresses. Out-of-bounds
  model suggestions are dropped with warnings. This is a heuristic for small tables,
  not complete workbook understanding.
- `review/tools.py`: validated local A1 ranges, at most 60 returned cells, row notes
  with their header, at most 10 rule-sheet text hits, and at most 20 majority-pattern
  neighbours in a contiguous formula run. `json_only` runs these four lookups in code
  first (the range lookup reads the candidate cell). It needs no model tool calls.
  Tool mode permits four model rounds and eight tool calls per round; one final
  invalid-JSON repair, if needed, has tools disabled. Reaching four tool rounds
  without an answer produces `ask`.
- `review/judge.py`: quotes must match existing cell data or supplied candidate
  label/header/note context, ignoring case and whitespace. An intended exception
  additionally needs a quote in its actual notes cell, its cell comment, or an
  identified rules sheet. Invalid grounding changes the verdict to `ask`, preserves
  `original_verdict`, records reasons, and drops any suggested fix. Other fixes must
  target existing cells, begin with `=`, and reference only known sheets. Unsupported
  names, external/3D references and dynamic INDIRECT/OFFSET targets are conservatively
  rejected. This does not prove the remaining formula is correct; no formulas execute.
- `review/summary.py`: unknown/duplicate IDs are removed from order and conflicts;
  a conflict with fewer than two remaining IDs is removed.
- `review/pipeline.py`: stage failures preserve engine findings and produce fallback
  decisions. Candidate limits and budget limits have explicit unreviewed records.
  Budget exhaustion points to precomputed sample results. Requests contain selected
  outline/context and bounded tool outputs, not a serialized workbook. Inputs are
  never saved or modified.

## Spending limits and funding boundary

`Budget` uses an injectable aware clock and Asia/Tokyo calendar days. It enforces the
Order 03 $0.15 run cap (which can only be lowered), $7 build-period cap, $0.15 November
daily cap across lanes, December judge/public daily caps of $0.60/$0.20, and $45 global
cap across both `ek` and `pw` entries. Calls outside the dated periods are refused.

Before every initial call, tool follow-up, or repair, admission reserves an estimated
prompt (serialized characters / 3, rounded up, plus 64 framing tokens) and the output
token ceiling. This includes tool definitions and response schemas. HTTP admission
also allows for every possible retry. Reported usage determines recorded cost;
cached input discounts apply only when explicit cached-token usage is returned.
Missing usage is estimated from request/response characters. Failed HTTP attempts
with unknown usage use the admitted ceiling, conservatively including unused retries
when a final error occurs. Reports mark these costs estimated. Observed overages
stop subsequent calls; token estimates are not guarantees of provider billing.

`MemoryLedger` is for isolated tests/mocks. `FileLedger` appends JSON lines under a
thread lock and `fcntl` file lock, flushing before unlock. Admission, the request and
accounting share that lock. Malformed ledgers fail closed. Live CLI mode always uses
a durable ledger; **both apps must use the same ledger path** for the global cap to
include both. A separate ledger is a separate accounting universe. A process killed
after provider acceptance but before recording can leave unrecorded spend; reconcile
provider usage before resuming after such an interruption. This local ledger is not
a billing service or a crash-proof payment reservation system.

The owner's permitted cash spending remains **$0**. These limits are allowances for
granted credits only, not permission to charge a card. This implementation cannot
identify whether an account balance is credits or cash, inspect credit expiration,
or disable provider auto-payment. Before enabling live calls, verify credits cover
the allowance and prevent cash billing in the provider account. Those conditions
have not been verified because there is no key. Never infer funding from a key's
presence or a successful HTTP response.

## Commands and fixtures

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q -rs tests/test_model_stage.py
.venv/bin/python -m sheetaudit.review samples/club_membership_fees.xlsx --mock tests/fixtures/mock_scripts/club_membership_fees.json
.venv/bin/python -m sheetaudit.review samples/small_business_budget.xlsx --mock tests/fixtures/mock_scripts/small_business_budget.json
.venv/bin/python -m sheetaudit.review samples/event_budget.xlsx --mock tests/fixtures/mock_scripts/event_budget.json
```

Optional CLI controls: `--lane public|judge`, `--schema-style`, `--judge-mode tools|json_only`,
`--ledger PATH`, and `--out PATH`. `--live` is opt-in, reads `NEBIUS_API_KEY`, and refuses
an absent key. It was not invoked. Production base URL changes belong on
`HttpChatClient(base_url=...)`; no provider SDK is used.

The three normal mock scripts agree with `samples/ground_truth.json`; the additional
unplanted Fee Rules!B3 assumption is also reviewed. Their quotes come from the actual
sample cells. `grounding_failures.json` deliberately fabricates a Members!C6 quote
and labels Members!C26 an intended exception without note evidence. Both downgrade
to `ask`. Each workbook's intended exception also exercises a `find_notes` round trip.
`samples/precomputed/*.json` are explicitly marked `"source": "mock"`.

Coverage includes HTTP wire construction in a socket-free transport double, all four
schema formats, retries/errors, reasoning/fenced output, JSON repair, tool responses,
secret redaction, grounding, invalid fixes, privacy sentinel, budget dates/caps and
shared ledger persistence/concurrent admission. The actual loopback suite separately
checks `POST /v1/chat/completions`, Authorization, model IDs, schema shapes, rate-limit
headers, 429 recovery, 500 exhaustion, repair, and the complete club sample. Only bind
failure triggers a skip; handler/assertion failures do not. Mock and transport/stub
success say **nothing about real model accuracy**.

## First live checklist: at most six calls

Do not run this until credentials and the credit-only funding conditions above are
verified. Use the shared budget for each probe, `max_retries=0`, `max_tokens=256`,
small synthetic data (estimated input at most 1,500 tokens per probe), and no repair
retry. Record schema validation outcomes and all returned rate-limit headers. Stop
on a funding/budget issue; count failed attempts toward the six-call ceiling.

1. Nano, `schema_style=nebius_doc`, tiny sheet-role object.
2. Nano, `schema_style=openai`, the same tiny object.
3. Nano, `schema_style=json_object`, the same tiny object.
4. Super, `judge_mode=tools`, request one `find_notes` call. Inspect its name and
   argument shape; this one-call probe does not verify the full tool round trip.
5. Super, `judge_mode=json_only`, supply a known note and ask for final judgment.
6. Ultra, compact synthetic verdicts and a short summary schema.

At the supplied uncached catalog prices these ceilings total about **$0.0041**
(3 Nano + 2 Super + 1 Ultra calls), below $0.05. Actual usage/pricing remains unverified.
There are four switch values but only three wire-format variants: `none` omits
`response_format`. Testing every switch value plus the two Super modes and Ultra
would require seven calls. To retain the six-call ceiling, substitute `none` for one
Nano probe if needed; do not add a seventh call. This is the explicit resolution of
the order's conflicting checklist counts.

Until live probes run, unknowns are: the accepted schema wrapper per model, whether
any of these models supports JSON mode, reliable Nemotron tool emission versus the
`json_only` fallback, actual reasoning fields, real token usage/cost, rate-limit
behaviour, credit-only account configuration, and real judgment quality.

## Sources and API evidence

Provider facts below are the official-document excerpts supplied in Order 03,
fetched by the requester on 2026-10-24. They were not re-fetched during this job.

- Base URL, endpoint and bearer header: [Token Factory quickstart](https://docs.tokenfactory.nebius.com/quickstart.md).
- Exact model IDs and per-token prices: [Token Factory model catalog](https://tokenfactory.nebius.com/model-catalog.md).
- The provider example puts the schema directly under `json_schema`; the alternate
  OpenAI wrapper remains a compatibility hypothesis here:
  [Structured output](https://docs.tokenfactory.nebius.com/ai-models-inference/json.md).
- The supplied docs demonstrate `tools` and `tool_choice` with Llama, which does not
  establish Nemotron support: [Function calling](https://docs.tokenfactory.nebius.com/ai-models-inference/function-calling.md).
- The documented over-limit response is 429 and uses the three `x-ratelimit-*`
  headers preserved by this adapter: [Rate limits](https://docs.tokenfactory.nebius.com/ai-models-inference/rate-limits.md).

Local source checks, with no dependency installation:

- Python 3.14's `OpenerDirector.open` accepts a `Request` and `timeout`; the redirect
  hook documents its return contract. Source:
  [installed urllib.request](/opt/homebrew/Cellar/python@3.14/3.14.8_2/Frameworks/Python.framework/Versions/3.14/lib/python3.14/urllib/request.py).
  Relevant excerpt:

  ```python
  def redirect_request(self, req, fp, code, msg, headers, newurl):
      """Return a Request or None in response to a redirect.
  ```

- The loopback fixture uses the actual `HTTPServer.server_bind` implementation,
  which delegates to `socketserver.TCPServer.server_bind`. Source:
  [installed http.server](/opt/homebrew/Cellar/python@3.14/3.14.8_2/Frameworks/Python.framework/Versions/3.14/lib/python3.14/http/server.py).
- Workbook saved-value semantics and A1 formula tokenization remain the existing
  engine contract, with local library sources in [ENGINE_NOTES.md](ENGINE_NOTES.md).

## Acceptance outside the sandbox (2026-10-24 01:5x JST, reviewer)

- The 18 loopback tests that were skipped in the worker's sandbox ran on the host: 2 of
  them failed because the stub client's 2 s total time budget left no room for the
  capped 2 s retry wait. The two tests now give the client a 10 s budget; the client
  behaviour (refuse a wait that does not fit the total budget) is unchanged.
- `precomputed_sample` was resolved against the working directory; it is now resolved
  against the project root.
- Grounding: label/header/note context now counts only when the model cites the
  candidate cell itself. Before, a quote from the candidate's note cited at an unrelated
  existing cell (e.g. `Members!B2`) passed as grounded. A regression test was added.
- Live mode's default ledger is now one file shared by both apps:
  `$X129_LEDGER_PATH`, else `~/.x129/ledger.jsonl` (was `./.exception_keeper_ledger.jsonl`).
- Result: `152 passed` from the project root and from outside it, with no skips. The three
  mock CLI runs reproduce `samples/precomputed/*.json` exactly. `--live` without
  `NEBIUS_API_KEY` exits with code 2 and a clear message.

## Order 05 — staged API, sessions and web preview

This section supersedes the single-invocation flow and per-run accounting described
above for the web API. The deterministic engine, judgment grounding and summary
validation remain shared with `review()`. No dependency, download, remote call,
browser tool, git operation or delegation was used for this order.

### API and client contract

`sheetaudit/api.py` exposes `handle(request, env, deps) -> (status, body)`.
`Dependencies` supplies the ledger, chat client and aware clock; the handler does
not acquire credentials, choose a file path, save uploads or create network clients.
`serve.py` provides the local transport at `127.0.0.1:4174` and serves `web/` plus
allowlisted sample assets. `sheetaudit/http.py` bounds JSON request bodies and sends
generic JSON errors without request logging. Credentials are read at call time;
the API redacts the environment secrets and supplied passphrase from responses.

Every `POST /api/ek` sends `workbook_b64` again:

- `start`: send `{stage: "start", workbook_b64, passphrase?}`. Validation precedes
  model use. Run the engine and Nano; return a UUID session, validated `outline`,
  all engine `candidates`, sheet dimensions, and `max_candidates: 12`.
- `judge`: send the session and `payload: {candidate_id, outline?}`. Run the engine
  again and judge only that candidate with Super. The returned `decision` contains
  authoritative engine fields plus the grounded model decision. Tool/repair rounds
  remain inside this single candidate's invocation.
- `summary`: send the session and `payload: {decisions, outline?}`. `decisions` is
  the array of complete, unchanged `decision` objects collected from successful
  judge responses. The fresh engine owns evidence and impact. Ultra orders findings;
  candidates beyond the reviewed set remain explicitly not reviewed.

The web client sends the returned Nano outline in every later payload. Its hash must
match start. Omission is allowed for minimal callers and uses an empty, conservative
outline; this can downgrade exceptions whose only evidence is on a rules sheet.
No additional Nano call occurs in judge or summary. Summary requires exactly the
successfully collected decisions, including partial sessions; it never accepts
fabricated, edited, duplicate or unknown decisions. Hashes of decisions protect this
contract without retaining their text. Saved date values serialize to strings.

Each successful response includes `source`, `model`, `lane`, `session_cost_usd`,
`remaining_session_usd`, warnings and a per-invocation `cost` usage report. Mock
costs are simulated accounting values; the sample UI always displays actual cost $0.
Errors use `bad_request` (400), `too_large` (413), `unsupported_file` (415), `budget`
(429), or a generic `upstream_error` (502). Provider/ledger exception text is not
returned. The local server does not log request paths or bodies.

### Ledger session rules and spending

Sessions are server-issued UUIDs. A later call needs a successful start, the same
passphrase-derived lane, and the same workbook hash. Correct passphrase selects
`judge`; missing or incorrect passphrase selects `public`. Comparison always uses
fixed-size SHA-256 digests. A pending started marker refuses overlapping calls.
No judge may follow summary; duplicate candidate attempts are refused. There are
at most 12 judge attempts and one summary attempt, including failed attempts.
With candidates present, at least one successful judge is required before summary.
An empty workbook may go directly from start to summary.

The ledger retains only accounting, stage markers, candidate identifiers and hashes;
no workbook bytes, engine context, outline text or decision text is stored. Records
include `app`, `lane`, `day`, `amount`, `session`, `stage`, and `event`:
`started` and `finished` have zero amount; `usage` records actual/estimated charge.
Start/finish success is distinct from model usage. Older records, including
PatchWitness's zero-cost started/finished records and usage records without event,
continue to count by amount. Session/order filtering includes `app == "ek"`.

`Budget` now computes the $0.15 session cap from the ledger even across separate
handler/Budget instances. A standalone Budget receives a fresh UUID unless given
one explicitly; `run_spent` remains only the current instance's reporting total.
The historical `run_cap` argument can lower the session cap, and its legacy error
label remains `Per-run` when no explicit session was supplied, preserving existing
callers/tests. Other dated caps and the shared $45 cap are unchanged. A new cap
requires **total recorded spending for both apps <= granted credits**, across dates.

Unless both `EK_LIVE=1` and positive finite `EK_GRANTED_CREDITS_USD` are configured,
each API model stage returns exactly this refusal (after valid input/order checks):

```json
{"error":"budget","message":"Live calls are disabled. Use the sample results (cost $0)."}
```

The original CLI's live entry now enforces this opt-in too. Mock clients do not
implicitly bypass the API gate; tests inject fake live configuration and a scripted
client, making no real calls. The adapters use 10-second per-chat time budgets and
no HTTP retries. These settings are not proof of fitting a deployment's invocation
limit; a candidate can still use multiple tool/repair calls.

`MemoryLedger` and `FileLedger` keep the injectable interface. FileLedger uses both
its existing `fcntl` lock and PatchWitness's adjacent exclusive `.lock` file protocol,
with token ownership and a one-second lock wait. Python does not automatically
reclaim stale lock files. The deployment store is deliberately undecided. Both apps
must use one shared, consistently locked store; otherwise the combined cap cannot
be enforced. This is still not crash-proof billing: reconcile interrupted provider
calls before resuming, and do not use this preview ledger as a deployment decision.

### Upload and UI limits

Decoded uploads are at most 1,000,000 bytes; request JSON is at most 1,600,000 bytes.
Before workbook loading, the ZIP directory is checked for at most 200 members and
at most 20,000,000 total declared uncompressed bytes. Ordinary `.xlsx` content types
and workbook members are required; macro-enabled packages are rejected. The engine's
existing 200,000-cell rectangle limit per sheet remains enforced. Invalid base64 is
bad request; non-ZIP/unsupported workbooks are unsupported file; size limits are too
large. Uploaded bytes use `BytesIO`, with no temporary file or workbook persistence.
ZIP metadata validation is a bounded small-workbook guard, not a general archive
security proof.

The English UI loads the club sample by default. Each of three sample buttons loads
the original precomputed JSON, visibly labelled **sample result — scripted, not a
live model run**, cost $0. The live flow preserves collected findings on refusal,
shows the accumulated session cost and links back to samples. Findings follow the
summary order and show formula, majority R1C1, saved value, engine impact, model
reason/name, quote/cell grounding, optional fixes and questions. Grounding failures
are labelled; model impact is never used. There is no corrected-workbook export.

The cell map uses verdict symbols and text labels alongside colour. It represents
the sheet's used rectangle, with keyboard-focusable cells and row/column labels.
All three samples fit in full; larger sheets use pages of at most 40 rows by 12
columns to bound DOM work. Markdown copy includes a selectable-text fallback;
CSV download escapes formula-like text. Assets and module requests use relative
URLs only. System fonts, light/dark palettes, visible focus and reduced-motion
styles need no downloads. Layout self-review tightened mobile evidence into one
column, bounded cell-map rendering and preserved sample/live provenance at the top.
An offline calculation of the ten text/background palette pairs found minimum
contrast ratios of 5.71 in light mode and 6.47 in dark mode. This checks colour
tokens, not rendered typography, layout or assistive-technology behaviour.
No visual-browser, assistive-technology or real-model quality claim is made.

`web/dev-smoke.html` uses the same `renderReport()` and `loadSample()` as the main
page. Its `pre#out` records actual finding DOM counts, verdict counts, map presence,
and captured console/window errors for the reviewer's headless-browser check.

### Offline source excerpts and unverified deployment

In-memory ZIP loading uses the installed Python ZIP interface:

```text
file: Either the path to the file, or a file-like object.
```
Source: [installed zipfile, ZipFile docstring](/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/lib/python3.14/zipfile/__init__.py:1423).

The installed workbook reader accepts a binary stream:

```text
:type filename: string or a file-like object open in binary mode
```
Source: [installed openpyxl, load_workbook docstring](.venv/lib/python3.14/site-packages/openpyxl/reader/excel.py:322).

The local server follows the installed runtime's dispatch convention:

```python
mname = 'do_' + self.command
```
Source: [installed http.server, BaseHTTPRequestHandler](/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/lib/python3.14/http/server.py).

The fixed-length comparison uses the installed `hmac.compare_digest` contract:

```text
This function uses an approach designed to prevent
timing analysis, making it appropriate for cryptography.
```
Source: installed Python 3.14 `hmac.compare_digest.__doc__`, inspected offline.

The read-only architectural reference is PatchWitness's `server/api.mjs` and
`server/budget.mjs`: injected handler dependencies, server UUID, SHA-256 lane
comparison, started/finished markers, amount-based shared-credit accounting and
the `.lock` protocol. No sister-app file was changed.

The Order 05 `api/ek.py` BaseHTTPRequestHandler candidate was unverified and used
a local-file placeholder. Order 07 removes that wrapper and replaces it with the
root WSGI entrypoint and a Redis ledger described below. A local file path alone
does not establish a durable deployment store. No deployment was attempted in
Order 05; model schema/tool support, billing and judgment quality at that stage
were unverified.

### Order 05 verification

The existing 152 tests were not edited or weakened. Four mock fixture files gained
explicit stage and candidate keys on their existing response entries. Legacy
unkeyed response lists remain supported for isolated transport and repair tests.
The pipeline and API select independent cursors by `(stage, candidate_id)`; tools
and repairs remain ordered within that key. Precomputed JSON files were unchanged.

The split-flow regression compares every finding field and the entire summary for
all three samples, including grounding, impact and order. New checks cover upload
refusals, stage/lane/workbook binding, duplicate/twelve-attempt limits, summary
tampering, session/credit caps across handlers, FileLedger markers, concurrent
admission, credential redaction, relative web references and the full local-server
flow with an injected mock client.

Observed here with the final `.venv/bin/python -m pytest -q`:

```text
194 passed, 19 skipped in 1.25s
```

All 19 skips have the same sole reason: loopback bind failed with
`[Errno 1] Operation not permitted`. They comprise the original 18 model-transport
tests plus `test_local_server_full_sample_with_mock_client`. The explicit
`.venv/bin/python serve.py` attempt failed at that same bind; no server was left
running. The pure-handler sample flow passed. The page was not viewed in a browser.
The socket-free JSON transport full-sample test also passed. A 13-candidate workbook
test verifies that exactly twelve candidates can be judged and the tail remains
not reviewed. The API's `json_only` switch and keyed repair path are covered.

Skipped cases (all have the bind failure above):

| Test | Cases |
| --- | ---: |
| `tests/test_api.py::test_local_server_full_sample_with_mock_client` | 1 |
| `tests/test_model_stage.py::test_http_schema_styles` | 4 |
| `tests/test_model_stage.py::test_retry_429_then_success` | 1 |
| `tests/test_model_stage.py::test_retry_after_date_or_malformed` | 2 |
| `tests/test_model_stage.py::test_500_twice_then_error` | 1 |
| `tests/test_model_stage.py::test_other_4xx_never_retry` | 4 |
| `tests/test_model_stage.py::test_total_time_budget_refuses_retry_wait` | 1 |
| `tests/test_model_stage.py::test_secret_echo_is_redacted` | 1 |
| `tests/test_model_stage.py::test_http_one_repair_retry` | 3 |
| `tests/test_model_stage.py::test_full_http_pipeline_tool_roundtrip_and_downgrades` | 1 |

## Acceptance of the API and web app (2026-10-24 02:5x JST, reviewer)

- `213 passed` from the project root and from outside it, with no skips (the 19 loopback tests skipped in the worker's sandbox ran on the host).
- Headless Chrome over CDP (a temporary profile, not the owner's browser) on `serve.py`: `web/dev-smoke.html` rendered all three samples with verdict counts equal to `samples/ground_truth.json` (club 3 error / 1 exception / 1 ask / 1 assumption; budget 3 error / 2 assumption; event 1 error / 1 exception), cell maps rendered, no console errors. `index.html` has no horizontal scroll at 375 px and 1280 px.
- Reviewer probes on the pure handler: live disabled by default (429 `budget`); not a zip → 415; a 30 MB-uncompressed zip → 413; a 1.5 MB body → 413; judge before start → 400; correct passphrase → lane `judge`, wrong → `public`; neither the passphrase nor the key appears in any response.
- Before submission: the precomputed sample results are scripted mock output, yet each finding card shows "Decision: Nemotron Super". Replace them with live results (or label each card as scripted) before the demo is public.

## Live verification (2026-10-24)

The key arrived on 2026-10-24 and the live checks above were run against Token Factory; the earlier "no key yet" and "unknown until live" notes are historical.
Results, run-to-run spread, the schema-style decision and the limits that changed are in the README section "Recorded live results (2026-10-24)".

## Order 07 — WSGI entrypoint and shared Redis ledger (2026-10-28)

This section supersedes the undecided deployment store and old deployment wrapper
above. Root `app.py` exports `app(environ, start_response)`. It adapts the existing
`sheetaudit.http.post` transport to WSGI, preserving the request size checks and JSON
responses, and serves `web/` plus the three allowlisted workbook/report pairs.
Static paths reject traversal and symlinks outside the allowed directory.
`serve.py` and all pre-existing tests are unchanged. `api/ek.py` is deleted.

`RedisLedger` implements the same accounting interface and JSON-object record format
as `FileLedger`. Required fields remain `app`, `lane`, `day`, `amount`; amounts must
be finite and nonnegative. Started/finished markers and optional usage details,
including `estimated`, remain compatible with PatchWitness. Every amount across
both apps contributes to the global $45 and granted-credit caps.

Configuration reads `UPSTASH_REDIS_REST_URL` with `KV_REST_API_URL` as fallback and
`UPSTASH_REDIS_REST_TOKEN` with `KV_REST_API_TOKEN` as fallback. URL and prefix are
selected at construction; the token is resolved from the environment for each
HTTP call and is never retained on the ledger. `X129_LEDGER_PREFIX` defaults to
`x129:ledger`; keys are `<prefix>:records` and `<prefix>:lock`. Both apps must share
these keys and the compare-and-delete locking protocol. PatchWitness is a separate
repository and was not modified or verified here.

The reentrant process lock wraps a random-owner `SET key value NX PX 120000`,
waiting at most 100 seconds and polling every 0.5 seconds. Each outer acquisition
refreshes the record list with one `LRANGE key 0 -1`; nested reads use that list.
Appends use `RPUSH key value`; release uses `EVAL script 1 key owner` and deletes
only a matching owner. A lost/expired lease, invalid entry, malformed response,
Redis error or lock timeout refuses further calls. Local lease checks prevent
known-expired owners from appending; they do not make interrupted billing crash-proof.

The lock spans budget admission through model usage recording, with the same
semantics as the file lock. Concurrent live sessions are serialised during each
model call; this is intended at hackathon traffic. Stage/order markers also use
the shared lock, as before. A process interrupted after a provider charge still
requires reconciliation before resuming. The 120-second lease is not renewed.

The deployment selects Redis only when both URL and token are present. With
`EK_LIVE=1` and no Redis pair, it returns the previous wrapper's 503 `budget`
message, `Shared durable ledger is not configured.` A file path never enables
deployment live calls. Without live opt-in, the existing disabled-live response
remains. The deployed chat client uses `timeout_s=55, max_retries=0`; tool/repair
rounds may still require multiple chat calls in one stage.

`requirements.txt` contains only `openpyxl==3.1.5`. Installed package metadata was
read with `importlib.metadata.version`: `openpyxl` reported `3.1.5`, and
`xlsxwriter` reported `3.3.0`. An import scan found XlsxWriter only in
`samples/make_samples.py` and tests, not the runtime import graph, so it is omitted.
No package installation or download was performed.

### Sources and deployment uncertainties

The installed standard-library WSGI handler calls an application with these two
arguments; the new callable follows that contract:

```python
self.result = application(self.environ, self.start_response)
```
Source: installed Python 3.14 `wsgiref.handlers.BaseHandler.run`, inspected offline
with `inspect.getsource` on 2026-10-28. `urllib.request.Request`'s installed signature
includes `data`, `headers`, and `method`; `OpenerDirector.open` includes `timeout`.
Both signatures were inspected offline. This establishes the local Python API,
not Vercel's detection or hosting behaviour.

The requester supplied the following current Vercel excerpt:

```text
app for most ASGI or WSGI frameworks
routes every request to it
```
Source: [Vercel Python runtime](https://vercel.com/docs/functions/runtimes/python.md),
requester-fetched 2026-10-28; the supplied text also lists `app.py` as an entrypoint.
Actual detection of this standard-library WSGI `app`, packaging of workbook/static
assets, and deployed routing remain **unverified until deploy**.

```text
300s (5 minutes) default and max
```
Source: [Vercel limits](https://vercel.com/docs/limits.md), Hobby maximum duration,
requester-fetched 2026-10-28. No `vercel.json` is included because the documented
`maxDuration` setting for a Python framework entrypoint could not be quoted
offline. We rely on the supplied 300-second default; the effective deployment
limit and a complete stage's duration remain **unverified until deploy**.

Upstash REST request shape is **unverified until deploy**. No local excerpt of
the [Upstash REST documentation](https://upstash.com/docs/redis/features/restapi)
was available, and this order prohibits remote access. `RedisLedger._command`
contains the entire assumed wire contract from the order: POST a command JSON
array to the base URL with bearer authorization and consume `result` or `error`.
Only `SET`, `LRANGE`, `RPUSH` and compare-and-delete `EVAL` are used. HTTP redirects
are disabled, HTTP calls have a five-second timeout with no retry, replies are
bounded to 10 MB, and provider/configuration text is omitted from errors. The
fake server verifies this assumed contract; it is not evidence of live Upstash
compatibility. Marketplace environment injection, Redis ACLs, actual shared keys
with PatchWitness and deployed concurrent accounting also await deployment.

### Offline verification

New tests cover FileLedger/Redis JSONL interchange, PatchWitness records and both
global caps, all ledger reads rejecting corrupt records, invalid amounts, nested
locking, contention timeout, owner-safe unlock, lease expiry, malformed replies,
authentication errors, configuration aliases and call-time token rotation.
WSGI checks cover the full sample start/judge/summary flow, static assets, HEAD,
unknown paths, traversal/symlinks, body limits, live gating and secret redaction.

The fake Upstash server uses standard-library `HTTPServer` on `127.0.0.1` and checks
bearer authorization. The same Redis tests also run through a socket-free HTTP
double so binding restrictions do not leave ledger behaviour untested. Loopback
cases skip only on a bind failure. No remote API, browser tool, Git operation or
delegation was used.

Final command: `.venv/bin/python -m pytest -q`:

```text
281 passed, 64 skipped in 1.61s
```

The original 217 cases retain their baseline result of 198 passed and 19 skipped.
The added 128 cases contributed 83 passed and 45 skipped. Every skip has the same
reason: `Loopback bind failed: [Errno 1] Operation not permitted`. Socket-free
versions of every new Redis test and both Redis-backed WSGI tests passed.

Skipped cases (parameter counts in the last column):

| File | Test | Cases |
| --- | --- | ---: |
| `tests/test_api.py` | `test_local_server_full_sample_with_mock_client` | 1 |
| `tests/test_model_stage.py` | `test_http_schema_styles` | 4 |
| `tests/test_model_stage.py` | `test_retry_429_then_success` | 1 |
| `tests/test_model_stage.py` | `test_retry_after_date_or_malformed` | 2 |
| `tests/test_model_stage.py` | `test_500_twice_then_error` | 1 |
| `tests/test_model_stage.py` | `test_other_4xx_never_retry` | 4 |
| `tests/test_model_stage.py` | `test_total_time_budget_refuses_retry_wait` | 1 |
| `tests/test_model_stage.py` | `test_secret_echo_is_redacted` | 1 |
| `tests/test_model_stage.py` | `test_http_one_repair_retry` | 3 |
| `tests/test_model_stage.py` | `test_full_http_pipeline_tool_roundtrip_and_downgrades` | 1 |
| `tests/test_redis_ledger.py` | `test_round_trip_and_reentrant_refresh` | 1 |
| `tests/test_redis_ledger.py` | `test_file_record_format_in_both_directions` | 1 |
| `tests/test_redis_ledger.py` | `test_patchwitness_records_enforce_global_and_granted_caps` | 2 |
| `tests/test_redis_ledger.py` | `test_invalid_record_refuses_all_reads` | 7 |
| `tests/test_redis_ledger.py` | `test_bad_amount_never_appended` | 6 |
| `tests/test_redis_ledger.py` | `test_lock_contention_times_out_then_can_acquire` | 1 |
| `tests/test_redis_ledger.py` | `test_unlock_preserves_other_owners_lock` | 1 |
| `tests/test_redis_ledger.py` | `test_expired_lease_refuses_append` | 1 |
| `tests/test_redis_ledger.py` | `test_bad_redis_replies_are_safe_refusals` | 20 |
| `tests/test_redis_ledger.py` | `test_environment_aliases_prefix_and_token_at_call_time` | 1 |
| `tests/test_redis_ledger.py` | `test_primary_configuration_takes_precedence` | 1 |
| `tests/test_redis_ledger.py` | `test_authentication_error_never_leaks_token` | 1 |
| `tests/test_wsgi.py` | `test_full_sample_flow_through_wsgi_with_shared_redis` | 1 |
| `tests/test_wsgi.py` | `test_redis_failure_returns_budget_without_secrets` | 1 |

Real loopback transport and real deployment behaviour were not exercised in this
sandbox. The tests establish the local accounting and routing behaviour only.
