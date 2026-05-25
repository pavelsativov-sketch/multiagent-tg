"""OpenAI-совместимый клиент для LLM (Ollama, LM Studio, OpenAI, etc.)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMClient:
    """Тонкая обёртка над AsyncOpenAI с ретраями.

    Совместима с любым OpenAI-style endpoint:
        Ollama:    http://localhost:11434/v1
        LM Studio: http://localhost:1234/v1
        OpenAI:    https://api.openai.com/v1
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self.model = model
        self.base_url = base_url
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=120.0)
        # Gemini-2.5* по умолчанию использует "thinking" - тратит часть max_tokens
        # на reasoning. Для коротких ответов (роутинг, doctor) это ломает выдачу.
        # Отключаем thinking, если endpoint = Gemini.
        self._is_gemini = "googleapis.com" in base_url.lower()
        self._is_openrouter = "openrouter.ai" in base_url.lower()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type(Exception),
    )
    async def chat(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int = 600,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        """Сделать запрос к LLM. Возвращает целиком объект response (для доступа к
        tool_calls если они есть)."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        # Gemini-2.5: убираем "thinking" чтобы не сжирать max_tokens на reasoning.
        # reasoning_effort - стандартный OpenAI-параметр (для o-series), Gemini
        # его понимает и для своих 2.5 моделей.
        if self._is_gemini and self.model.startswith("gemini-2.5"):
            payload["extra_body"] = {"reasoning_effort": "none"}
        elif self._is_openrouter:
            payload["extra_body"] = {"reasoning": {"effort": "none"}}

        log.debug("LLM request: model=%s msgs=%d", self.model, len(messages))
        response = await self.client.chat.completions.create(**payload)
        return response

    async def chat_text(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int = 600,
    ) -> str:
        """Самый частый кейс: вернуть просто текст ответа."""
        resp = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        content = resp.choices[0].message.content or ""
        return content.strip()
