import json
import os
import urllib.parse
import urllib.request

from openai import OpenAI


class LiteratureAIClient:
    def __init__(self, base_url: str):
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise ValueError("Literature AI service URL is not configured")

    def _request_json(self, path: str, method: str = "GET", payload: dict | None = None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def ingest_path(self, payload: dict) -> dict:
        return self._request_json("/api/papers/ingest/path", method="POST", payload=payload)

    def get_paper(self, paper_id: str) -> dict:
        return self._request_json(f"/api/papers/{paper_id}")

    def extract_paper(self, paper_id: str) -> dict:
        return self._request_json(f"/api/papers/{paper_id}/extract", method="POST", payload={})

    def find_by_source_path(self, source_path: str) -> dict | None:
        query = urllib.parse.urlencode({"source_path": source_path, "limit": 1})
        result = self._request_json(f"/api/papers?{query}")
        if isinstance(result, list) and result:
            return result[0]
        return None

    def list_papers(self, limit: int = 100, offset: int = 0) -> list[dict]:
        query = urllib.parse.urlencode({"limit": limit, "offset": offset})
        result = self._request_json(f"/api/papers?{query}")
        return result if isinstance(result, list) else []

    def ai_workflow(self, payload: dict) -> dict:
        return self._request_json("/api/papers/ai_workflow", method="POST", payload=payload)

    def iter_all_papers(self, page_size: int = 100):
        offset = 0
        while True:
            page = self.list_papers(limit=page_size, offset=offset)
            if not page:
                break
            for item in page:
                yield item
            if len(page) < page_size:
                break
            offset += page_size

    @staticmethod
    def pick_accessible_pdf_path(paper_detail: dict) -> str | None:
        for key in ("source_path", "pdf_path"):
            value = (paper_detail.get(key) or "").strip()
            if value and os.path.exists(value):
                return value
        return None


def generate_chinese_title(original_title: str, api_key: str, base_url: str | None = None, model: str = "gpt-4o-mini") -> str | None:
    title = (original_title or "").strip()
    if not title or not api_key:
        return None

    client = OpenAI(api_key=api_key, base_url=base_url or None)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Translate the academic paper title into concise, accurate Chinese. "
                    "Keep technical terms precise. Return only the Chinese title."
                ),
            },
            {"role": "user", "content": title},
        ],
        temperature=0.2,
        timeout=30.0,
    )
    content = response.choices[0].message.content
    translated = content.strip() if content else ""
    return translated or None
