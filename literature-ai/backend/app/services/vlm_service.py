import base64
import json
import logging
from pathlib import Path

from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)

class VLMService(LLMService):
    """视觉语言模型服务，支持图片输入（同步，与 LLMService 架构一致）"""
    
    def analyze_image(self, image_path: str, prompt: str, model: str | None = None) -> dict:
        """发送图片+prompt到视觉模型（同步调用）"""
        self._tracker.pre_check()
        
        try:
            image_bytes = Path(image_path).read_bytes()
            b64_image = base64.b64encode(image_bytes).decode("utf-8")
            
            # 判断图片 MIME 类型
            suffix = Path(image_path).suffix.lower()
            mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
            mime_type = mime_map.get(suffix, "image/png")
            
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{b64_image}",
                                "detail": "low",  # 论文图片不需要高分辨率，按需降低费用
                            },
                        },
                    ],
                }
            ]
            
            # 同步调用 OpenAI client（与 LLMService.structured_extract 一致）
            response = self.client.chat.completions.create(
                model=model or self.settings.writer_model or "gpt-4o-mini",
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.1,
                timeout=self.settings.writer_timeout_seconds or 60.0,
            )
            
            # TODO: 计算实际 token 消耗。粗略估算。
            self._tracker.post_request(input_tokens=1000, output_tokens=100)
            
            content = response.choices[0].message.content
            return json.loads(content) if content else {}
        except Exception as e:
            logger.exception("VLM analysis failed for %s", image_path)
            return {}
