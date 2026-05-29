import json
import logging
from typing import Any, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from app.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


import time
import threading

class CostTracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.total_tokens = 0
        self.total_cost = 0.0
        self.max_cost = 5.0  # $5 max per session for safety
        self.requests_this_minute = 0
        self.minute_start = time.time()

    def pre_check(self):
        with self.lock:
            now = time.time()
            if now - self.minute_start > 60:
                self.minute_start = now
                self.requests_this_minute = 0
            
            if self.requests_this_minute >= 100:
                raise RuntimeError("Rate limit exceeded (100 req/min). Please slow down.")
            if self.total_cost >= self.max_cost:
                raise RuntimeError(f"Cost limit exceeded. Max: ${self.max_cost}")
            self.requests_this_minute += 1

    def account(self, input_tokens: int, output_tokens: int, cost_per_1k_input: float = 0.00015, cost_per_1k_output: float = 0.0006):
        with self.lock:
            req_cost = (input_tokens / 1000.0) * cost_per_1k_input + (output_tokens / 1000.0) * cost_per_1k_output
            self.total_tokens += (input_tokens + output_tokens)
            self.total_cost += req_cost

global_cost_tracker = CostTracker()


class LLMService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.writer_model or "gpt-4o-mini"
        self.client = OpenAI(
            api_key=settings.writer_api_key or "sk-dummy",
            base_url=settings.writer_api_base,
        )

    def is_configured(self) -> bool:
        """Return True only when the Writer LLM has the fields needed to call an API."""
        return (
            bool(self.settings.writer_api_key)
            and self.settings.writer_api_key != "sk-dummy"
            and bool(self.settings.writer_api_base)
            and bool(self.model)
        )

    def structured_extract(self, system_prompt: str, user_prompt: str, response_format: Type[T]) -> T | None:
        """Calls the LLM with structured output parsing (JSON schema)."""
        if not self.is_configured():
            logger.warning("LLMService is not configured (missing API key). Falling back.")
            return None

        try:
            global_cost_tracker.pre_check()
            schema_json = response_format.model_json_schema()
            
            full_system = (
                f"{system_prompt}\n\n"
                f"You MUST output raw JSON. The JSON must perfectly match this JSON Schema:\n"
                f"{json.dumps(schema_json, indent=2)}"
            )

            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": full_system},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                timeout=self.settings.writer_timeout_seconds or 60.0,
            )

            usage = response.usage
            if usage:
                global_cost_tracker.account(usage.prompt_tokens, usage.completion_tokens)

            content = response.choices[0].message.content
            if not content:
                return None

            return response_format.model_validate_json(content)

        except Exception as e:
            logger.error(f"LLM extraction failed: {e}", exc_info=True)
            return None

    def complete_text(self, system_prompt: str, user_prompt: str) -> str | None:
        """Call the Writer LLM for plain text output without changing application state."""
        if not self.is_configured():
            logger.warning("LLMService is not configured (missing Writer API fields).")
            return None

        try:
            global_cost_tracker.pre_check()
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                timeout=self.settings.writer_timeout_seconds or 60.0,
            )

            usage = response.usage
            if usage:
                global_cost_tracker.account(usage.prompt_tokens, usage.completion_tokens)

            content = response.choices[0].message.content
            return content.strip() if content else None
        except Exception as e:
            logger.error(f"LLM text completion failed: {e}", exc_info=True)
            return None
