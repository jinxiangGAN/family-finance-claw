# Household Finance Semantic Map

This document captures the high-frequency household finance utterances that `小灰毛` should understand reliably.

The goal is not to hard-code every sentence. The goal is to normalize the most common semantic patterns so Codex can interpret naturally while the action surface stays stable.

## Core principle

1. Codex should perform the first-pass understanding.
2. Resident actions should execute the normalized intent.
3. Regex and narrow parsers should only stabilize obvious high-frequency patterns, not replace contextual understanding.

## Intent families

### 1. Expense recording

Canonical intent:
- record one expense

Common utterances:
- `午饭 9.8`
- `晚餐30`
- `记一笔，咖啡 6`
- `小白午饭 9.8`
- `给小白记一笔，午饭 9.8`
- `午饭 9.8，小白花的`

Normalization rules:
- If no different owner is explicitly named, default the owner to the current sender.
- If `小白` / `小鸡毛` is explicitly named, pass `owner_user_id` and `owner_user_name`.
- Plain expense turns should be:
  - understand once
  - `record_expense` once
  - final reply

### 2. Budget writing

Canonical intent:
- create or update a budget

Common utterances:
- `房租预算 4500`
- `房租预算 4500元`
- `房租预算设为4500`
- `设置餐饮、交通、超市三项共用预算 2000`
- `三项预算共3000`

Normalization rules:
- Budget-like utterances should never be greedily treated as expense recording.
- Budget writes should go through Codex contextual understanding first.
- Grouped budgets are still budgets, not a separate user-facing concept.

### 3. Budget querying

Canonical intent:
- query current budget status

Common utterances:
- `看看预算`
- `当前预算`
- `有哪些预算`
- `房租预算是多少`
- `三项日常预算怎么样`

Normalization rules:
- Query replies should merge:
  - single-category budgets
  - family total budget
  - grouped budgets
- The user should not need to know the storage split between `budgets` and `budget_groups`.

### 4. Totals

Canonical intent:
- query today/month total

Common utterances:
- `今天花了多少`
- `查看今天花费`
- `查看今天全家的花费`
- `本月花了多少`
- `小白今天花了多少`
- `小鸡毛本月花了多少`

Normalization rules:
- Resolve scope correctly:
  - `me`
  - `spouse`
  - `family`
- `全家` / `家庭` / `我们` should map to `family`.
- Named-member phrasing should resolve relative to the current sender.

### 5. Details

Canonical intent:
- query itemized expense details

Common utterances:
- `小白的花费细则`
- `查看小白的花费明细`
- `全家花费细则`
- `餐饮明细`
- `小鸡毛餐饮明细`

Normalization rules:
- `明细` / `细则` should map to detail queries, not free-form chat.
- If a category is named, prefer category detail view.
- Otherwise, use recent detailed records with the resolved scope.

### 6. Follow-up scope correction

Canonical intent:
- modify the scope of the immediately previous query

Common utterances:
- `包括小白的`
- `算上小白`
- `加上小鸡毛`
- `带上小白`

Normalization rules:
- These are follow-up modifiers, not standalone finance actions.
- If the previous user turn was a `today total` query, rewrite to a family `today total`.
- If the previous user turn was a `month total` query, rewrite to a family `month total`.

### 7. Deletion

Canonical intent:
- delete a specific expense safely

Common utterances:
- `删除 #123`
- `删除 15 块午饭那笔`
- `删掉这笔`

Normalization rules:
- Direct deletion should prefer id-based deletion.
- Natural-language delete hints should first produce candidates, then ask for the id.

### 8. Family forwarding

Canonical intent:
- send a message to the other family member

Common utterances:
- `给小白发：今晚我晚点回家`
- `帮我给小白发消息，我想你啦`
- `跟小鸡毛说记得买牛奶`

Normalization rules:
- Codex should understand the delivery intent first.
- Delivery should use the resident family action surface.
- The final reply should distinguish:
  - accepted for delivery
  - actually delivered
  - blocked due to transport/user route issues

### 9. Voice and image expense turns

Canonical intent:
- turn speech/image into one finance action

Common utterances:
- short voice note for one expense
- receipt photo
- bill screenshot

Normalization rules:
- Voice:
  - transcribe first
  - show the transcription
  - then run the normal text flow
- Image:
  - inspect once
  - record one expense if recognizable
  - otherwise ask a brief follow-up or say it was not stable enough

## Stability heuristics

These patterns should be treated as high-risk for greedy parsing:

- budget-like phrasing with amounts
- grouped budget phrasing
- follow-up scope corrections
- spouse/family disambiguation
- image receipt turns
- writes that can modify existing state

These patterns are safer for resident fast paths:

- recent expenses
- today total
- month total
- exchange rate
- budget query
- detail query
- delete by explicit id
- family forwarding

## Product rule

If a sentence is semantically simple but context-sensitive, the right answer is:

- let Codex understand it first
- but constrain execution to one stable resident action whenever possible

That is the target shape for `小灰毛`.
