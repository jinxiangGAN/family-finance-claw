"""Abstract LLM provider supporting OpenAI-compatible APIs.

Supports: MiniMax, OpenAI, DeepSeek, Qwen, Gemini, and any OpenAI-compatible endpoint.
Switch providers by changing LLM_PROVIDER, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL in .env.

v4: Added embed() for vector memory support.
"""

import asyncio
import json
import logging
import math
import struct
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Rate-limit retry settings
_MAX_RETRIES = 3
_DEFAULT_RETRY_WAIT = 10  # seconds (Gemini free tier = 15 RPM)


# ═══════════════════════════════════════════
#  Pure-Python vector utilities (no numpy)
# ═══════════════════════════════════════════

def pack_embedding(embedding: list[float]) -> bytes:
    """Pack a float list into a compact binary BLOB (float32)."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def unpack_embedding(data: bytes) -> list[float]:
    """Unpack a binary BLOB back to a float list."""
    count = len(data) // 4
    return list(struct.unpack(f"{count}f", data))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ═══════════════════════════════════════════
#  LLM Provider base class
# ═══════════════════════════════════════════

class LLMProvider:
    """Unified LLM interface for chat completion, vision, and embedding."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: int = 30,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ── HTTP helper with 429 rate-limit retry ──

    async def _post_with_retry(
        self, url: str, headers: dict, payload: dict
    ) -> httpx.Response:
        """POST with automatic retry on HTTP 429 (rate limit).

        Gemini free tier = 15 RPM; this prevents the bot from crashing
        when two family members record expenses at the same time.
        """
        for attempt in range(1, _MAX_RETRIES + 1):
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)

            if resp.status_code != 429:
                resp.raise_for_status()
                return resp

            # 429 — parse Retry-After or use default wait
            retry_after = int(resp.headers.get("Retry-After", _DEFAULT_RETRY_WAIT))
            logger.warning(
                "[LLM] Rate limited (429), retry %d/%d after %ds",
                attempt, _MAX_RETRIES, retry_after,
            )
            await asyncio.sleep(retry_after)

        # Exhausted retries — raise the last 429 so caller can handle it
        resp.raise_for_status()
        return resp  # unreachable, but keeps type checker happy

    # ── Chat Completion ──

    async def chat_completion(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> tuple[dict, Optional[dict]]:
        """Call chat completion API. Returns (message, usage)."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools

        resp = await self._post_with_retry(url, headers, payload)

        data = resp.json()
        logger.debug("LLM response: %s", json.dumps(data, ensure_ascii=False)[:500])

        message = data.get("choices", [{}])[0].get("message", {})
        usage = data.get("usage")
        return message, usage

    # ── Vision ──

    async def chat_completion_with_image(
        self,
        text: str,
        image_url: str,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> tuple[str, Optional[dict]]:
        """Call vision-capable chat completion with an image."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        })

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = await self._post_with_retry(url, headers, payload)

        data = resp.json()
        logger.debug("LLM vision response: %s", json.dumps(data, ensure_ascii=False)[:500])

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage")
        return content, usage

    # ── Embedding ──

    async def embed(self, text: str, model: str = "") -> Optional[list[float]]:
        """Generate an embedding vector via the /embeddings endpoint.

        Returns None if the API call fails (caller should fall back to FTS5).
        """
        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or self.model,
            "input": text,
        }

        try:
            resp = await self._post_with_retry(url, headers, payload)
            data = resp.json()
            embedding = data.get("data", [{}])[0].get("embedding")
            if embedding and isinstance(embedding, list):
                logger.debug("Embedding generated: dim=%d", len(embedding))
                return embedding
        except Exception:
            logger.debug("Embedding API call failed, will use FTS5 fallback", exc_info=True)

        return None


# ─────────────── Provider presets ───────────────

PROVIDER_PRESETS: dict[str, dict] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "vision_model": "gpt-4o",
        "embedding_model": "text-embedding-3-small",
    },
    "minimax": {
        "base_url": "https://api.minimax.chat/v1/text",
        "default_model": "abab6.5s-chat",
        "vision_model": "abab6.5s-chat",
        "embedding_model": "embo-01",
        "chat_endpoint": "chatcompletion_v2",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "vision_model": "deepseek-chat",
        "embedding_model": "",  # Not available — use FTS5
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "vision_model": "qwen-vl-plus",
        "embedding_model": "text-embedding-v3",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-3-flash-preview",
        "vision_model": "gemini-3-flash-preview",
        "embedding_model": "text-embedding-004",
    },
    "custom": {
        "base_url": "",
        "default_model": "",
        "vision_model": "",
        "embedding_model": "",
    },
}


class MiniMaxProvider(LLMProvider):
    """MiniMax-specific provider (slightly different endpoint structure)."""

    async def chat_completion(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> tuple[dict, Optional[dict]]:
        url = f"{self.base_url}/chatcompletion_v2"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools

        resp = await self._post_with_retry(url, headers, payload)

        data = resp.json()
        logger.debug("MiniMax response: %s", json.dumps(data, ensure_ascii=False)[:500])

        message = data.get("choices", [{}])[0].get("message", {})
        usage = data.get("usage")
        return message, usage

    async def chat_completion_with_image(
        self,
        text: str,
        image_url: str,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> tuple[str, Optional[dict]]:
        """MiniMax vision via the same endpoint."""
        url = f"{self.base_url}/chatcompletion_v2"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        })
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp = await self._post_with_retry(url, headers, payload)
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage")
        return content, usage

    async def embed(self, text: str, model: str = "") -> Optional[list[float]]:
        """MiniMax embedding endpoint."""
        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or "embo-01",
            "input": text,
            "type": "db",
        }
        try:
            resp = await self._post_with_retry(url, headers, payload)
            data = resp.json()
            embedding = data.get("data", [{}])[0].get("embedding")
            if embedding and isinstance(embedding, list):
                logger.debug("MiniMax embedding: dim=%d", len(embedding))
                return embedding
        except Exception:
            logger.debug("MiniMax embedding failed", exc_info=True)
        return None


def create_provider(
    provider_name: str,
    api_key: str,
    model: str = "",
    base_url: str = "",
) -> LLMProvider:
    """Factory function to create an LLM provider."""
    preset = PROVIDER_PRESETS.get(provider_name, PROVIDER_PRESETS["custom"])

    effective_base_url = base_url or preset.get("base_url", "")
    effective_model = model or preset.get("default_model", "")

    if not effective_base_url:
        raise ValueError(f"No base_url configured for provider '{provider_name}'")

    if provider_name == "minimax":
        return MiniMaxProvider(
            api_key=api_key,
            model=effective_model,
            base_url=effective_base_url,
        )

    return LLMProvider(
        api_key=api_key,
        model=effective_model,
        base_url=effective_base_url,
    )
