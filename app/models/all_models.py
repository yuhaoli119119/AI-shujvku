"""统一数据模型 —— 唯一真实定义在 app/core/models.py。

本文件仅提供向后兼容的别名，确保 db.py / workers/tasks.py 等遗留代码正常工作。
所有 SQLModel 类均从 core.models 导入，不存在重复表定义。
"""

from typing import Optional

# ── 直接引用核心模型（唯一真实定义）───────────────────────────────
from app.core.models import Paper, File, Chunk, ExtractionJob, ExtractedRecord


# ── 旧名兼容性别名 ────────────────────────────────────────────────
# Author 表在项目中未被实际使用，保留 None 占位防止 ImportError
Author = None  # type: ignore[assignment]

# 旧代码中使用 PaperFile / PaperChunk 作为类名
PaperFile = File
PaperChunk = Chunk
