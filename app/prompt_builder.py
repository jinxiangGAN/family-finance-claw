"""Dynamic Prompt Factory — modular prompt assembly per request.

Replaces the static global System Prompt with a PromptBuilder that assembles:
  [System_Directive]    — core persona & capabilities
  [Time_Space_Anchor]   — current time, location, currency (prevents hallucination)
  [Core_Profile]        — user's persistent identity & financial goals
  [Working_Context]     — recent conversation turns
  [Relevant_Context]    — top-K episodic memories recalled by similarity
  [Persona_Overlay]     — private/group chat tone adjustment

Inspired by OpenClaw's prompt composition architecture.
"""

import logging
from datetime import datetime

from zoneinfo import ZoneInfo

from app.config import CATEGORIES, CURRENCY, FAMILY_MEMBERS, LOCATION, TIMEZONE

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
#  Prompt Sections
# ═══════════════════════════════════════════

SYSTEM_DIRECTIVE = """\
你是一个智能家庭财务管家机器人。你不只是一个记账工具，更是一个了解这个家庭、能给出个性化财务建议的贴心助手。
这个家庭有两个人（夫妻）。

你的能力：
1. 记录日常支出（record_expense），支持多币种自动折算
2. 查询支出（query_monthly_total / query_category_total / query_summary）
3. 预算管理（set_budget / query_budget）
4. 消费分析与财务建议（get_spending_analysis）
5. 删除误记（delete_last_expense）
6. 事件/旅行标签（start_event / stop_event / query_event_summary）
7. 导出 CSV（export_csv）
8. 长期记忆管理（store_memory / recall_memories / forget_memory）
9. 更新用户画像（update_user_profile）— 当你察觉到用户的财务目标或偏好变化时主动调用

回复规则：
- 用简洁友好的中文回复
- 金额后面带货币单位
- 如果用户用非默认货币记账，回复中提示已自动折算
- 如果 skill 返回了 budget_alert，一定要在回复中提醒用户
- 如果用户的消息包含多笔消费，每笔都分别调用 record_expense

记忆规则：
- 当用户表达偏好（如"我不喜欢在外面吃"）、设定目标（如"这个月减少打车"）、做出家庭决定时，主动调用 store_memory
- 当你察觉到用户的核心目标或长期偏好发生变化时，调用 update_user_profile 更新画像
- 回复中自然地引用记忆，不要刻意强调"我的记忆显示..."
- 当用户的消费与记忆中的目标冲突时（如说过要减少打车但又打车了），温和地提醒"""


PERSONA_PRIVATE = """\

[对话场景] 私聊 · {display_name}
[回复风格] 温暖、贴心、像一个懂你的朋友。
- 称呼用户为「{display_name}」
- 可以主动关心消费习惯，给出个性化理财建议
- 适当用 emoji 和口语化表达
- 可以引用用户画像中的目标来鼓励或提醒
- 如果发现好的省钱机会，主动建议"""


PERSONA_GROUP = """\

[对话场景] 家庭群聊
[回复风格] 客观、简洁、中立、只播报数字。
- 使用家庭视角，不偏向任何一方
- 不在群里展示个人消费细节或个人目标
- 不进行个人说教
- 简明扼要地回答，数字为主
- 保护每个成员的消费隐私"""


VISION_PROMPT = f"""\
你是一个 OCR 助手。请识别这张图片中的消费信息。

提取以下信息并返回严格的 JSON（不要包含其他文字）：
[{{"category": "分类", "amount": 金额, "note": "备注", "currency": "货币代码"}}]

可选分类：{", ".join(CATEGORIES)}
默认货币：{CURRENCY}
如果无法识别，返回：[{{"error": "无法识别"}}]"""


# ═══════════════════════════════════════════
#  PromptBuilder
# ═══════════════════════════════════════════

class PromptBuilder:
    """Assembles a complete system prompt from modular sections.

    Usage:
        builder = PromptBuilder()
        prompt = builder.build(
            user_id=123,
            is_private=True,
            memory_context="...",  # from MemoryManager.assemble_memory_context()
        )
    """

    def build(
        self,
        user_id: int,
        is_private: bool,
        memory_context: str = "",
    ) -> str:
        """Assemble the full system prompt for a single LLM call."""
        sections: list[str] = []

        # 1. System Directive (core persona)
        sections.append(SYSTEM_DIRECTIVE)

        # 2. Time-Space Anchor (ground truth injection)
        sections.append(self._time_space_anchor())

        # 3. Memory context (assembled by MemoryManager: profile + working + episodic)
        if memory_context:
            sections.append(memory_context)

        # 4. Persona overlay (private vs group)
        sections.append(self._persona_overlay(user_id, is_private))

        return "\n\n".join(sections)

    def build_vision(self) -> str:
        """Return the vision/OCR system prompt."""
        sections = [VISION_PROMPT, self._time_space_anchor()]
        return "\n\n".join(sections)

    # ─── Private section builders ───

    @staticmethod
    def _time_space_anchor() -> str:
        """Inject absolute time and location to prevent temporal hallucination."""
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        return (
            f"[时空锚点]\n"
            f"Current location: {LOCATION}\n"
            f"Current date/time: {now.strftime('%Y-%m-%d %H:%M %A')} ({TIMEZONE})\n"
            f"Default currency: {CURRENCY}\n"
            f"Family members: {', '.join(f'{v}(id:{k})' for k, v in FAMILY_MEMBERS.items()) or 'not configured'}"
        )

    @staticmethod
    def _persona_overlay(user_id: int, is_private: bool) -> str:
        """Generate persona section based on chat context."""
        if is_private:
            display_name = FAMILY_MEMBERS.get(user_id, "你")
            return PERSONA_PRIVATE.format(display_name=display_name)
        else:
            return PERSONA_GROUP
