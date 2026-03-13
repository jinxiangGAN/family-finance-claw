# Family Finance Telegram Bot

A smart Telegram bot for family expense tracking, powered by a **3-tier memory-augmented LLM agent** with MCP-style pluggable tools and dynamic prompt assembly. Designed for a 2-person household to record, query, and analyze daily spending through natural language — with long-term memory, user profiling, and context-aware persona that truly understands your family.

## Features

### 🧠 3-Tier Memory Architecture (v4)
- **Core Profile (Tier 1)** — Persistent user identity: financial goals, preferences, lifestyle traits. Auto-updated by the Agent when it detects changes.
- **Working Memory (Tier 2)** — Ephemeral conversation context; maintains natural multi-turn dialogue flow with auto-cleanup on session timeout.
- **Episodic Memory (Tier 3)** — Past events, decisions, and spending patterns. Supports **vector cosine similarity** search (with LLM embedding API) and **FTS5 full-text search** fallback.

### 🧩 Dynamic Prompt Factory
- **Modular Prompt Assembly** — No more static global system prompt. Each request dynamically composes:
  - `[System_Directive]` — Core persona and capabilities
  - `[Time_Space_Anchor]` — Injected current time, location, currency (prevents temporal hallucination)
  - `[Core_Profile]` — User's persistent identity and financial goals
  - `[Working_Context]` — Recent conversation turns
  - `[Relevant_Context]` — Top-K episodic memories recalled by semantic similarity
  - `[Persona_Overlay]` — Private/group chat tone adaptation

### 🎭 Session-Aware Persona
- **Private Chat** — Warm, empathetic, personalized advice; references your core profile goals
- **Group Chat** — Objective, concise, numbers-only; protects individual privacy

### 💰 Expense Tracking
- **Natural Language** — Send `lunch 35` or `taxi 18` to record instantly
- **Receipt OCR** — Send a photo of a receipt → vision model auto-extracts expenses
- **Multi-Currency** — Record in SGD, CNY, USD, AUD, JPY, etc. with automatic conversion
- **Three Query Views** — Check spending for yourself, your spouse, or the whole family
- **Event/Trip Tags** — Tag expenses for trips (e.g., "Japan Trip") with AA split summary

### 📊 Budget & Analysis
- **Budget Management** — Set monthly budgets per category with automatic overspend alerts
- **Financial Analysis & Advice** — Ask the bot for spending insights and saving tips
- **Weekly Summary** — Automated report pushed to all family members every Sunday

### 🤖 Proactive Engagement
- **Friday Evening Check-in** — Budget-aware weekend suggestions
- **Daily Budget Alerts** — Notification when any budget exceeds 80%
- **Memory-Augmented Reports** — Weekly summaries reference your stored goals and decisions

### 🔧 Infrastructure
- **MCP Tool Registry** — Pluggable tool architecture; add new skills by dropping a file (17 tools)
- **Multi-Provider LLM** — Switch between MiniMax, OpenAI, DeepSeek, Qwen, or any OpenAI-compatible API
- **Vector Embedding Support** — Optional LLM embedding API for semantic memory search
- **API Cost Control** — Monthly token limit with automatic fallback to regex parsing
- **CSV Export** — `/export` to download expense data
- **Docker Ready** — One-command deployment with docker-compose

## Architecture

```
User Message (text / photo)
     ↓
Telegram Bot (polling mode)
     ↓
Session Manager ── private vs group ──→ Persona Overlay
     ↓
MemoryManager (3-tier)
  ├── Tier 1: Core Profile   (DB: core_profiles, always injected)
  ├── Tier 2: Working Memory  (in-memory, recent turns)
  └── Tier 3: Episodic Memory (DB: episodic_memories + embedding BLOB)
       ├── Vector cosine search (if embedding model configured)
       └── FTS5 fallback (always available)
     ↓
PromptBuilder (dynamic assembly)
  ├── [System_Directive]    — persona & capabilities
  ├── [Time_Space_Anchor]   — current time/location/currency
  ├── [Core_Profile]        — user identity & goals
  ├── [Working_Context]     — conversation history
  ├── [Relevant_Context]    — top-K recalled episodes
  └── [Persona_Overlay]     — private/group adaptation
     ↓
LLM Agent (function calling / regex fallback)
  ├── Text → MCP tool dispatch
  └── Photo → Vision OCR → MCP tool dispatch
     ↓
MCP Tool Registry (auto-discover & dispatch, 17 tools)
  ├── expense_tools  (record, delete, export)
  ├── query_tools    (totals, summary, budget, analysis)
  ├── event_tools    (start, stop, event summary)
  └── memory_tools   (store, recall, forget, update_profile, get_profile)
     ↓
SQLite Database
  ├── expenses, budgets, events, api_usage
  ├── core_profiles (Tier 1)
  ├── episodic_memories + embedding BLOB (Tier 3)
  └── episodic_fts (FTS5 full-text index)
```

## Project Structure

```
family-finance-bot/
├── app/
│   ├── main.py              # Entry point
│   ├── telegram_bot.py      # Bot handlers, commands, scheduled jobs
│   ├── agent.py             # LLM agent v4: 3-tier memory + prompt factory
│   ├── prompt_builder.py    # Dynamic modular prompt assembly (NEW)
│   ├── llm_provider.py      # Abstract LLM + embedding interface
│   ├── memory.py            # 3-tier MemoryManager (NEW architecture)
│   ├── session.py           # Session management + working memory
│   ├── skills.py            # Core skill implementations
│   ├── api_tracker.py       # Token usage tracking and cost control
│   ├── scheduler.py         # Scheduled jobs: weekly report, nudge, alerts
│   ├── config.py            # Configuration from environment
│   ├── database.py          # SQLite init, 3-tier memory tables, FTS5
│   ├── mcp_tools/           # MCP-style pluggable tool registry
│   │   ├── registry.py      # Auto-discover + dispatch
│   │   ├── expense_tools.py # Record, delete, export (3 tools)
│   │   ├── query_tools.py   # Query, budget, analysis (6 tools)
│   │   ├── event_tools.py   # Event/trip management (3 tools)
│   │   └── memory_tools.py  # Memory + profile (5 tools, was 3)
│   ├── models/
│   │   └── expense.py       # Data models
│   └── services/
│       ├── expense_service.py   # Expense CRUD + CSV export
│       └── stats_service.py     # Statistics and query logic
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Quick Start

### Option A: Direct

```bash
git clone https://github.com/jinxiangGAN/family-finance-bot.git
cd family-finance-bot
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your tokens
python -m app.main
```

### Option B: Docker

```bash
git clone https://github.com/jinxiangGAN/family-finance-bot.git
cd family-finance-bot
cp .env.example .env
# Edit .env with your tokens
docker-compose up -d
```

## Configuration

### LLM Provider Examples

**MiniMax** (default):
```env
LLM_PROVIDER=minimax
LLM_API_KEY=your_minimax_key
LLM_MODEL=abab6.5s-chat
LLM_EMBEDDING_MODEL=embo-01    # Optional: enables vector memory
```

**OpenAI**:
```env
LLM_PROVIDER=openai
LLM_API_KEY=sk-xxx
LLM_MODEL=gpt-4o-mini
LLM_VISION_MODEL=gpt-4o
LLM_EMBEDDING_MODEL=text-embedding-3-small
```

**DeepSeek**:
```env
LLM_PROVIDER=deepseek
LLM_API_KEY=your_deepseek_key
LLM_MODEL=deepseek-chat
# No embedding model — uses FTS5 fallback
```

**Qwen (Alibaba)**:
```env
LLM_PROVIDER=qwen
LLM_API_KEY=your_qwen_key
LLM_MODEL=qwen-plus
LLM_VISION_MODEL=qwen-vl-plus
LLM_EMBEDDING_MODEL=text-embedding-v3
```

**Custom (any OpenAI-compatible)**:
```env
LLM_PROVIDER=custom
LLM_API_KEY=your_key
LLM_BASE_URL=https://your-endpoint.com/v1
LLM_MODEL=your-model
```

### Full Configuration Reference

| Variable | Required | Default | Description |
|:--|:--|:--|:--|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token from BotFather |
| `LLM_PROVIDER` | No | `minimax` | LLM provider: openai/minimax/deepseek/qwen/custom |
| `LLM_API_KEY` | No | — | LLM API key (falls back to regex without it) |
| `LLM_MODEL` | No | `abab6.5s-chat` | Model name for text processing |
| `LLM_VISION_MODEL` | No | same as LLM_MODEL | Model for receipt OCR |
| `LLM_EMBEDDING_MODEL` | No | — | Embedding model for vector memory (empty → FTS5 only) |
| `LLM_BASE_URL` | No | auto | Custom API endpoint |
| `LLM_MONTHLY_TOKEN_LIMIT` | No | `500000` | Monthly token cap (0 = unlimited) |
| `DATABASE_PATH` | No | `data/expenses.db` | SQLite file path |
| `ALLOWED_USER_IDS` | No | — | Comma-separated Telegram user IDs |
| `FAMILY_MEMBERS` | No | — | `uid:name` pairs, comma-separated |
| `TIMEZONE` | No | `Asia/Singapore` | Timezone for date calculations |
| `LOCATION` | No | `Singapore` | Location for Time-Space Anchor |
| `CURRENCY` | No | `SGD` | Default currency |
| `WEEKLY_SUMMARY_DAY` | No | `6` (Sunday) | Day for weekly report |
| `WEEKLY_SUMMARY_HOUR` | No | `20` | Hour for weekly report |
| `MEMORY_MAX_WORKING` | No | `10` | Max conversation turns in working memory |
| `MEMORY_RECALL_TOP_K` | No | `3` | Top-K episodic memories per query |

## Usage Examples

### Basic Expense Tracking

| Message | Result |
|:--|:--|
| `午饭 35` | Records 35 SGD under Food |
| `taxi 18` | Records 18 SGD under Transport |
| `lunch 50 CNY` | Records 50 CNY → auto-converts to SGD |

### Receipt OCR

Send a photo of any receipt, taxi screenshot, or food delivery bill. The bot will:
1. Use the vision model to extract items, amounts, and currency
2. Auto-categorize and record each expense
3. Reply with a summary of what was recorded

### 3-Tier Memory in Action

```
User: 我们决定年底去日本旅行
Bot:  好的，我记住了！我已更新你的目标：为年底日本旅行存钱。
      [Agent auto-called: update_user_profile(key='近期目标', value='年底日本旅行')]
      [Agent auto-called: store_memory(content='决定年底去日本旅行', category='goal')]

User: 打车 30
Bot:  ✅ 已记录：交通 30.00 SGD（打车）
      💡 提醒一下，你之前说过要存钱去日本旅行哦，打车能省则省？
      [Recalled from Tier 1 core_profile: 近期目标=年底日本旅行]
      [Recalled from Tier 3 episodic: "决定年底去日本旅行"]

User: 你还记得我们的目标吗？
Bot:  当然记得！你的近期目标是为年底日本旅行存钱。
      本月你还设定了减少外食、增加储蓄的计划。加油！💪
      [Tier 1 core_profile injected into every conversation]
```

### Private Chat vs Group Chat

```
[Private Chat — Warm, Empathetic]
Bot: 老婆，看了下这周餐饮预算还剩 200 SGD，
     周末想出去吃顿好的犒劳自己吗？🍽️
     对了，你之前说想减少外食，所以在家做也不错哦～

[Family Group Chat — Objective, Concise]
Bot: 📊 家庭本月支出：2,450.00 SGD
     餐饮 980 | 交通 520 | 生活 450 | 购物 300 | 其他 200
```

### Event/Trip Tags

```
User: 开始日本旅行
Bot:  ✅ 已开启事件标签「日本旅行」，后续记账将自动标记

User: 拉面 1500 日元
Bot:  ✅ 已记录 餐饮 1500.00 JPY (拉面) → 13.50 SGD [日本旅行]

User: 结束旅行
Bot:  ✅ 已关闭事件标签「日本旅行」

User: 日本旅行汇总
Bot:  📊 日本旅行
      👤 Husband: 450.00 SGD
      👫 Wife: 380.00 SGD
      💰 Total: 830.00 SGD
      📐 AA each: 415.00 SGD
```

### CSV Export

| Command | What it does |
|:--|:--|
| `/export` | Export your own expenses as CSV |
| `/export family` | Export all family expenses as CSV |

### Bot Commands

| Command | Description |
|:--|:--|
| `/start` | Welcome message |
| `/help` | Detailed usage guide |
| `/delete` | Delete most recent expense |
| `/export` | Export CSV file |
| `/usage` | Check LLM API token usage |
| `/memory` | View stored memories |

### Proactive Scheduled Messages

| Time | What happens |
|:--|:--|
| Friday 6PM | Budget-aware weekend check-in with personalized suggestions |
| Daily 9PM | Alert if any budget exceeds 80% |
| Sunday 8PM | Comprehensive weekly financial report with memory insights |

## Adding New Tools (MCP Pattern)

Create a new file in `app/mcp_tools/`, e.g. `calendar_tools.py`:

```python
"""MCP Tool: Calendar reminders."""

def _handle_set_reminder(user_id, user_name, params):
    # your implementation
    return {"success": True, "message": "Reminder set!"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a financial reminder (e.g., rent due date)",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Reminder text"},
                    "date": {"type": "string", "description": "Date (YYYY-MM-DD)"},
                },
                "required": ["text", "date"],
            },
        },
    },
]

HANDLERS = {
    "set_reminder": _handle_set_reminder,
}
```

The tool is automatically discovered and available to the LLM agent — no other code changes needed.

## Database Schema

### expenses
| Column | Type | Description |
|:--|:--|:--|
| id | INTEGER | Primary key |
| user_id | INTEGER | Telegram user ID |
| user_name | TEXT | Display name |
| category | TEXT | Expense category |
| amount | REAL | Original amount |
| currency | TEXT | Original currency code |
| amount_sgd | REAL | Converted SGD amount |
| note | TEXT | Description |
| event_tag | TEXT | Event/trip tag |
| created_at | TIMESTAMP | Creation time |

### core_profiles (Tier 1 — NEW)
| Column | Type | Description |
|:--|:--|:--|
| user_id | INTEGER | Telegram user ID |
| key | TEXT | Profile dimension (e.g., '近期目标', '饮食偏好') |
| value | TEXT | Description |
| updated_at | TIMESTAMP | Last update time |

### episodic_memories (Tier 3 — NEW)
| Column | Type | Description |
|:--|:--|:--|
| id | INTEGER | Primary key |
| user_id | INTEGER | Owner (0 = family-shared) |
| content | TEXT | Memory text |
| category | TEXT | preference/goal/decision/habit/reminder/general |
| importance | INTEGER | 1-10 scale |
| embedding | BLOB | Optional vector embedding (float32 packed) |
| created_at | TIMESTAMP | When it was stored |

> `episodic_fts` FTS5 virtual table provides full-text search fallback.

### budgets
| Column | Type | Description |
|:--|:--|:--|
| user_id | INTEGER | Telegram user ID |
| category | TEXT | Category or '_total' |
| monthly_limit | REAL | Monthly budget limit |

### events
| Column | Type | Description |
|:--|:--|:--|
| user_id | INTEGER | Telegram user ID |
| tag | TEXT | Event tag name |
| is_active | INTEGER | Currently active (1/0) |

### api_usage
| Column | Type | Description |
|:--|:--|:--|
| user_id | INTEGER | Telegram user ID |
| prompt_tokens | INTEGER | Prompt tokens used |
| completion_tokens | INTEGER | Completion tokens used |
| total_tokens | INTEGER | Total tokens |
| model | TEXT | Model name |
| created_at | TIMESTAMP | Timestamp |

## Deployment

### tmux
```bash
tmux new -s bot
python -m app.main
# Ctrl+B then D to detach
```

### systemd
```bash
sudo tee /etc/systemd/system/finance-bot.service << EOF
[Unit]
Description=Family Finance Telegram Bot
After=network.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory=/path/to/family-finance-bot
ExecStart=/path/to/python -m app.main
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now finance-bot
```

### Docker
```bash
docker-compose up -d

# View logs
docker-compose logs -f

# Restart
docker-compose restart

# Stop
docker-compose down
```

## License

MIT
