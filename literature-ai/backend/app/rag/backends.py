from __future__ import annotations

import json
import re
from typing import Any, Protocol

from app.config import Settings


class WriterBackend(Protocol):
    name: str

    def generate(
        self,
        prompt_payload: dict[str, Any],
        rule_sections: dict[str, Any],
        messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        ...


class RuleWriterBackend:
    name = "rule"

    def generate(
        self,
        prompt_payload: dict[str, Any],
        rule_sections: dict[str, Any],
        messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            "backend_used": self.name,
            "prompt_preview": self._preview(prompt_payload),
            "sections": rule_sections,
            "llm_diagnostics": {
                "mode": "offline_rule",
                "requested_backend": self.name,
                "final_backend": self.name,
            },
        }

    @staticmethod
    def _preview(prompt_payload: dict[str, Any]) -> str:
        preview = {
            "instruction": prompt_payload.get("instruction"),
            "topic": prompt_payload.get("topic"),
            "requested_sections": prompt_payload.get("requested_sections"),
            "guardrails": prompt_payload.get("guardrails"),
            "llm_output_contract": prompt_payload.get("llm_output_contract"),
        }
        return json.dumps(preview, ensure_ascii=False, indent=2)

    def status(self) -> dict[str, Any]:
        return {
            "mode": "offline_rule",
            "requested_backend": self.name,
            "final_backend": self.name,
            "ready": True,
        }


class StubLLMWriterBackend:
    """Offline-safe stand-in for a future real LLM backend."""

    name = "llm_stub"

    def generate(
        self,
        prompt_payload: dict[str, Any],
        rule_sections: dict[str, Any],
        messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        sections = {}
        for key, value in rule_sections.items():
            if isinstance(value, str) and value:
                sections[key] = f"[LLM-STUB REWRITE]\n{value}"
            else:
                sections[key] = value
        return {
            "backend_used": self.name,
            "prompt_preview": RuleWriterBackend._preview(prompt_payload),
            "sections": sections,
            "llm_diagnostics": {
                "mode": "offline_stub",
                "requested_backend": self.name,
                "final_backend": self.name,
            },
        }

    def status(self) -> dict[str, Any]:
        return {
            "mode": "offline_stub",
            "requested_backend": self.name,
            "final_backend": self.name,
            "ready": True,
        }


class OpenAICompatibleWriterBackend:
    name = "openai_compatible"
    JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)

    def __init__(self, settings: Settings, fallback: WriterBackend) -> None:
        self.settings = settings
        self.fallback = fallback

    def generate(
        self,
        prompt_payload: dict[str, Any],
        rule_sections: dict[str, Any],
        messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        missing_config = self._missing_configuration(messages)
        if missing_config:
            return {
                "backend_used": self.name,
                "prompt_preview": RuleWriterBackend._preview(prompt_payload),
                "sections": {},
                "llm_status": "missing_configuration",
                "llm_error": f"请先在设置页面配置 API：缺少 {', '.join(missing_config)}。Writer 必须配置 LLM 才能生成内容。",
                "llm_diagnostics": {
                    "mode": "missing_configuration",
                    "requested_backend": self.name,
                    "final_backend": self.name,
                    "missing_configuration": missing_config,
                    "request_url": self._build_chat_completions_url(self.settings.writer_api_base or "") if self.settings.writer_api_base else None,
                    "message_count": len(messages or []),
                },
            }

        url = self._build_chat_completions_url(self.settings.writer_api_base or "")
        payload = {
            "model": self.settings.writer_model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        try:
            import httpx

            with httpx.Client(timeout=self.settings.writer_timeout_seconds) as client:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.settings.writer_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
            content = self._extract_message_content(data)
            parsed, parse_mode = self._parse_sections(content, rule_sections)
            return {
                "backend_used": self.name,
                "prompt_preview": RuleWriterBackend._preview(prompt_payload),
                "sections": parsed,
                "llm_status": "ok",
                "llm_diagnostics": {
                    "mode": "live_llm",
                    "requested_backend": self.name,
                    "final_backend": self.name,
                    "request_url": url,
                    "message_count": len(messages or []),
                    "parse_mode": parse_mode,
                },
            }
        except Exception as exc:
            return {
                "backend_used": self.name,
                "prompt_preview": RuleWriterBackend._preview(prompt_payload),
                "sections": {},
                "llm_status": f"error:{type(exc).__name__}",
                "llm_error": f"LLM 调用失败：{exc}。请检查 API 配置是否正确。",
                "llm_diagnostics": {
                    "mode": "error",
                    "requested_backend": self.name,
                    "final_backend": self.name,
                    "request_url": url,
                    "message_count": len(messages or []),
                    "error_type": type(exc).__name__,
                    "error_detail": str(exc),
                },
            }

    def status(self) -> dict[str, Any]:
        missing_config = self._missing_configuration(messages=[{"role": "system", "content": "status_probe"}])
        missing_config = [item for item in missing_config if item != "messages"]
        request_url = self._build_chat_completions_url(self.settings.writer_api_base or "") if self.settings.writer_api_base else None
        if missing_config:
            return {
                "mode": "missing_configuration",
                "requested_backend": self.name,
                "final_backend": f"{self.name}->{self.fallback.name}",
                "ready": False,
                "missing_configuration": missing_config,
                "request_url": request_url,
                "fallback_backend": self.fallback.name,
            }
        return {
            "mode": "live_llm_ready",
            "requested_backend": self.name,
            "final_backend": self.name,
            "ready": True,
            "missing_configuration": [],
            "request_url": request_url,
            "fallback_backend": self.fallback.name,
            "model": self.settings.writer_model,
            "timeout_seconds": self.settings.writer_timeout_seconds,
        }

    @classmethod
    def _parse_sections(cls, content: str, rule_sections: dict[str, Any]) -> tuple[dict[str, Any], str]:
        normalized, parse_mode = cls._normalize_content_to_json_text(content)
        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError:
            return rule_sections, f"{parse_mode}:invalid_json_fallback"
        merged = dict(rule_sections)
        for key in ["outline", "introduction", "dft_results", "discussion", "figure_storyline"]:
            if key in parsed:
                merged[key] = cls._coerce_section_value(key, parsed[key], rule_sections.get(key))
        return merged, parse_mode

    @staticmethod
    def _coerce_section_value(key: str, value: Any, fallback: Any) -> Any:
        if key in {"outline", "figure_storyline"}:
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
            if isinstance(value, str):
                lines = [line.strip(" -0123456789.").strip() for line in re.split(r"[\n;]+", value) if line.strip()]
                return [line for line in lines if line]
            return fallback if fallback is not None else []
        if isinstance(value, str):
            return value.strip()
        if value is None:
            return fallback if fallback is not None else ""
        return str(value)

    def _missing_configuration(self, messages: list[dict[str, str]] | None) -> list[str]:
        missing: list[str] = []
        if not self.settings.writer_api_base:
            missing.append("writer_api_base")
        if not self.settings.writer_api_key:
            missing.append("writer_api_key")
        if not messages:
            missing.append("messages")
        return missing

    def _build_chat_completions_url(self, api_base: str) -> str:
        base = api_base.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/chat/completions"

    @classmethod
    def _normalize_content_to_json_text(cls, content: str) -> tuple[str, str]:
        if not isinstance(content, str):
            return json.dumps(content), "non_string_json_dump"
        stripped = content.strip()
        block_match = cls.JSON_BLOCK_PATTERN.search(stripped)
        if block_match:
            return block_match.group(1).strip(), "json_code_fence"
        return stripped, "plain_text"

    @staticmethod
    def _extract_message_content(data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("OpenAI-compatible response missing choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ValueError("OpenAI-compatible response missing message")
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_chunks = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    text_chunks.append(str(item["text"]))
            if text_chunks:
                return "\n".join(text_chunks)
        raise ValueError("OpenAI-compatible response content is empty or unsupported")


def resolve_writer_backend(name: str | None, settings: Settings) -> WriterBackend:
    lowered = (name or "").lower()
    fallback_name = (settings.writer_fallback_backend or "rule").lower()
    fallback = StubLLMWriterBackend() if fallback_name == "llm_stub" else RuleWriterBackend()
    if lowered == "llm_stub":
        return StubLLMWriterBackend()
    if lowered in {"openai", "openai_compatible", "llm"}:
        return OpenAICompatibleWriterBackend(settings=settings, fallback=fallback)
    return RuleWriterBackend()
