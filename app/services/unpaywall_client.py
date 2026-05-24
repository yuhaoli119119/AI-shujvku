import httpx
import time
import logging
from typing import Optional
from ..schemas.metadata import OALocation
from ..config import settings

logger = logging.getLogger(__name__)

class UnpaywallClient:
    BASE_URL = "https://api.unpaywall.org/v2/"

    def __init__(self):
        self.email = settings.UNPAYWALL_EMAIL
        self.rate_limit_delay = 1.0 / max(settings.DEFAULT_RATE_LIMIT_RPS, 1)

    async def find_oa_pdf(self, doi: str) -> Optional[OALocation]:
        if not doi:
            return None
            
        if doi.startswith("http"):
            doi = doi.split("doi.org/")[-1]

        try:
            # 速率限制
            time.sleep(self.rate_limit_delay)
            
            params = {"email": self.email}
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(f"{self.BASE_URL}{doi}", params=params)
                if response.status_code != 200:
                    logger.warning(f"Unpaywall 返回状态码: {response.status_code}")
                    return None
                data = response.json()

            best_location = data.get("best_oa_location")
            if not best_location:
                return None

            return OALocation(
                url=best_location.get("url"),
                url_for_pdf=best_location.get("url_for_pdf"),
                is_best=True,
                license=best_location.get("license"),
                version=best_location.get("version"),
                host_type=best_location.get("host_type")
            )
        except Exception as e:
            logger.error(f"Unpaywall 错误: {e}")
            return None
