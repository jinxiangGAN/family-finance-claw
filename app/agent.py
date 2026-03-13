"""LLM Agent v4: 3-tier memory + dynamic prompt factory + session-aware.

Architecture (inspired by OpenClaw):
  1. User message → Session tracking (working memory + persona)
  2. MemoryManager.assemble_memory_context → Tier 1/2/3 recall
  3. PromptBuilder.build → modular system prompt assembly
  4. LLM call with MCP tools → execute tool calls
  5. Post-turn: update working memory buffer
  6. Return final reply (tone adapted to private/group chat)
"""

import json
import logging
import re
from typing import Optional

from app.api_tracker import is_within_limit, record_usage
from app.config import (
    CURRENCY,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_EMBEDDING_MODEL,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_VISION_MODEL,
    MEMORY_RECALL_TOP_K,
)
from app.llm_provider import PROVIDER_PRESETS, create_provider
from app.mcp_tools.registry import execute_tool, get_all_tools
from app.memory import MemoryManager, set_memory_manager
from app.prompt_builder import VISION_PROMPT, PromptBuilder
from app.session import Session

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
#  Singleton initialization
# ═══════════════════════════════════════════

_provider = None
_vision_provider = None
_memory_manager: Optional[MemoryManager] = None
_prompt_builder: Optional[PromptBuilder] = None


def _get_provider():
    global _provider
    if _provider is None and LLM_API_KEY:
        _provider = create_provider(LLM_PROVIDER, LLM_API_KEY, LLM_MODEL, LLM_BASE_URL)
    return _provider


def _get_vision_provider():
    global _vision_provider
    if _vision_provider is None and LLM_API_KEY:
        vision_model = LLM_VISION_MODEL or LLM_MODEL
        _vision_provider = create_provider(LLM_PROVIDER, LLM_API_KEY, vision_model, LLM_BASE_URL)
    return _vision_provider


def _get_memory_manager() -> MemoryManager:
    """Get or initialize the global MemoryManager with embedding support."""
    global _memory_manager
    if _memory_manager is None:
        provider = _get_provider()
        # Determine embedding model
        embedding_model = LLM_EMBEDDING_MODEL
        if not embedding_model:
            preset = PROVIDER_PRESETS.get(LLM_PROVIDER, {})
            embedding_model = preset.get("embedding_model", "")
        _memory_manager = MemoryManager(provider=provider, embedding_model=embedding_model)
        set_memory_manager(_memory_manager)  # register globally for legacy code paths
        logger.info(
            "MemoryManager initialized (provider=%s, embedding=%s)",
            LLM_PROVIDER if provider else "none",
            embedding_model or "FTS5-only",
        )
    return _memory_manager


def _get_prompt_builder() -> PromptBuilder:
    global _prompt_builder
    if _prompt_builder is None:
        _prompt_builder = PromptBuilder()
    return _prompt_builder


# ═══════════════════════════════════════════
#  Main entry: text messages
# ═══════════════════════════════════════════

async def agent_handle(text: str, user_id: int, user_name: str, session: Session) -> str:
    """Main agent entry: memory-augmented, session-aware processing."""
    if not LLM_API_KEY or not is_within_limit():
        if not LLM_API_KEY:
            logger.info("No API key, using fallback")
        else:
            logger.warning("API token limit reached, using fallback")
        return _fallback_handle(text, user_id, user_name)

    try:
        return await _llm_agent_loop(text, user_id, user_name, session)
    except Exception:
        logger.exception("Agent LLM loop failed, falling back")
        return _fallback_handle(text, user_id, user_name)


async def _llm_agent_loop(text: str, user_id: int, user_name: str, session: Session) -> str:
    provider = _get_provider()
    if provider is None:
        return _fallback_handle(text, user_id, user_name)

    mm = _get_memory_manager()
    builder = _get_prompt_builder()

    # Step 1: Assemble memory context (all 3 tiers)
    memory_context = await mm.assemble_memory_context(user_id, text)

    # Step 2: Build dynamic system prompt
    system_prompt = builder.build(
        user_id=user_id,
        is_private=session.is_private,
        memory_context=memory_context,
    )

    # Step 3: Build messages (include working memory as conversation history)
    messages = [{"role": "system", "content": system_prompt}]

    # Inject working memory turns for multi-turn coherence
    wm = mm.get_working_memory(user_id)
    for past_msg in wm.get_messages():
        messages.append(past_msg)

    # Current user message
    messages.append({"role": "user", "content": text})

    # Step 4: Get MCP tools & call LLM
    tools = get_all_tools()

    resp_msg, usage = await provider.chat_completion(messages, tools=tools)
    if usage:
        record_usage(
            user_id,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
            LLM_MODEL,
        )

    tool_calls = resp_msg.get("tool_calls")
    if not tool_calls:
        reply = resp_msg.get("content", "🤔 我没有理解你的意思，请输入 /help 查看帮助。")
        # Update working memory
        mm.add_working_turn(user_id, "user", text)
        mm.add_working_turn(user_id, "assistant", reply)
        return reply

    # Step 5: Execute tool calls (via MCP registry)
    messages.append(resp_msg)

    for tc in tool_calls:
        func = tc.get("function", {})
        tool_name = func.get("name", "")
        try:
            params = json.loads(func.get("arguments", "{}"))
        except json.JSONDecodeError:
            params = {}

        result = execute_tool(tool_name, user_id, user_name, params)
        logger.info("Tool %s → %s", tool_name, json.dumps(result, ensure_ascii=False)[:200])

        messages.append({
            "role": "tool",
            "tool_call_id": tc.get("id", ""),
            "content": json.dumps(result, ensure_ascii=False),
        })

    # Step 6: Final LLM call for human-readable reply
    final_msg, usage2 = await provider.chat_completion(messages, tools=tools)
    if usage2:
        record_usage(
            user_id,
            usage2.get("prompt_tokens", 0),
            usage2.get("completion_tokens", 0),
            usage2.get("total_tokens", 0),
            LLM_MODEL,
        )

    reply = final_msg.get("content", "操作完成。")

    # Step 7: Update working memory buffer
    mm.add_working_turn(user_id, "user", text)
    mm.add_working_turn(user_id, "assistant", reply)

    return reply


# ═══════════════════════════════════════════
#  Image (Receipt OCR) handling
# ═══════════════════════════════════════════

async def agent_handle_image(
    image_url: str, caption: str, user_id: int, user_name: str
) -> str:
    """Handle an image message: OCR → record expenses."""
    if not LLM_API_KEY or not is_within_limit():
        return "📷 收据识别需要 LLM API，当前不可用。请手动输入记账信息。"

    vision = _get_vision_provider()
    if vision is None:
        return "📷 Vision 模型未配置，无法识别收据。"

    try:
        builder = _get_prompt_builder()
        vision_prompt = builder.build_vision()
        prompt = caption.strip() if caption else "请识别这张图片中的消费信息"

        content, usage = await vision.chat_completion_with_image(
            text=prompt,
            image_url=image_url,
            system_prompt=vision_prompt,
        )
        if usage:
            record_usage(
                user_id,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
                LLM_MODEL,
            )

        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        items = json.loads(content)
        if not isinstance(items, list):
            items = [items]

        if items and items[0].get("error"):
            return f"📷 无法识别图片中的消费信息：{items[0]['error']}\n请手动输入。"

        replies = []
        for item in items:
            result = execute_tool("record_expense", user_id, user_name, {
                "category": item.get("category", "其他"),
                "amount": item.get("amount", 0),
                "note": item.get("note", "收据"),
                "currency": item.get("currency", CURRENCY),
            })
            if result.get("success"):
                cur = result.get("currency", CURRENCY)
                line = f"✅ {result['category']}  {result['amount']:.2f} {cur}"
                if result.get("note"):
                    line += f"（{result['note']}）"
                if result.get("amount_sgd") and cur != CURRENCY:
                    line += f" → {result['amount_sgd']:.2f} {CURRENCY}"
                replies.append(line)
                if result.get("budget_alert"):
                    replies.append(result["budget_alert"])

        if replies:
            return "📷 收据识别成功！\n\n" + "\n".join(replies)
        return "📷 未能从图片中提取到消费信息，请手动输入。"

    except Exception:
        logger.exception("Receipt OCR failed")
        return "📷 收据识别失败，请手动输入记账信息。"


# ═══════════════════════════════════════════
#  CSV export helper
# ═══════════════════════════════════════════

async def agent_handle_export(user_id: int, user_name: str, scope: str = "me") -> Optional[str]:
    """Handle /export command. Returns CSV content or None."""
    result = execute_tool("export_csv", user_id, user_name, {"scope": scope})
    if result.get("success"):
        return result.get("csv_content", "")
    return None


# ═══════════════════════════════════════════
#  Regex fallback (when LLM is unavailable)
# ═══════════════════════════════════════════

_EXPENSE_RE = re.compile(r"^(.+?)\s*(\d+(?:\.\d+)?)\s*元?$")

_CATEGORY_KEYWORDS: dict[str, str] = {
    "饭": "餐饮", "餐": "餐饮", "吃": "餐饮", "食": "餐饮",
    "奶茶": "餐饮", "咖啡": "餐饮", "外卖": "餐饮", "零食": "餐饮",
    "车": "交通", "地铁": "交通", "公交": "交通", "打车": "交通",
    "买": "购物", "购": "购物", "超市": "购物",
    "电影": "娱乐", "游戏": "娱乐",
    "水电": "生活", "房租": "生活", "话费": "生活",
    "药": "医疗", "医": "医疗",
}


def _guess_category(note: str) -> str:
    for keyword, cat in _CATEGORY_KEYWORDS.items():
        if keyword in note:
            return cat
    return "其他"


def _fallback_handle(text: str, user_id: int, user_name: str) -> str:
    text = text.strip()

    if "汇总" in text:
        scope = "family" if any(k in text for k in ("家庭", "总", "一共")) else "me"
        if any(k in text for k in ("老婆", "老公", "妻子", "丈夫")):
            scope = "spouse"
        result = execute_tool("query_summary", user_id, user_name, {"scope": scope})
        return _format_summary(result)

    if "花了多少" in text:
        scope = "me"
        if any(k in text for k in ("家庭", "总共", "一共")):
            scope = "family"
        elif any(k in text for k in ("老婆", "老公", "妻子", "丈夫")):
            scope = "spouse"
        from app.config import CATEGORIES
        cat = None
        for c in CATEGORIES:
            if c in text:
                cat = c
                break
        if cat:
            result = execute_tool("query_category_total", user_id, user_name, {"category": cat, "scope": scope})
            return f"📊 {result['label']}本月{result['category']}支出：{result['total']:.2f} {CURRENCY}"
        else:
            result = execute_tool("query_monthly_total", user_id, user_name, {"scope": scope})
            return f"📊 {result['label']}本月总支出：{result['total']:.2f} {CURRENCY}"

    if "预算" in text:
        if any(k in text for k in ("设", "改", "调")):
            return "⚠️ 设置预算请在 LLM 可用时使用，或使用 /help 查看帮助。"
        result = execute_tool("query_budget", user_id, user_name, {})
        return _format_budget(result)

    m = _EXPENSE_RE.match(text)
    if m:
        note = m.group(1).strip()
        amount = float(m.group(2))
        category = _guess_category(note)
        result = execute_tool("record_expense", user_id, user_name, {
            "category": category, "amount": amount, "note": note,
        })
        reply = f"✅ 已记录\n{result['category']}  {result['amount']:.2f} {CURRENCY}"
        if result.get("note"):
            reply += f"（{result['note']}）"
        if result.get("budget_alert"):
            reply += f"\n\n{result['budget_alert']}"
        return reply

    return "🤔 无法识别您的消息。请输入记账信息或查询指令，输入 /help 查看帮助。"


def _format_summary(result: dict) -> str:
    summary = result.get("summary", [])
    if not summary:
        return f"📊 {result['label']}本月暂无支出记录。"
    lines = [f"📊 {result['label']} · 本月支出汇总\n"]
    for item in summary:
        lines.append(f"  {item['category']}：{item['total']:.2f} {CURRENCY}")
    lines.append(f"\n💰 合计：{result['grand_total']:.2f} {CURRENCY}")
    return "\n".join(lines)


def _format_budget(result: dict) -> str:
    budgets = result.get("budgets", [])
    if not budgets:
        return "📋 尚未设置任何预算。"
    lines = ["📋 预算使用情况\n"]
    for b in budgets:
        status = "🔴 超支" if b["over_budget"] else "🟢 正常"
        lines.append(
            f"  {b['category']}：{b['spent']:.2f}/{b['monthly_limit']:.2f} {CURRENCY} "
            f"（剩余 {b['remaining']:.2f}）{status}"
        )
    return "\n".join(lines)
