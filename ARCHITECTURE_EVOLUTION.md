# Architecture Evolution: API Bot to Codex Agent Service

This document explains why the project changed, what changed, and how to talk about the system clearly during review, retrospectives, or interviews.

## 1. Original State

The project started as a Telegram finance bot built around a remote LLM API.

The old shape was roughly:

```text
Telegram
-> prompt builder
-> remote LLM API
-> function calling / tool dispatch
-> SQLite
```

That version already had useful ideas:
- expense tracking
- tool-based business logic
- memory
- summaries
- Telegram UX

But the central intelligence still depended on an external API model workflow.

## 2. Why Move Away from the API-Centric Design

The shift away from the hosted API path came from several practical issues.

### 2.1 Limited control over tool behavior

The finance bot needed the model to:
- use existing repo skills
- stay grounded in SQLite
- avoid hallucinating numbers
- manage memory carefully

With a hosted API-first loop, the business logic and the model loop were more loosely coupled than desired.

### 2.2 Memory quality problems

There were issues such as:
- memory not updating reliably
- vague memory extraction
- memory mixing personal and family context
- factual replies that were not always clearly grounded in the database

### 2.3 Need for a more agentic local workflow

The project was becoming less like “a prompt calling an API” and more like:
- a repo with business logic
- a database
- a tool layer
- a bot interface
- an agent orchestrator

At that point, local Codex became a better fit than a pure hosted API loop.

## 3. The New Direction

The repo was reoriented around this idea:

```text
Telegram Bot
-> local Codex
-> bridge_ops
-> skills
-> SQLite
```

In this design:
- Telegram is just the messaging surface
- Codex is the reasoning/orchestration layer
- `bridge_ops` is the controlled execution gateway
- skills remain the business layer
- SQLite remains the source of truth

## 4. Major Evolution Steps

### Step 1: Replace Gemini/API-first runtime with local Codex bridge

The first big change was replacing the remote provider-driven agent loop with a local Codex bridge.

This changed the system from:
- API-oriented orchestration

to:
- repo-oriented orchestration

That matters because Codex can reason over the actual workspace and invoke the existing code paths directly.

### Step 2: Tighten fact grounding

The next goal was reducing hallucination risk.

The project moved toward:
- bridge prompt constraints
- `app.bridge_ops` as the preferred path
- database snapshot injection
- stronger rules around finance and memory answers

The core idea became:

> finance facts should come from the database, not from model imagination

### Step 3: Improve memory discipline

Memory moved from a looser “agent may decide to store it” style to a stricter lifecycle:

- detect possible memory
- ask for confirmation
- rewrite to English
- store in the database
- support archive
- support update/versioning

This solved several problems:
- accidental memory writes
- inconsistent language in stored memory
- no clear way to revise outdated memory

### Step 4: Separate household finance concerns

The repo also matured at the domain level:
- personal vs family identities
- regular vs special spending
- budget current state vs budget history
- budget alert deduplication
- special plans for trips or projects

This made the bot more suitable for real household use instead of a demo-style ledger bot.

### Step 5: Move toward resident session architecture

The next bottleneck was performance and continuity.

One-shot `codex exec` on every Telegram turn works, but it is not the ideal end-state for a true Telegram agent.

So the project moved to this shape:

```text
Telegram
-> handlers
-> resident agent service
-> Codex session manager
-> Codex thread resume
```

This introduced:
- assistant registry
- session manager keyed by assistant/user/chat
- persistent Codex thread ids
- session store for chat-to-thread mapping

This is a key architectural shift:

> the system is no longer just “calling Codex”; it is starting to manage Codex as an application runtime

### Step 5.5: Move from persistent threads toward a resident Codex process

After thread persistence was added, the next real-world bottleneck became obvious:

- feedback still felt slow
- database-related turns still paid per-turn CLI startup cost
- the owner explicitly wanted Codex to be truly resident, not just session-aware

That led to the next step:

```text
Telegram
-> resident agent service
-> Codex session manager
-> resident codex app-server
-> bridge_ops
-> skills
-> SQLite
```

This is an important distinction:

- persistent thread means continuity of context
- persistent process means lower turn startup overhead and a more agent-like runtime

The repo now prefers a resident `codex app-server` runtime and falls back to `exec/resume` only when necessary.

### Step 6: Prepare for multi-assistant orchestration

The final direction was not just “make one finance bot better”.

It was:

> keep the current repo working, but shape it so one outer Codex service can later route multiple assistants across multiple repos

That led to:
- `AssistantConfig`
- `AssistantRegistry`
- `AssistantRouter`
- explicit `assistant_id`
- configurable registry loading from JSON

This is future-facing architecture work, even though only one assistant is active today.

## 5. Current Architecture

Today the practical architecture is:

```text
Telegram
-> app/bot/handlers.py
-> assistant router
-> resident agent service
-> codex session manager
-> resident codex app-server (preferred)
-> codex exec / codex exec resume (fallback)
-> app.bridge_ops
-> skills
-> SQLite
```

Important points:

- Telegram does not own business logic.
- Finance logic is still implemented in services/skills.
- Codex is the orchestrator.
- SQLite is the factual store.
- Session continuity is tied to Codex thread ids.
- Runtime residency is now tied to a long-lived `codex app-server` process when available.

## 6. What Improved Because of This Evolution

### Better control

The repo now controls more of the real workflow:
- how facts are read
- how writes are confirmed
- how memory is stored
- how special spending is handled

### Better product safety

The system is stricter about:
- memory confirmation
- high-risk action confirmation
- budget alert spam
- deleting records safely
- distinguishing regular vs special spend

### Better extensibility

The current codebase is much closer to a reusable agent platform.

It is no longer just:
- “a Telegram bot with some prompts”

It is closer to:
- “a Codex-powered orchestration service with a finance assistant implementation”

## 7. What Is Still Not Final

The project has moved a long way, but it is useful to be clear about what is still transitional.

### 7.1 Resident runtime is now present, but still maturing

The system now has a real resident runtime path through `codex app-server`.

That is a major step up from pure per-turn `exec`, but it is still not the same as:
- one forever-running interactive subprocess per chat
- a complete outer multi-repo gateway service

### 7.2 Multi-assistant outer gateway is prepared, not fully built

The repo now has:
- assistant registry
- assistant routing
- config-based registry loading

But the external orchestration layer that truly manages multiple repos from one top-level service is still future work.

## 8. How To Explain This In an Interview

A good concise version is:

> I started with a Telegram finance bot built around a hosted LLM API, but as the product matured I needed tighter control over tool usage, memory, and database grounding. I migrated the system to a local Codex-driven architecture where Telegram is only the interface, Codex is the orchestration layer, `bridge_ops` is the controlled execution gateway, and SQLite is the source of truth. Then I introduced session persistence, thread resume, assistant routing, and a registry abstraction so the system can evolve from a single finance bot into a multi-assistant Codex service across multiple repositories.

If you want a more engineering-heavy version:

> The core change was moving from prompt-centric API orchestration to repo-centric agent orchestration. That let me reuse existing business logic directly, reduce hallucination risk by grounding finance actions in the database, implement explicit memory lifecycle controls, and prepare the runtime for persistent session management and future multi-assistant routing.

## 9. Key Takeaway

The important story is not just:

> “I replaced Gemini with Codex.”

The real story is:

> “I turned a Telegram bot that depended on a hosted model API into a more controlled, database-grounded, extensible agent system with session continuity and a path to multi-assistant orchestration.”
