# Exception Keeper

Exception Keeper finds the spreadsheet cells that break the pattern, then asks NVIDIA
Nemotron to tell a mistake from a documented exception. Every verdict must point back at
the workbook (a quote found at the cell it cites, or the empty input itself); a verdict
that cannot be grounded is downgraded to "ask a person".

- **Live demo:** https://exception-keeper.vercel.app/
- **Video (under 3 minutes):** https://youtu.be/dnhnF02bAvY
- **Track:** Best Apps and Agents · Nebius x NVIDIA Global AI Hackathon

The web app includes three fictional sample workbooks. Their results are **recorded live
runs** of NVIDIA Nemotron 3 models through Nebius Token Factory (2026-10-24); viewing them
costs $0 and calls nothing. Uploading your own `.xlsx` runs the models live.

## How NVIDIA Nemotron and Nebius Token Factory are used

Code finds and measures the unusual cells; the models only interpret them. All model calls
go to the Nebius Token Factory OpenAI-compatible API (`https://api.tokenfactory.nebius.com/v1/`)
at runtime, with structured JSON output.

| Stage | Model | What it does | Calls per workbook |
|---|---|---|---|
| Find candidates | none (deterministic engine) | Five detectors: pattern break, typed number in a formula run, total that skips cells, formula reading an empty input, labelled constant. The engine also computes the impact. | 0 |
| Outline | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Which sheets hold data, rules and notes. | 1 |
| Judge | `nvidia/nemotron-3-super-120b-a12b` | Classifies each candidate as error, documented exception, ask or assumption to verify. Read-only tools (read a range, find notes, find a rule, list same-pattern cells) are offered; in the recorded runs Super did not call them. | up to 12 |
| Ground | none (code) | A quote must be found at the cited cell; an exception must quote a note, comment or rule; a fix must be a valid formula. Otherwise the verdict becomes "ask". | 0 |
| Summarise | `nvidia/Nemotron-3-Ultra-550b-a55b` | Orders the findings, names conflicts, writes the headline and the one question for the user. | 1 |

Token Factory made the three-size design cheap to build: one endpoint and key, a model-ID
change between stages, rate-limit headers and per-model prices that feed the app's spending
ledger. A full sample run cost $0.002–$0.016.

## Run it locally

Developed and tested with Python 3.14.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest xlsxwriter   # xlsxwriter: tests and sample generator
.venv/bin/python -m pytest -q        # offline tests, no network
.venv/bin/python serve.py            # then open http://127.0.0.1:4174
```

The samples work without a key. To run the models live locally, set `NEBIUS_API_KEY`,
`EK_LIVE=1` and `EK_GRANTED_CREDITS_USD` (your granted credit) in the environment.
`samples/make_samples.py` rebuilds the three sample workbooks and their ground truth.

## Recorded live results (2026-10-24)

The deterministic engine finds the candidate cells; Nemotron Nano outlines the workbook,
Nemotron Super judges each candidate and Nemotron Ultra orders the findings. The same
judging code was run 6–9 times per sample, against the planted labels in
`samples/ground_truth.json` (a planted "ambiguous" cell counts as correct only when the
app asks).

**The shipped sample files are selected runs, not typical runs.** After four plain runs the
last one was kept, and it already tied the best score seen; later re-runs replaced a file
only when they scored higher (event_budget once). The files themselves are unedited. The
table gives every run, so the spread is visible.

| Sample | Planted cells | Correct per run, in order | Shipped run |
|---|---|---|---|
| club_membership_fees | 5 | 4, 3, 4, 4, 4, 4, 3, 4, 4 | 4 of 5 (4th run, 12:35 JST) |
| small_business_budget | 5 | 3, 3, 4, 4, 3, 3, 3, 1, 4 | 4 of 5 (4th run, 12:34 JST) |
| event_budget | 2 | 1, 1, 1, 1, 1, 2 | 2 of 2 (6th run, 13:12 JST, the only full run) |

Repeated misses: C26 (ambiguous) was judged an error in 9 of 9 runs; event D4 (a blank
input with a "pending" comment) was asked in 4 of 6 and called a documented exception
once; B10 (assumption) was asked in 7 of 9 and B11 (assumption) judged an error in 6 of 9.
Across all runs, 33 of 102 verdicts on planted cells were wrong: 17 were "ask", which leaves
the cell to a person, and 16 were confident mistakes (mostly C26 and B11, once D4). A run costs $0.002–$0.016 per sample. Known limitations:

- Verdicts vary between runs even at temperature 0. The engine's evidence and impact
  figures are deterministic; the model verdicts are not.
- With reasoning on, strict `json_schema` answers cut quoted text at the first double
  quote (`=IF(B14=` for `=IF(B14="Student",...)`). Such quotes still ground as a prefix
  of the cell, but look truncated on screen. `json_object` kept the quotes but broke the
  shape or ran out of tokens more often (7 of 12), so the strict schema stays the default.
- Grounding downgrades a verdict to "ask" when a quote is not found at the cited cell;
  it caught a comment cited at the wrong cell and a claim that a non-empty cell was blank.
- Single model calls took up to 10.7 s. The adapters allow 55 s per call; the serverless
  function limit must be checked at deploy.
- Logs of every run (per call: model, tokens, finish reason, seconds; no message text)
  are kept outside this repository. `scripts/live_samples.py` records a run; the summary
  prompt was tightened (one English sentence, at most 160 characters) before the shipped
  runs, after an earlier headline hit the schema's 200-character cut mid-word.

## Local preview

Use the existing environment from this directory; no installation or build step:

```sh
.venv/bin/python serve.py
```

Open `http://127.0.0.1:4174` yourself. The server never launches a browser.
It serves `web/`, the three allowlisted sample workbooks and precomputed reports,
and `POST /api/ek`. Uploaded workbook bytes are processed in memory and not saved.
The preview is a local development server, not a deployment configuration.

Live calls default to disabled. Without **both** `EK_LIVE=1` and a positive, finite
`EK_GRANTED_CREDITS_USD`, model stages refuse with:

```json
{"error":"budget","message":"Live calls are disabled. Use the sample results (cost $0)."}
```

Samples need no API key. Live mode additionally
needs `NEBIUS_API_KEY`, read only at call time. The optional judge lane uses
`EK_JUDGE_PASSPHRASE`, also read only at call time. Do not put either secret in files,
URLs or command-line arguments. Set `EK_GRANTED_CREDITS_USD` to the credit actually
granted (shared with PatchWitness); the ledger refuses calls beyond it.

For live accounting, set `X129_LEDGER_PATH` to the same ledger used by PatchWitness.
Otherwise the local preview uses `.ek-preview-ledger.jsonl` in this directory.
This preview default cannot count spend recorded in a different ledger.
`EK_SCHEMA_STYLE` selects `openai`, `nebius_doc`, `json_object`, or `none`.
`EK_JUDGE_MODE` selects `tools` or `json_only`. See [MODEL_STAGE_NOTES.md](MODEL_STAGE_NOTES.md)
for session rules, caps and deployment limitations.

## Deploy on Vercel

The root `app.py` exports the WSGI callable `app`. It serves the web page, web
assets, allowlisted samples and `POST /api/ek`. `serve.py` remains the local preview.
`requirements.txt` pins the runtime workbook reader; no Redis client library is used.

Provision one Upstash Redis database through the Vercel Marketplace and connect
both Exception Keeper and PatchWitness to it. The environment variable names are
`UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`, with `KV_REST_API_URL` and
`KV_REST_API_TOKEN` as fallback names. Both apps must use the same optional
`X129_LEDGER_PREFIX` (default `x129:ledger`), record format and lock protocol.
Live deployment never uses `X129_LEDGER_PATH` or a per-instance file.

Live execution requires explicit opt-in through `EK_LIVE=1`, a positive finite
`EK_GRANTED_CREDITS_USD`, and `NEBIUS_API_KEY`. `EK_JUDGE_PASSPHRASE` is optional
for the judge lane. Set credentials through the environment only. No secret values
belong in files, URLs or command-line arguments. Without Redis configuration,
live model stages return 503 `budget`; with live disabled, sample viewing remains
available at no model cost.

Vercel's supplied Python documentation names `app.py` and says
`app for most ASGI or WSGI frameworks`; its supplied limits excerpt gives Hobby
`300s (5 minutes) default and max`. Sources: [Python runtime](https://vercel.com/docs/functions/runtimes/python.md)
and [limits](https://vercel.com/docs/limits.md), fetched by the requester on 2026-10-28.
`vercel.json` sets `maxDuration` 120 s on the `app.py` entrypoint, the form Vercel documents
for Python apps ("Key the `functions` entry on that entrypoint file"). The Upstash REST request
shape (a JSON array command in a POST body with a Bearer token) follows
[Upstash's REST API docs](https://upstash.com/docs/redis/features/restapi).
See the dated Order 07 section in [MODEL_STAGE_NOTES.md](MODEL_STAGE_NOTES.md).

## Offline checks

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q -rs
```

The full preview round trip is
`tests/test_api.py::test_local_server_full_sample_with_mock_client`.
It injects a scripted client into the real local server and runs start, every judge,
and summary. Tests use no remote services. Loopback tests skip only when binding
`127.0.0.1` fails. If this sandbox forbids binding, preview startup also cannot be
verified here; run the same command on the host.

For the reviewer's browser smoke check, open `/dev-smoke.html` on the preview.
Its `pre#out` becomes JSON keyed by sample with actual rendered finding counts,
verdict counts, cell-map presence and console errors. It uses the same renderer as
the main page. No browser check was performed by the coding worker.

The original deterministic engine and scripted review CLI remain available:

```sh
.venv/bin/python -m sheetaudit samples/event_budget.xlsx --json
.venv/bin/python -m sheetaudit.review samples/event_budget.xlsx --mock tests/fixtures/mock_scripts/event_budget.json
```

Library and runtime source excerpts are recorded in [ENGINE_NOTES.md](ENGINE_NOTES.md)
and [MODEL_STAGE_NOTES.md](MODEL_STAGE_NOTES.md). The WSGI entrypoint and Redis
ledger are tested offline; no deployment was attempted. Corrected-workbook export
remains outside this order.

## License

[Apache License 2.0](LICENSE).
