import json
import os

CONFIG_FILE = "config.json"


class ConfigManager:
    def __init__(self):
        self.config = {
            "api_key": "",
            "base_url": "https://api.openai.com/v1",
            "proxy": "",
            "search_limit": 20,
            "theme": "Midnight",
            "llm_model": "gpt-4o-mini",
            "literature_ai_url": "http://localhost:8000",
            "findpapers_email": "",
            "findpapers_ieee_api_key": "",
            "findpapers_wos_api_key": "",
            "findpapers_scopus_api_key": "",
            "findpapers_pubmed_api_key": "",
            "findpapers_openalex_api_key": "",
            "findpapers_semantic_scholar_api_key": "",
            "findpapers_ssl_verify": True,
        }
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as file:
                    saved_config = json.load(file)
                    self.config.update(saved_config)
            except Exception:
                pass

    def save(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as file:
            json.dump(self.config, file, indent=4, ensure_ascii=False)

    def get(self, key, default=None):
        if key in self.config:
            return self.config[key]
        return default

    def set(self, key, value):
        self.config[key] = value
        self.save()
