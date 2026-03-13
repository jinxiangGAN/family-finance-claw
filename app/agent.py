"""LLM Agent: uses MiniMax function calling to dispatch skills.

Flow:
1. User message → LLM (with tool definitions)
2. LLM returns tool_calls → execute skills → get results
3. Feed results back to LLM → LLM generates final human-readable reply
"""

import json
import logging
import re
from typing import Optional

import httpx

from app.api_tracker import is_within_limit, record_usage
from app.config import CURRENCY, MINIMAX_API_KEY, MINIMAX_MODEL
from app.skills import TOOL_DEFINITIONS, execute_skill

logger = logging.getLogger(__name__)

MINIMAX_API_URL = "https://api.minimax.chat/v1/text/chatcompletion_v2"

SYSTEM_PROMPT = f"""你是一个家庭记账助手机器人。这个家庭有两个人（夫妻）。
你的默认货币是 {CURRENCY}。

你可以帮助用户：
1. 记录日常支出（调用 record_expense）
2. 查询支出情况（调用 query_monthly_total / query_category_total / query_summary）
3. 设置和查看预算（调用 set_budget / query_budget）
4. 分析消费习惯并给出财务建议（调用 get_spending_analysis）
5. 删除误记的支出（调用 delete_last_expense）

回复规则：
- 用简洁友好的中文回复
- 金额后面带货币单位 {CURRENCY}
- 如果 skill 返回了 budget_alert，一定要在回复中提醒用户
- 对于财务建议，根据 get_spending_analysis 返回的数据给出具体可行的建议
- 如果用户的消息包含多笔消费，每笔都分别调用 record_expense
"""


async def agent_handle(
    text: str, user_id: int, user_name: str
) -> str:
    """Main agent entry: process user text and return a reply string.

    Falls back to simple regex if API is unavailable or over limit.
    """
    # Check API budget
    if not MINIMAX_API_KEY or not is_within_limit():
        if not MINIMAX_API_KEY:
            logger.info("No API key, using fallback")
        else:
            logger.warning("API token limit reached, using fallback")
        return _fallback_handle(text, user_id, user_name)

    try:
        return await _llm_agent_loop(text, user_id, user_name)
    except Exception:
        logger.exception("Agent LLM loop failed, falling back")
        return _fallback_handle(text, user_id, user_name)


async def _llm_agent_loop(text: str, user_id: int, user_name: str) -> str:
    """Run the LLM agent loop: call → tool_calls → execute → feed back → final reply."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]

    # Step 1: initial LLM call
    resp_msg, usage = await _call_minimax(messages)
    if usage:
        record_usage(user_id, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), usage.get("total_tokens", 0), MINIMAX_MODEL)

    # Step 2: if LLM wants to call tools, execute them
    tool_calls = resp_msg.get("tool_calls")
    if not tool_calls:
        # LLM replied directly (e.g., for casual chat)
        return resp_msg.get("content", "🤔 我没有理解你的意思，请输入 /help 查看帮助。")

    # Add assistant message with tool_calls
    messages.append(resp_msg)

    # Execute each tool call
    for tc in tool_calls:
        func = tc.get("function", {})
        skill_name = func.get("name", "")
        try:
            params = json.loads(func.get("arguments", "{}"))
        except json.JSONDecodeError:
            params = {}

        result = execute_skill(skill_name, user_id, user_name, params)
        logger.info("Skill %s → %s", skill_name, result)

        messages.append({
            "role": "tool",
            "tool_call_id": tc.get("id", ""),
            "content": json.dumps(result, ensure_ascii=False),
        })

    # Step 3: feed results back to LLM for final reply
    final_msg, usage2 = await _call_minimax(messages)
    if usage2:
        record_usage(user_id, usage2.get("prompt_tokens", 0), usage2.get("completion_tokens", 0), usage2.get("total_tokens", 0), MINIMAX_MODEL)

    return final_msg.get("content", "操作完成。")


async def _call_minimax(messages: list[dict]) -> tuple[dict, Optional[dict]]:
    """Call MiniMax chat completion with function calling. Returns (message, usage)."""
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MINIMAX_MODEL,
        "messages": messages,
        "tools": TOOL_DEFINITIONS,
        "temperature": 0.3,
        "max_tokens": 1024,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(MINIMAX_API_URL, headers=headers, json=payload)
        resp.raise_for_status()

    data = resp.json()
    logger.debug("MiniMax response: %s", json.dumps(data, ensure_ascii=False)[:500])

    message = data.get("choices", [{}])[0].get("message", {})
    usage = data.get("usage")
    return message, usage


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
    """Simple regex-based handler when LLM is unavailable."""
    text = text.strip()

    # Query patterns
    if "汇总" in text:
        scope = "family" if any(k in text for k in ("家庭", "总", "一共")) else "me"
        if any(k in text for k in ("老婆", "老公", "妻子", "丈夫")):
            scope = "spouse"
        result = execute_skill("query_summary", user_id, user_name, {"scope": scope})
        return _format_summary(result)

    if "花了多少" in text:
        scope = "me"
        if any(k in text for k in ("家庭", "总共", "一共")):
            scope = "family"
        elif any(k in text for k in ("老婆", "老公", "妻子", "丈夫")):
            scope = "spouse"
        # Check category
        from app.config import CATEGORIES
        cat = None
        for c in CATEGORIES:
            if c in text:
                cat = c
                break
        if cat:
            result = execute_skill("query_category_total", user_id, user_name, {"category": cat, "scope": scope})
            return f"📊 {result['label']}本月{result['category']}支出：{result['total']:.2f} {CURRENCY}"
        else:
            result = execute_skill("query_monthly_total", user_id, user_name, {"scope": scope})
            return f"📊 {result['label']}本月总支出：{result['total']:.2f} {CURRENCY}"

    if "预算" in text:
        if any(k in text for k in ("设", "改", "调")):
            return "⚠️ 设置预算请在 LLM 可用时使用，或使用 /help 查看帮助。"
        result = execute_skill("query_budget", user_id, user_name, {})
        return _format_budget(result)

    # Expense pattern
    m = _EXPENSE_RE.match(text)
    if m:
        note = m.group(1).strip()
        amount = float(m.group(2))
        category = _guess_category(note)
        result = execute_skill("record_expense", user_id, user_name, {
            "category": category, "amount": amount, "note": note
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
