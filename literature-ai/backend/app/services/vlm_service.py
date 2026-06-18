import logging

from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)


class VLMService(LLMService):
    """Disabled compatibility shim for the old backend visual model."""

    def analyze_image(self, image_path: str, prompt: str, model: str | None = None) -> dict:
        """Backend image analysis is disabled; inspect images in the IDE workflow."""
        logger.info("Backend visual model is disabled. Use IDE/MCP AI or human review for images.")
        return {}
