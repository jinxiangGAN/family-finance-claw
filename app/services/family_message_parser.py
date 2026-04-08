"""Shared helpers for family message forwarding requests."""

from __future__ import annotations

from typing import Optional

from app.config import FAMILY_MEMBERS

_INTRO_PREFIXES = (
    "小灰毛帮我",
    "麻烦小灰毛",
    "请小灰毛",
    "让小灰毛",
    "你帮我",
    "麻烦你",
    "请你",
    "让你",
    "小灰毛",
    "帮我",
    "替我",
    "麻烦",
    "帮忙",
    "请",
)
_LEADING_CONNECTORS = ("给", "跟", "和", "对")
_LEADING_VERBS = (
    "发条消息给",
    "发个消息给",
    "发消息给",
    "发个微信给",
    "发微信给",
    "发给",
    "转达给",
    "转发给",
    "转告一下",
    "转告",
    "告诉一下",
    "告诉",
    "通知一下",
    "通知",
)
_MESSAGE_MARKERS = (
    "发条消息",
    "发个消息",
    "发消息",
    "发个微信",
    "发微信",
    "带句话",
    "捎句话",
    "提一句",
    "转一句",
    "说一声",
    "说一下",
    "说下",
    "说",
    "讲一下",
    "讲下",
    "讲",
)
_SEPARATOR_CHARS = " \t\r\n:：,，"


def _sorted_by_length(values: tuple[str, ...]) -> list[str]:
    return sorted(values, key=len, reverse=True)


def _build_family_alias_map() -> dict[str, int]:
    alias_map: dict[str, int] = {}
    sorted_ids = sorted(FAMILY_MEMBERS)
    if len(sorted_ids) == 2:
        husband_id, wife_id = sorted_ids
        alias_map.setdefault("小鸡毛", husband_id)
        alias_map.setdefault("老公", husband_id)
        alias_map.setdefault("丈夫", husband_id)
        alias_map.setdefault("先生", husband_id)
        alias_map.setdefault("小白", wife_id)
        alias_map.setdefault("老婆", wife_id)
        alias_map.setdefault("妻子", wife_id)
        alias_map.setdefault("媳妇", wife_id)
        alias_map.setdefault("太太", wife_id)
    for uid, name in FAMILY_MEMBERS.items():
        normalized = name.strip().lower()
        if normalized:
            alias_map[normalized] = uid
        if "小鸡毛" in name:
            alias_map.setdefault("小鸡毛", uid)
            alias_map.setdefault("老公", uid)
            alias_map.setdefault("丈夫", uid)
            alias_map.setdefault("先生", uid)
        if "小白" in name:
            alias_map.setdefault("小白", uid)
            alias_map.setdefault("老婆", uid)
            alias_map.setdefault("妻子", uid)
            alias_map.setdefault("媳妇", uid)
            alias_map.setdefault("太太", uid)
    return alias_map


def _strip_intro_prefixes(text: str) -> str:
    remaining = text.strip()
    while remaining:
        matched = False
        for prefix in _sorted_by_length(_INTRO_PREFIXES):
            if remaining.startswith(prefix):
                remaining = remaining[len(prefix) :].lstrip(_SEPARATOR_CHARS)
                matched = True
                break
        if not matched:
            break
    return remaining


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> tuple[str, bool]:
    remaining = text.lstrip()
    for prefix in _sorted_by_length(prefixes):
        if remaining.startswith(prefix):
            return remaining[len(prefix) :], True
    return text, False


def _match_target_prefix(
    text: str,
    *,
    exclude_user_id: int | None,
) -> tuple[int, str, str] | None:
    remaining = text.lstrip()
    lowered = remaining.lower()
    alias_map = _build_family_alias_map()
    for alias in sorted(alias_map, key=len, reverse=True):
        target_id = alias_map[alias]
        if exclude_user_id is not None and target_id == exclude_user_id:
            continue
        if lowered.startswith(alias):
            target_name = FAMILY_MEMBERS.get(target_id, str(target_id))
            return target_id, target_name, remaining[len(alias) :]
    return None


def _extract_body(rest: str, *, allow_direct_body: bool) -> str | None:
    remaining = rest.lstrip()
    had_marker = False
    for marker in _sorted_by_length(_MESSAGE_MARKERS):
        if remaining.startswith(marker):
            remaining = remaining[len(marker) :]
            had_marker = True
            break
    if not had_marker and not allow_direct_body:
        if not remaining or remaining[0] not in _SEPARATOR_CHARS:
            return None
    body = remaining.lstrip(_SEPARATOR_CHARS).strip()
    return body or None


def resolve_family_member_id(identifier: str, *, exclude_user_id: int | None = None) -> int | None:
    normalized = identifier.strip().lower()
    target_id = _build_family_alias_map().get(normalized)
    if target_id is None:
        return None
    if exclude_user_id is not None and target_id == exclude_user_id:
        return None
    return target_id


def parse_forward_message(text: str, *, sender_user_id: int) -> Optional[dict[str, object]]:
    stripped = _strip_intro_prefixes(text)
    remainder, matched_connector = _strip_prefix(stripped, _LEADING_CONNECTORS)
    if matched_connector:
        target_match = _match_target_prefix(remainder, exclude_user_id=sender_user_id)
        if target_match is not None:
            target_id, target_name, rest = target_match
            body = _extract_body(rest, allow_direct_body=False)
            if body:
                return {
                    "target_id": target_id,
                    "target_name": target_name,
                    "body": body,
                }

    remainder, matched_verb = _strip_prefix(stripped, _LEADING_VERBS)
    if matched_verb:
        target_match = _match_target_prefix(remainder, exclude_user_id=sender_user_id)
        if target_match is not None:
            target_id, target_name, rest = target_match
            body = _extract_body(rest, allow_direct_body=True)
            if body:
                return {
                    "target_id": target_id,
                    "target_name": target_name,
                    "body": body,
                }

    return None


def looks_like_forward_message_request(text: str, *, sender_user_id: int) -> bool:
    return parse_forward_message(text, sender_user_id=sender_user_id) is not None
