# Family Finance Claw

Telegram family finance bot powered by local Codex instead of a remote LLM API.

The current system is designed for a 2-person household:
- `小灰毛` is the assistant
- `小鸡毛` is the male owner
- `小白` is the female owner

It supports daily expense tracking, budget management, memory confirmation, special-project spending, scheduled summaries, and a Codex-backed agent workflow that can evolve into a multi-assistant orchestration layer later.

For latency-sensitive common turns, the bot now also uses a simple-finance fast path:
- still handled by Codex
- but with a much smaller prompt
- and a single finance workbench action path for common expense and budget actions

## What It Does

- Natural-language expense tracking in Telegram
- Receipt/photo-based expense handling
- Personal vs family queries
- Monthly budgets with change history and deduplicated alerts
- `regular` vs `special` spending scopes
- Special plans like trips with `planning / active / closed` states
- Memory capture with confirmation first, then English storage
- Memory archive/update lifecycle
- Weekly and monthly summary jobs
- Friday proactive weekend spending suggestions
- Local Codex bridge with database-grounded finance replies

## Current Architecture

```text
Telegram
-> bot handlers
-> assistant router
-> resident agent service
-> Codex session manager
-> resident Codex runtime (app-server preferred, exec/resume fallback)
-> resident action registry
   -> finance workbench for simple turns
   -> terminal workbench for command-style actions
   -> resident bridge surface
-> skills
-> SQLite
```

## Key Design Choices

### 1. Codex instead of a remote API

The bot no longer depends on Gemini or another hosted LLM API for the main message loop.

Instead:
- Telegram receives the message
- the repo builds a strict prompt
- local Codex handles the turn
- finance facts are expected to go through `app.bridge_ops`

This makes it easier to:
- keep all business logic in the repo
- reuse existing skills directly
- reduce API lock-in
- evolve toward a resident multi-assistant service later

### 2. Database-first finance answers

For finance, budget, memory, and history questions, the intended path is:

```text
Codex
-> app.bridge_ops
-> skill execution
-> SQLite
```

This keeps amounts and historical claims grounded in the database instead of free-form generation.

### 3. Session-aware Codex threads and resident runtime

The bot now tracks Codex sessions by:
- `assistant_id`
- `user_id`
- `chat_id`

The current runtime now supports:
- a resident `codex app-server` process when available
- stored Codex thread ids per Telegram thread
- `app-server` first, with `exec/resume` as fallback
- explicit degraded/fallback state so runtime downgrades are visible
- persistence of chat-to-thread mapping in `data/codex_sessions.json`
- runtime/provider-facing configuration, so the upper service layer is not permanently locked to Codex

Threading rules are:
- private chat -> personal thread
- family group chat -> family thread
- another group chat -> another separate family thread

### 4. Future multi-assistant path

Even though the repo currently runs one assistant, the structure now leaves room for:
- one outer Codex service
- multiple assistants
- multiple repos
- assistant routing by id / alias
- per-assistant workspace, bridge, and session store
- future runtime adapters such as Claude Code, without replacing the whole Telegram/product skeleton

## Main Modules

### Telegram layer

- [`app/bot/handlers.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/bot/handlers.py)
  Telegram commands, text routing, photo routing, help, reset, memory display.

- [`app/bot/scheduler.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/bot/scheduler.py)
  Weekly summary, monthly summary, budget alerts, nudges.

### Agent layer

- [`app/core/agent.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/core/agent.py)
  Prompt construction, memory confirmation, write-operation confirmation, delete-candidate matching, Codex turn orchestration.

- [`app/core/resident_agent.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/core/resident_agent.py)
  Service abstraction that Telegram talks to.

- [`app/core/codex_session.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/core/codex_session.py)
  Codex runtime/session state, resident `app-server`, persistent thread ids, and fallback logic.

- [`app/core/runtime_provider.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/core/runtime_provider.py)
  Provider-facing adapter layer so the service can keep the same outer architecture while switching runtime backends later.

- [`app/core/finance_workbench.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/core/finance_workbench.py)
  Fixed action surface for common finance turns such as simple expense recording, recent records, monthly total, budget query, budget update, and delete-by-id.

- [`app/core/terminal_workbench.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/core/terminal_workbench.py)
  Resident action surface for terminal-style commands such as runtime status, memory listing, export preparation, and context reset.

- [`app/core/action_registry.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/core/action_registry.py)
  In-process Unix-socket action gateway that lets Codex and Telegram handlers share one resident execution surface instead of shelling out to fresh Python processes.

- [`app/core/assistant_registry.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/core/assistant_registry.py)
  Assistant definitions and optional JSON-based registry loading.

- [`app/core/assistant_router.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/core/assistant_router.py)
  Explicit assistant routing such as `@family-finance ...` or `/assistant family-finance ...`.

- [`app/core/session.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/core/session.py)
  Telegram session state, display names, sticky assistant id.

### Finance layer

- [`app/bridge_ops.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/bridge_ops.py)
  Controlled CLI entrypoint for snapshots, skill calls, and memory writes.

- [`app/services/skills.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/services/skills.py)
  High-level business actions.

- [`app/services/expense_service.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/services/expense_service.py)
  Expense CRUD and export support.

- [`app/services/stats_service.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/services/stats_service.py)
  Summaries, aggregations, report data.

- [`app/database.py`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/app/database.py)
  SQLite schema and migrations.

## Important Product Behaviors

### Expense handling

- Short commands like `午饭 15` should record directly.
- Simple finance requests like `午饭 15`, `本月花了多少`, `看看最近5笔`, `删除 #123`, and `餐饮预算设为1000` should prefer the fast path.
- High-risk writes should confirm first:
  - delete expense
  - change budget
  - start/stop special plan
  - archive/update memory

### Memory handling

- Bot detects likely memory candidates
- asks whether to store them
- stores confirmed memory in English
- supports archive and versioned update

### Special spending

Expenses can be:
- `regular`
- `special`

This lets the bot keep day-to-day spending separate from project-style spending such as:
- travel
- renovation
- wedding

Special plans can be created before the event starts, so costs like flights or visas can be attached during `planning`.

## Telegram Commands

- `/start`
- `/help`
- `/delete`
- `/export`
- `/usage`
- `/memory`
- `/reset`

Natural-language help also works, such as:
- `你会什么`
- `你能做什么`
- `怎么用你`

## Local Run

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Prepare `.env`

```bash
cp .env.example .env
```

For local VM/non-Docker runs, it is best to set explicit absolute paths:

```env
TELEGRAM_BOT_TOKEN=...
ALLOWED_USER_IDS=...
FAMILY_MEMBERS=123456:小鸡毛,789012:小白

PYTHON_BIN=/absolute/path/to/python
CODEX_BIN=codex
CODEX_HOME=/absolute/path/to/codex-home
CODEX_WORKDIR=/absolute/path/to/family-finance-claw
DATABASE_PATH=/absolute/path/to/family-finance-claw/data/expenses.db
CODEX_SESSION_STORE_PATH=/absolute/path/to/family-finance-claw/data/codex_sessions.json
RUNTIME_PROVIDER=codex
CODEX_REASONING_EFFORT=low
DEFAULT_ASSISTANT_ID=family-finance
DEFAULT_ASSISTANT_NAME=小灰毛
CODEX_RUNTIME_MODE=app-server
CODEX_SERVICE_TIER=fast
```

### 3. Make sure Codex is logged in

```bash
codex login
```

Or if you want a repo-local Codex home:

```bash
mkdir -p codex-home
CODEX_HOME=$(pwd)/codex-home codex login
```

### 4. Start the bot

```bash
python -m app.main
```

If you want a safer rollout on a VM, use:

```env
CODEX_RUNTIME_MODE=auto
```

That will prefer the resident `app-server` runtime and fall back to the older `exec/resume` path if needed.

## Docker

The repo still includes Docker support, but the simplest production path for a single Ubuntu VM is often:
- install Codex on the VM
- use a Python environment directly
- run the bot under `tmux` or `systemd`

If you use Docker, make sure:
- `CODEX_HOME` is mounted
- data directory is mounted
- the container can access a logged-in Codex home

## Assistant Registry Configuration

By default, the repo registers only the finance assistant.

If you want registry-based loading later, set:

```env
ASSISTANT_REGISTRY_PATH=/absolute/path/to/assistants.json
```

See the example file:
- [`data/assistants.example.json`](/Users/jinxiang.gan/Desktop/code/project/family-finance-claw/data/assistants.example.json)

That is the first step toward one outer Codex service routing multiple assistants across multiple repos.

## Recommended Test Flow

After deployment, test these messages in Telegram:

1. `你会什么`
2. `午饭 15`
3. `看看最近5笔`
4. `餐饮预算设为1000`
5. `是`
6. `创建日本旅行计划`
7. `是`
8. `日本签证 300`
9. `这月花了多少`
10. `以后少点外卖`
11. `是`
12. `/memory`

## Repo Notes

- Prompt/instruction text is stored in English for model-facing logic.
- User-facing Telegram replies remain Chinese.
- This repo is no longer best described as “Gemini API bot”.
- It is now better described as a local Codex-powered Telegram finance agent with a database-backed skill layer.
