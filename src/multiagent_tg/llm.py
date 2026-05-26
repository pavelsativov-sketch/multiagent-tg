"""OpenAI-совместимый клиент для LLM (Ollama, LM Studio, OpenAI, Gemini, OpenRouter, etc.)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

log = logging.getLogger(__name__)

# Модели-фолбэки для OpenRouter (бесплатные).
# Если основная модель недоступна (404/429), пробуем следующую.
OPENROUTER_FREE_FALLBACKS: list[str] = [
    "google/gemma-4-31b-it:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-v4-flash:free",
    "qwen/qwen3-coder:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "openai/gpt-oss-120b:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
]


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMClient:
    """Тонкая обёртка над AsyncOpenAI с ретраями и автоматическим фолбэком моделей.

    Совместима с любым OpenAI-style endpoint:
        Ollama:      http://localhost:11434/v1
        LM Studio:   http://localhost:1234/v1
        OpenAI:      https://api.openai.com/v1
        Gemini:      https://generativelanguage.googleapis.com/v1beta/openai/
        OpenRouter:  https://openrouter.ai/api/v1
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=120.0)
        self._is_gemini = "googleapis.com" in base_url.lower()
        self._is_openrouter = "openrouter.ai" in base_url.lower()
        self._is_openai = "api.openai.com" in base_url.lower()

    @property
    def provider(self) -> str:
        if self._is_gemini:
            return "gemini"
        if self._is_openrouter:
            return "openrouter"
        if self._is_openai:
            return "openai"
        if "localhost" in self.base_url:
            return "local"
        return "custom"

    def _build_payload(
        self,
        model: str,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        return payload

    async def _call_model(self, model: str, payload: dict[str, Any]) -> Any:
        """Один вызов к модели с ретраями."""
        payload = {**payload, "model": model}
        log.debug("LLM request: model=%s provider=%s", model, self.provider)
        response = await self.client.chat.completions.create(**payload)
        return response

    async def chat(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int = 600,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        """Сделать запрос к LLM с автоматическим фолбэком на другие модели при ошибках."""
        payload = self._build_payload(
            self.model, messages, temperature, max_tokens, tools, tool_choice,
        )

        models_to_try = [self.model]
        if self._is_openrouter:
            for fb in OPENROUTER_FREE_FALLBACKS:
                if fb != self.model and fb not in models_to_try:
                    models_to_try.append(fb)

        last_error: Exception | None = None

        for model in models_to_try:
            for attempt in range(3):
                try:
                    response = await self._call_model(model, payload)
                    if model != self.model:
                        log.info("Фолбэк сработал: %s → %s", self.model, model)
                    return response
                except Exception as e:
                    last_error = e
                    err_str = str(e)
                    is_not_found = "404" in err_str or "NOT_FOUND" in err_str
                    is_rate_limit = "429" in err_str or "rate" in err_str.lower()
                    is_daily_limit = "free-models-per-day" in err_str

                    if is_daily_limit:
                        log.warning(
                            "Дневной лимит бесплатных моделей исчерпан. "
                            "Добавьте кредиты на https://openrouter.ai/settings/credits"
                        )
                        raise

                    if is_not_found:
                        log.warning("Модель %s не найдена (404), пробую следующую", model)
                        break

                    if is_rate_limit and attempt < 2:
                        wait_sec = 10 * (attempt + 1)
                        log.info("Rate limit для %s, жду %dс (попытка %d/3)", model, wait_sec, attempt + 1)
                        await asyncio.sleep(wait_sec)
                        continue
                    elif is_rate_limit:
                        log.warning("Rate limit для %s исчерпан, пробую следующую модель", model)
                        break

                    if attempt < 2:
                        wait_sec = 5 * (attempt + 1)
                        log.warning("Ошибка %s (попытка %d/3): %s", model, attempt + 1, e)
                        await asyncio.sleep(wait_sec)
                        continue
                    else:
                        log.warning("Модель %s: 3 неудачных попытки, пробую следующую", model)
                        break

        if last_error:
            raise last_error
        raise RuntimeError("Не удалось получить ответ ни от одной модели")

    async def chat_text(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int = 600,
    ) -> str:
        """Самый частый кейс: вернуть просто текст ответа."""
        resp = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        if not resp.choices:
            return "(пустой ответ от модели)"
        content = resp.choices[0].message.content or ""
        return content.strip()

    async def health_check(self) -> dict[str, Any]:
        """Проверить что LLM-endpoint доступен и отвечает."""
        try:
            text = await self.chat_text(
                [ChatMessage(role="user", content="Скажи 'ok' одним словом.")],
                temperature=0,
                max_tokens=50,
            )
            return {"ok": True, "response": text, "model": self.model, "provider": self.provider}
        except Exception as e:
            return {"ok": False, "error": str(e), "model": self.model, "provider": self.provider}
