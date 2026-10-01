# File-Management AI Agent — Checkpoint 1

A small AI agent whose "brain" is the Qwen3-14B model and whose harness (the code
here) calls the model, executes filesystem actions, and returns a single JSON
result. It handles four task types: create a file, delete a file, read a file and
answer a question, and summarize multiple files into a new file.

The agent does **not** delegate to any existing agent platform or SDK. At runtime
the only external service it calls is the configured Qwen3-14B `/chat/completions`
endpoint. Everything else uses the Python standard library only.

## Folder structure

```
run_agent.sh          Entry point. Resolves source relative to itself, execs main.py.
agent/
  main.py             CLI entry, ReAct loop, JSON output contract, error guards.
  llm_client.py       stdlib HTTP client for OpenAI-compatible /chat/completions;
                      retries + <think> stripping.
  fsops.py            Sandboxed read/write/delete/list; path validation chokepoint.
  prompts.py          System prompt (action schema + examples) and user-message builder.
tests/
  run_tests.sh        Smoke tests for all four capabilities in fresh temp workspaces.
  env.example.sh      Sample env var blocks (course / OpenRouter / local Ollama).
readme.md
```

## How it works

1. `run_agent.sh` receives the full prompt as `$1` and runs with the task workspace
   as the current directory. It execs `agent/main.py`.
2. `main.py` reads `OPENAI_BASE_URL`, `MODEL`, and `QWEN_API_KEY` from the
   environment (nothing is hard-coded) and builds a system prompt plus a first
   user message that includes a bounded listing of the workspace.
3. The model replies with exactly one JSON action
   (`read_file` / `read_files` / `write_file` / `delete_file` / `list_dir` / `final`).
   The harness executes it, appends an observation, and loops until the model emits
   a `final` action or a step/time budget is hit.
4. Exactly one JSON object is written to stdout:
   `{"status":"success|error","message":"..."}`. All logs go to stderr.
   Exit code is 0 on success, nonzero on error.

### Safety / sandboxing
All file paths flow through `fsops.resolve()`, which rejects absolute paths, `~`,
and any path that escapes the workspace root (checked via `realpath`). Deletes are
restricted to regular files (never directories). Reads are capped at 2 MB.

## Requirements

- **Python 3.9+** (tested on Python 3.9.6 and 3.11.14, macOS / Darwin). Standard
  library only — **no required pip packages**. It runs on a plain system/base
  Python with no virtual environment needed.
- **Bash** for the entry point.
- Network access to the configured `/chat/completions` endpoint.
- `requirements.txt` lists one **optional** package (`certifi`), useful only on
  macOS/conda where the default CA bundle may be missing. On Linux it is not
  needed; the agent falls back to the system CA store automatically.

### TLS / networking notes
- The client verifies HTTPS using, in order: `AGENT_CA_BUNDLE` (if set),
  `certifi`'s bundle (if `certifi` is importable), then the system default. On
  macOS/conda the default bundle is often missing/empty, causing
  `CERTIFICATE_VERIFY_FAILED`; having `certifi` installed (it usually already is)
  fixes this transparently. `AGENT_INSECURE_SSL=1` disables verification for local
  dev only — never used for grading. Linux graders normally work with the system
  bundle and need none of this.
- Requests send a normal `User-Agent`, since the endpoint sits behind Cloudflare,
  which blocks the default `Python-urllib` UA with error 1010.

## Grading contract

```bash
cd "$TASK_DIR"
OPENAI_BASE_URL="https://llm.montii.me/v1" \
MODEL="Qwen/Qwen3-14B" \
QWEN_API_KEY="$GRADING_API_KEY" \
bash "$SUBMISSION_DIR/run_agent.sh" "$PROMPT"
```

## Running the tests locally

```bash
export OPENAI_BASE_URL="https://llm.montii.me/v1"
export MODEL="Qwen/Qwen3-14B"
export QWEN_API_KEY="your-dev-key"      # not committed
bash tests/run_tests.sh
```

Each test builds a fresh `mktemp -d` workspace, runs the agent as a new process,
validates the stdout JSON and exit code, and inspects the resulting files. See
`tests/env.example.sh` for OpenRouter / local-Ollama alternatives during
development; grading always uses the course endpoint and model `Qwen/Qwen3-14B`.

## Notes for the grader

- Qwen3 reasoning ("thinking") is **enabled** for better accuracy on counting,
  arithmetic, and multi-step answers. The client strips the `<think>...</think>`
  block and the loop extracts the first balanced JSON object, so reasoning never
  leaks to stdout. Sampling follows Qwen3's guidance for thinking mode
  (temperature 0.6, top_p 0.95), since greedy decoding degrades in that mode.
- Every model call is bounded by an absolute deadline derived from a 160s internal
  budget, so a slow/long thinking response can never push the run past the 180s
  hard limit; the agent always emits valid JSON in time.
- The whole run is wrapped so that stdout is always a single valid JSON object,
  even on configuration errors, network failures, or unexpected exceptions.
- Time budget is capped at 150s internally to stay under the 180s hard limit.
```
