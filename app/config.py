from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./storage/lit_ai.db"
    REDIS_URL: str = "redis://localhost:6379/0"
    
    OPENALEX_API_KEY: Optional[str] = None
    UNPAYWALL_EMAIL: str = "your_email@example.com"
    
    GROBID_URL: str = "http://localhost:8070"
    
    AI_PROVIDER: str = "openai"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-2024-08-06"
    
    STORAGE_BACKEND: str = "local"
    LOCAL_STORAGE_DIR: str = "./storage/papers"
    
    DEFAULT_RATE_LIMIT_RPS: int = 1
    MAX_DOWNLOAD_RETRIES: int = 3
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def validate(self) -> None:
        """启动时验证必要配置"""
        warnings = []
        if not self.OPENAI_API_KEY:
            warnings.append("OPENAI_API_KEY 未设置，AI 提取功能将不可用")
        if not self.UNPAYWALL_EMAIL or self.UNPAYWALL_EMAIL == "your_email@example.com":
            warnings.append("UNPAYWALL_EMAIL 未正确配置，Unpaywall 功能可能受限")
        for msg in warnings:
            logger.warning(msg)

settings = Settings()
settings.validate()
