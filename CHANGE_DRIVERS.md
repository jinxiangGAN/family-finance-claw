# Change Drivers

This document records not just what changed, but why the owner asked for the change. It is intentionally product-driven rather than commit-driven.

## Core Direction

### Move from API bot to local Codex

Why it was requested:
- the bot should stop depending on Gemini/API calls
- Telegram should hand messages to local Codex
- Codex should reuse repo skills and the SQLite database directly

Outcome:
- the main path moved from hosted LLM API orchestration to local Codex orchestration
- `bridge_ops` became the controlled gateway into finance/database actions

## Safety and Grounding

### Stronger database grounding

Why it was requested:
- memory updates were unreliable
- factual replies could hallucinate
- the owner wanted answers to be based on the database

Outcome:
- finance/memory answers were pushed toward database-first handling
- prompts, `AGENTS.md`, and `bridge_ops` constraints were tightened

### Confirmation for risky actions

Why it was requested:
- some actions should not fire accidentally
- deleting records, changing budgets, and editing memory are high-cost mistakes

Outcome:
- high-risk writes now ask for confirmation
- ordinary short expense recording remains fast and does not require confirmation

## Household Model

### Separate the two people, but keep shared family visibility

Why it was requested:
- the owner and spouse should have separate identities
- they should still be able to query each other and family totals
- group chat context should feel family-level, while private chat should feel personal

Outcome:
- expenses stay tied to per-user ids
- personal and family memory/profile scopes were separated
- personal thread vs family thread behavior was introduced

### Friendly naming and persona

Why it was requested:
- the assistant should feel like a household member, not a generic bot
- `小灰毛` should talk in a warmer, livelier, more human way
- household roles should map to `小鸡毛` and `小白`

Outcome:
- the soul file and runtime prompt were updated
- display names and household naming became part of the product identity

## Finance Product Fit

### Better budgets and alerts

Why it was requested:
- budgets need both current state and history
- repeated rent reminders were annoying

Outcome:
- current budget, budget change history, and alert deduplication were added
- “already reached budget” categories no longer nag every day

### Special/project spending

Why it was requested:
- not all spending should count as regular household consumption
- travel planning needed to track flights, visas, and other pre-trip costs separately

Outcome:
- `regular` vs `special` spending scopes were introduced
- special plans gained `planning / active / closed`
- summaries can exclude special spending from regular monthly behavior

### Safer deletion flow

Why it was requested:
- deleting “that 15-dollar lunch” directly is risky
- the owner wanted a Telegram-friendly but safer delete workflow

Outcome:
- the bot can surface candidate matches
- real deletion still happens through a specific record id

## Memory Lifecycle

### Confirm, store in English, then allow archive/update

Why it was requested:
- memory should not be auto-written without confirmation
- memory storage should be English, even if conversation is Chinese
- stale memories need revision rather than only hard deletion

Outcome:
- candidate memory is detected first
- user confirms
- stored memory is rewritten into English
- archive/update/versioning was added

## UX and Operability

### Better help and discoverability

Why it was requested:
- some Telegram commands are hard coded
- users should not need to memorize all commands

Outcome:
- `/help` was expanded
- natural-language help triggers were added

### Clearer evidence of code changes

Why it was requested:
- when using Telegram to ask for code changes, it was hard to tell whether a file was really modified

Outcome:
- confirmation and reporting around risky actions were improved
- the architecture was moved toward clearer resident service abstractions

## Deployment and Runtime

### Cloud-friendly paths and VM deployment

Why it was requested:
- absolute paths tied to one machine would break on cloud deployment
- the owner wanted an easy VM deployment path

Outcome:
- path handling was made environment-aware
- Docker and local VM instructions were cleaned up

### True resident Codex service

Why it was requested:
- the owner felt the bot was too slow
- the main pain became latency, especially for database-related interactions
- the stated product goal is a Telegram bot backed by a true resident agent service
- the long-term goal is one Codex outer layer serving multiple assistants across multiple repos

Outcome:
- session management, assistant registry, and routing were introduced first
- then the runtime moved from pure per-turn `exec` toward a resident `codex app-server` process
- current design now prefers resident `app-server` and falls back to `exec/resume` when necessary

### Faster simple finance turns

Why it was requested:
- after the resident runtime work, the biggest remaining pain was still latency
- the owner pointed out that many turns are simple and should not need a heavy agent loop

Outcome:
- a simple-finance fast path was added
- common turns now use a much smaller Codex prompt
- the allowed tool path is narrowed to one expected skill for common expense and budget actions

## Documentation

### Keep the architectural story reviewable

Why it was requested:
- the owner wanted a clear record for review and interview preparation
- it should explain not only what changed, but the reasoning behind the evolution

Outcome:
- `README.md` was rewritten around the current architecture
- `ARCHITECTURE_EVOLUTION.md` was added
- this document was added to preserve the “why” behind each major change
