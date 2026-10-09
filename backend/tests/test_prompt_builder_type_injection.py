"""Tests for prompt_builder target_paper_type injection."""
from __future__ import annotations

from app.rag.prompt_builder import PaperWriterPromptBuilder


class TestTargetPaperTypeInjection:
    """Tests for PaperWriterPromptBuilder target_paper_type support."""

    def setup_method(self):
        self.builder = PaperWriterPromptBuilder(prompt_path=__import__("pathlib").Path("/nonexistent.yaml"))

    def test_build_returns_target_paper_type(self):
        """build() should include target_paper_type in its output dict."""
        result = self.builder.build(
            topic="CO2 reduction",
            user_notes=None,
            requested_sections=["outline"],
            retrieved={},
            target_paper_type="A1",
        )
        assert result["target_paper_type"] == "A1"

    def test_build_default_target_paper_type_is_none(self):
        """Without target_paper_type, the field should be None."""
        result = self.builder.build(
            topic="CO2 reduction",
            user_notes=None,
            requested_sections=["outline"],
            retrieved={},
        )
        assert result["target_paper_type"] is None

    def test_render_messages_injects_type_context_in_system(self):
        """When target_paper_type is set, system prompt should contain type context."""
        payload = self.builder.build(
            topic="CO2 reduction",
            user_notes=None,
            requested_sections=["outline"],
            retrieved={},
            target_paper_type="A1",
        )
        messages = self.builder.render_messages(payload, {})
        system = messages[0]["content"]
        assert "论文类型上下文" in system
        assert "A1" in system
        assert "纯计算" in system

    def test_render_messages_no_type_context_when_none(self):
        """When target_paper_type is None, system prompt should NOT contain type context."""
        payload = self.builder.build(
            topic="CO2 reduction",
            user_notes=None,
            requested_sections=["outline"],
            retrieved={},
        )
        messages = self.builder.render_messages(payload, {})
        system = messages[0]["content"]
        assert "论文类型上下文" not in system

    def test_render_messages_type_c_injects_experimental(self):
        """C type should inject experimental context."""
        payload = self.builder.build(
            topic="ORR catalyst",
            user_notes=None,
            requested_sections=["outline"],
            retrieved={},
            target_paper_type="C2",
        )
        messages = self.builder.render_messages(payload, {})
        system = messages[0]["content"]
        assert "纯实验" in system

    def test_render_messages_type_r_injects_review(self):
        """R type should inject review context."""
        payload = self.builder.build(
            topic="Li-S batteries",
            user_notes=None,
            requested_sections=["outline"],
            retrieved={},
            target_paper_type="R",
        )
        messages = self.builder.render_messages(payload, {})
        system = messages[0]["content"]
        assert "综述" in system

    def test_render_messages_user_yaml_contains_target_paper_type(self):
        """User YAML should contain target_paper_type field."""
        payload = self.builder.build(
            topic="CO2 reduction",
            user_notes=None,
            requested_sections=["outline"],
            retrieved={},
            target_paper_type="B1",
        )
        messages = self.builder.render_messages(payload, {})
        user_yaml = messages[1]["content"]
        assert "B1" in user_yaml
        assert "target_paper_type" in user_yaml
