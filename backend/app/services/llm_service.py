import logging
from typing import Any, Type, TypeVar

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
    DISABLED_REASON = "Web-side LLM is disabled. Use IDE/MCP AI for parsing, review, and verification."

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.writer_model or "gpt-4o-mini"
        self.client = None

    def is_configured(self) -> bool:
        """Web-side LLM execution is intentionally disabled.

        Parsing and verification are handled by IDE/MCP AI so the web app never
        sends paper text or credentials to a Writer/internal-parser LLM.
        """
        return False

    def structured_extract(self, system_prompt: str, user_prompt: str, response_format: Type[T]) -> T | None:
        """Disabled compatibility method for old web-side LLM callers."""
        logger.info(self.DISABLED_REASON)
        return None

    def complete_text(self, system_prompt: str, user_prompt: str) -> str | None:
        """Disabled compatibility method for old web-side LLM callers."""
        logger.info(self.DISABLED_REASON)
        return None
