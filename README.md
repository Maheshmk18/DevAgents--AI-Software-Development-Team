# AI Dev Team Multi-Agent System

This project is an industry-style multi-agent engineering system for software tasks. It is designed for learning, experimentation, and portfolio use.

## Architecture

- Frontend: React + Vite + TypeScript
- Backend: FastAPI + Python
- Workflow orchestration: LangGraph state graph (`planner → developer → reviewer ⇄ developer → tester → docs → finalize`)
- Agents: LangChain chat models (**Groq**) running a real **tool-calling loop** — each agent decides which of its role's tools to call and with what arguments
- Memory: MongoDB
- Tool layer: three real MCP stdio servers (`repo`, `github`, `gmail`) behind a role-scoped tool registry, exposed to the model as function-calling schemas
- Evaluation: a scoring harness that grades every finished run
- User safety: two human approval gates — one before the team starts, one before side-effecting actions

## Agents (real subagents)

Each stage is an LLM subagent, not a template filler. Every agent gets:

- a role-specific system prompt,
- **only the tool schemas allowed for its role**, and
- a shared tool-calling loop (`app/agents/runtime.py`) that keeps going until the
  model stops requesting tools.

Inside the loop the *model* chooses the tool and the arguments. Read-only tools
run immediately; side-effecting tools are **never executed** — they are recorded
as deferred actions behind the human approval gate. Tool results are fed back to
the model so the next step builds on what it just learned.

| Agent | Role |
| --- | --- |
| Planner | Explores the repository, then produces the ordered task plan |
| Developer | Reads the touched files and proposes the real file writes + the test command |
| Reviewer | Re-reads the files, judges correctness and risk, ends with `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED` |
| Tester | Decides the validation commands that prove the change works |
| Docs | Proposes the CHANGELOG/README update, and an issue or email when useful |

Plus one system node, the **Finalizer**, which writes the run's final answer. The Reviewer can send work back to the Developer (bounded, max 2 attempts) before the run continues to validation.

With no LLM configured each agent falls back to a deterministic action, so the
workflow still completes offline (see *LLM provider* below).

## Run lifecycle

1. `POST /api/runs` runs the planner only, then stops at **`waiting_for_human_approval`** with approval `approve_plan_before_execution`.
2. Approving gate 1 runs the whole LangGraph graph, executing read-only tools over MCP as it goes.
3. Any side-effecting proposals become approval `execute_deferred_actions` and the run stops at **`waiting_for_action_approval`**.
4. Approving gate 2 executes those actions over MCP; rejecting marks them `skipped`. Either way the run finishes as `completed`.

Rejecting gate 1 rejects the run outright — the graph never runs.

## Project structure

```text
backend/
  app/
    agents/           # planner, developer, reviewer, tester, docs + runtime.py (tool-calling loop)
    api/              # routes, approvals, tools, MCP
    core/             # agent registry and helpers
    integrations/     # github, gmail clients
    mcp/
      servers/        # FastMCP stdio servers: repo, github, gmail
      client.py       # stdio session helpers
      manager.py      # background event loop + persistent sessions
      tool_registry.py
    services/         # workflow, execution, approval, evaluation, llm
    workflow/         # LangGraph state graph, state, orchestrator
    config.py
    db.py
    main.py
    schemas.py
  tests/            # pytest contract + agent-runtime tests
  requirements.txt
  .env.example
frontend/
  src/
  package.json
  vite.config.ts
```

## Local setup

### Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend

```bash
cd frontend
npm install
copy .env.example .env
npm run dev -- --host 0.0.0.0
```

Backend runs on <http://localhost:8000>, frontend on <http://localhost:5173>.
`VITE_API_BASE` in `frontend/.env` points the console at the backend.

## Features

- Split backend and frontend architecture
- FastAPI health, run, approval, and evaluation endpoints
- MongoDB persistence with an automatic in-memory fallback; availability is cached so an unreachable Mongo never stalls a request
- Real LangGraph state graph with a bounded reviewer ⇄ developer retry loop
- Real LLM subagents that choose their own tools (LangChain tool calling)
- Three MCP stdio servers behind a role-scoped tool registry
- Read-only tools run automatically; side-effecting tools wait behind an approval gate
- Two human approval gates: before the team starts, and before side-effecting actions
- Agent registry and structured agent classes
- React console focused on the dev-team workflow: run history, stage summaries,
  tool activity, deferred actions, MCP servers, approvals, and evaluation scores
- External tool layer for Gmail and GitHub workflows

## Tool layer

The tool layer is a safe, role-scoped registry with connectors for repository
operations, Gmail, GitHub, and evaluation scoring.

### MCP servers

Tools are served by three standalone MCP (Model Context Protocol) stdio servers:

| Server | Tools | Side-effecting |
| --- | --- | --- |
| `repo` | `list_files`, `read_file`, `search_files`, `write_file`, `run_command` | `write_file`, `run_command` |
| `github` | `get_repository`, `list_pull_requests`, `create_issue` | `create_issue` |
| `gmail` | `list_messages`, `send_email` | `send_email` |

Each one runs standalone:

```bash
cd backend
python -m app.mcp.servers.repo
python -m app.mcp.servers.github
python -m app.mcp.servers.gmail
```

The backend connects to them over stdio through `app/mcp/manager.py`, which keeps
one persistent session per server on a background event loop and exposes a
synchronous facade for the (synchronous) LangGraph nodes. Sessions open lazily,
so the ~1s Python start-up cost is paid once per server per process.

Set `MCP_ENABLED=false` to bypass the servers entirely, and the registry falls
back to the in-process clients automatically if a server fails to start, so a run
never breaks. Live status is available at `GET /api/mcp/servers`.

### Tool policy

An agent may only ever use the tools mapped to its role — the set is scoped both
in the schemas sent to the model and re-checked inside the loop. Within that set,
**read-only** tools run automatically and **side-effecting** tools are recorded
as *deferred actions* that are never executed without approval.

| Agent | Tools exposed | Deferred by policy |
| --- | --- | --- |
| Planner | `repo.list_files`, `repo.search_files`, `github.get_repository` | — |
| Developer | `repo.read_file`, `repo.list_files`, `repo.write_file`, `repo.run_command`, `github.get_repository` | `repo.write_file`, `repo.run_command` |
| Reviewer | `repo.read_file`, `repo.run_command`, `github.list_pull_requests` | `repo.run_command` |
| Tester | `repo.run_command`, `github.list_pull_requests` | `repo.run_command` |
| Docs | `repo.read_file`, `repo.write_file`, `gmail.send_email`, `github.create_issue` | `repo.write_file`, `gmail.send_email`, `github.create_issue` |

With an LLM configured the agent chooses which of those to call. Without one,
each agent uses a fixed deterministic action (developer → `repo.write_file`
`IMPLEMENTATION_NOTES.md` + `repo.run_command python -m pytest -q`; reviewer →
`python -m compileall`; tester → `python -m pytest -q`; docs → `CHANGELOG.md`).

Identical proposals from different agents are deduplicated. Tool output is
truncated before it is stored, so run history stays small. Agent tool use is
visible in the console under **Tool activity**, and each stage shows the agent's
own summary under **Workflow stages**.

## LLM provider

The agents use **Groq** as their LLM, through LangChain's `ChatGroq` model
(`app/services/llm_service.py`). There is no other provider: Groq is the whole
LLM story. With no Groq key configured the agents fall back to deterministic
actions, so the workflow always runs — you just don't get model-authored work.

```env
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=llama-3.3-70b-versatile
```

Get a key at <https://console.groq.com/keys>. Switch models with `GROQ_MODEL`
(see <https://console.groq.com/docs/models>).

### If the agent falls back instead of using the LLM

`GET /api/llm` reports the configured provider and the last error, and the
console shows an `LLM:` badge in the sidebar. Common causes:

- **`No Groq API key configured`** — add `GROQ_API_KEY` to `backend/.env` and
  restart the backend. The console badge will then read `LLM: groq`.
- **`RateLimitError ... insufficient_quota` / `401 invalid_api_key`** — the key is
  missing, wrong, or out of quota. Check it at <https://console.groq.com/keys>.

When no provider is reachable the workflow still completes, but the stage text is
the deterministic fallback rather than generated prose. After a provider failure
the LLM is skipped for a short cooldown (15 s), so one broken key does not add a
slow retry to every agent in the run.

## Database

MongoDB stores runs, approvals, and evaluation scores. If Mongo is unreachable the
app transparently uses an in-memory store instead, so the console keeps working —
but run history is then lost when the backend restarts.

The availability check is cached for 30 s, so an unreachable Mongo costs at most
one short timeout instead of one per operation.

If you want durable history, point `MONGO_URI` at a reachable cluster and make
sure the machine running the backend is allowed in **Atlas → Network Access**.
The symptom of a blocked IP is `SSL handshake failed` / `TLSV1_ALERT_INTERNAL_ERROR`
in the backend log.

## External credentials

Set these in the backend `.env` to replace the demo-safe fallbacks with real
calls:

```env
GITHUB_TOKEN=your_github_personal_access_token  # github.com -> Settings -> Developer settings
GMAIL_SENDER_EMAIL=you@gmail.com
GMAIL_ACCESS_TOKEN=your_google_oauth_access_token
```

If those values are not configured, the app uses demo-safe fallback responses so the workflow still runs locally.

## Getting credentials

### GitHub token

1. Sign in to GitHub → click your avatar → **Settings**.
2. **Developer settings** (bottom of the left sidebar) → **Personal access tokens**.
3. Pick **Fine-grained tokens** → **Generate new token**:
   - *Repository access*: select the repo the agent should inspect.
   - *Permissions*: `Metadata: Read-only`, `Contents: Read-only`,
     `Issues: Read and write`, `Pull requests: Read-only`.
4. Copy the token (starts with `github_pat_…`) into `GITHUB_TOKEN`.

Classic tokens work too — use the `repo` scope.

### Gmail

Two values are needed. `GMAIL_SENDER_EMAIL` is just your Gmail address.
`GMAIL_ACCESS_TOKEN` is an OAuth2 **bearer token**, never your password:

1. Open <https://console.cloud.google.com> and create (or select) a project.
2. **APIs & Services → Library** → enable the **Gmail API**.
3. **APIs & Services → OAuth consent screen** → External → add your own address
   under **Test users**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**, then copy
   the client ID and secret.
5. Open the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground):
   - Gear icon → tick **Use your own OAuth credentials** → paste the ID and secret.
   - Authorize `https://www.googleapis.com/auth/gmail.send` (add
     `https://www.googleapis.com/auth/gmail.readonly` to list messages).
   - Click **Exchange authorization code for tokens** and copy the **Access token**.
6. Paste it into `GMAIL_ACCESS_TOKEN`.

> **Heads up:** Google access tokens expire after about an hour. The Gmail client
> currently reads a static token, so for long-lived use you would store the
> refresh token and refresh it on demand — that part is not implemented yet.

## Tests

```bash
cd backend
python -m pytest tests -q
```

The suite covers the two things that are easy to break by accident: the graph
shape (five agents plus a non-agent finalizer, bounded review loop, no browser
node) and the tool-safety policy (read-only tools auto-run, side-effecting tools
are only ever *proposed*). `tests/test_agent_runtime.py` additionally drives the
LLM tool-calling loop with a scripted model **and** with a real `ChatGroq`
model over a mocked HTTP transport, so the full model→tool→model path is verified
without a key. `tests/test_db_fallback.py` covers the database availability
cache. Tests are hermetic: the LLM is disabled, MCP is forced in-process, and
Mongo looks unreachable, so they never make network calls or spawn server
subprocesses.

## Next milestones

1. Stream LangGraph progress to the console instead of one final payload
2. Add GitHub/CI integration at the action boundary
3. Persist and replay run history across restarts (durable Mongo deployment)
4. Add per-agent token/latency accounting to the evaluation harness

## Safety note

The project keeps human approval at the action boundary: no file is written and
no shell command runs until a human approves gate 2. The only tools an agent may
run on its own are read-only ones (`list_files`, `read_file`, `search_files`,
and GitHub reads).
