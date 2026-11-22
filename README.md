# ple-harness: plan–work–verify harness on Claude Opus 5.5

BhNu's entry for the Kaggle competition **Google - Agent Harness on
Physical-Engineering's Last Exam** (PLE). It is built from the kit's
[`custom-openai`](../ple-kit/sample_submissions/custom-openai/) sample — the
full-featured reference harness (streaming, MCP tools, context compaction,
deadline-aware retries) — with the planner/worker/verifier phase pattern from
the kit's [`plan-verify`](../ple-kit/sample_submissions/plan-verify/) sample
layered on top.

| File | Purpose |
|---|---|
| `serve.py` | Three-phase agent loop, container and MCP tools, context compaction, deadline management |
| `taskbridge.py` | Serve protocol implementation (shared across Python samples, unchanged) |
| `requirements.txt` | Pinned `openai` and `websockets` dependencies |
| `submission.yaml` | Model declaration (`google-cloud/claude-opus-5-5`), build command, server startup |
| `LICENSE` | Apache 2.0 |

## How it works

Each task runs in three phases, each a separate conversation with the same
model (all calls go through the OpenAI Chat Completions API via
`MODEL_BASE_URL`, `MODEL_API_KEY`, and `MODEL_NAME`, so the same code runs
against any `google-cloud/<id>` model and, for local runs, any personal model):

1. **Planner** (read-only: `bash`, `read_file`; first 10% of the task time).
   Inspects the project files and writes a structured plan: each deliverable
   with its exact path and format, the data and method to use, and how to
   check it. It cannot create or change files, and the harness enforces the
   tool set — a call to a tool the phase was not offered is refused.
2. **Worker** (full tools: `bash`, `read_file`, `write_file`, `edit_file`, plus
   the task's MCP tools; until 25% of the time remains). Executes the plan,
   writes the deliverables early and improves them, and returns the summary
   used as `final_message`.
3. **Verifier** (read-only, fresh conversation with only the task, the plan,
   and the worker's summary). Opens every deliverable, recomputes the key
   numbers, and replies `PASS` or a numbered list of gaps. Gaps are fed back
   into the worker's conversation for a fix round (up to 2 rounds while at
   least 2 minutes remain before the wrap-up deadline).

### Time budgets

| Phase | Wraps up when |
|---|---|
| Planner | 10% of total task time is used (`PLAN_SHARE`) |
| Worker | 25% of total task time remains (`CHECK_SHARE`) |
| Verifier | 12.5% of total task time remains (`VERIFY_SHARE`) |
| Fix round | 4 minutes remain (`WRAP_UP_SEC`) |

A phase's model calls and `bash` commands are capped to end before the phase's
wrap-up threshold, so a slow call cannot eat the next phase's time. Three
minutes before the task deadline (`FINISH_MARGIN_SEC`), the harness asks the
model for its final summary with no tools.

### Inherited from `custom-openai`

- **MCP discovery:** small tool catalogs (≤128 tools, <40,000 chars of schema
  JSON) are exposed directly as function tools; larger catalogs are listed in
  the system prompt with `mcp_find` / `mcp_call` for on-demand lookup.
- **Streaming + retries:** every model call is streamed; timeouts, `429`,
  `5xx`, dropped connections, mid-stream errors, and streams ending without a
  finish reason are retried up to 5 times with backoff, without adding failed
  partial responses to the conversation.
- **Context compaction:** large tool outputs are head/tail truncated on
  arrival; once the prompt passes `COMPACT_AT_TOKENS` (or the endpoint reports
  a context-length error), older tool outputs are trimmed, then older turns
  are summarized into notes by the model (Claude's signed thinking blocks are
  preserved on kept turns).
- **Claude prompt caching:** the system prompt and newest message carry
  `cache_control` breakpoints, so each turn reads the conversation so far from
  cache.

## Configuring the model

```yaml
model_api: google-cloud/claude-opus-5-5
```

The submitted entry always declares `google-cloud/claude-opus-5-5`. For local
dev runs, `model_api` may temporarily name a personal model (e.g.
`gemini/<model>` with `GEMINI_API_KEY` in an `--env-file` kept outside this
repository); the harness needs no code change because it reads the model
purely from environment variables. See `../PLAN.md` ("Model swap untuk dev
lokal") for the swap policy and the Writeup for how it is disclosed.

Optional settings under `env:` in `submission.yaml`:

| Variable | Default | Description |
|---|---|---|
| `MAX_OUTPUT_TOKENS` | `32000` | Max completion tokens per call (lowered automatically if the endpoint enforces a smaller cap) |
| `COMPACT_AT_TOKENS` | `150000` | Prompt token threshold that triggers context compaction |
| `REASONING_EFFORT` | the model's highest | Reasoning level sent with each call (`max` for Claude) |

## Checking and running

From the kit repository root (`../ple-kit`):

```bash
uv run python -m kit validate ../my-harness   # submission.yaml only (no Docker)
uv run python -m kit check ../my-harness      # full check: build, model, smoke, MCP, artifacts
uv run python eval/run.py ../my-harness --env-file local.env   # dev set; writes artifacts/
```

A mock smoke test that exercises the three phases against a fake model
endpoint and a fake evaluator (no real model key, writes no artifacts) lives
in `../tests/mock_smoke.py`.

## Fair play

- The harness never reads graders, reference files, or its own `artifacts/`,
  never hard-codes task answers, and never branches on specific tasks.
- `artifacts/` is produced only by `eval/run.py` from this harness; nothing in
  it is created or edited by hand.
- The harness connects only to its declared model's endpoint, through the
  egress proxy; no web search, no external APIs, no provider-hosted tools.
