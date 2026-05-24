import tempfile
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import app
from app.config import get_settings
from test_papers_api import setup_test_db


def test_writer_settings_endpoint(setup_test_db, monkeypatch):
    client = TestClient(app)

    # 1. 初始状态获取设置
    response = client.get("/api/writer/settings")
    assert response.status_code == 200
    data = response.json()
    assert "writer_backend" in data
    assert "writer_model" in data

    # 2. 提交更新设置
    payload = {
        "writer_backend": "openai_compatible",
        "writer_model": "test-model-abc",
        "writer_api_base": "https://test-api.example.com",
        "writer_api_key": "sk-test-key-123456",
        "writer_fallback_backend": "rule",
    }

    # 临时 patch .env 写入逻辑，避免修改真实的本地 .env
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_env = Path(tmpdir) / ".env"

        def fake_update_env_file(
            writer_backend: str,
            writer_model: str,
            writer_api_base: str | None,
            writer_api_key: str | None,
            writer_fallback_backend: str,
        ):
            lines = [
                f"LITAI_WRITER_BACKEND={writer_backend}\n",
                f"LITAI_WRITER_MODEL={writer_model}\n",
                f"LITAI_WRITER_API_BASE={writer_api_base or ''}\n",
                f"LITAI_WRITER_API_KEY={writer_api_key or ''}\n",
                f"LITAI_WRITER_FALLBACK_BACKEND={writer_fallback_backend}\n",
            ]
            with open(fake_env, "w", encoding="utf-8") as f:
                f.writelines(lines)

            # 同步更新 os.environ，保证 get_settings.cache_clear() 后重构的 Settings 能读取到最新值
            monkeypatch.setenv("LITAI_WRITER_BACKEND", writer_backend)
            monkeypatch.setenv("LITAI_WRITER_MODEL", writer_model)
            if writer_api_base:
                monkeypatch.setenv("LITAI_WRITER_API_BASE", writer_api_base)
            else:
                monkeypatch.delenv("LITAI_WRITER_API_BASE", raising=False)
            if writer_api_key:
                monkeypatch.setenv("LITAI_WRITER_API_KEY", writer_api_key)
            else:
                monkeypatch.delenv("LITAI_WRITER_API_KEY", raising=False)
            monkeypatch.setenv("LITAI_WRITER_FALLBACK_BACKEND", writer_fallback_backend)

        monkeypatch.setattr("app.api.writer.update_env_file", fake_update_env_file)

        response = client.post("/api/writer/settings", json=payload)
        assert response.status_code == 200
        res_data = response.json()
        assert res_data["writer_backend"] == "openai_compatible"
        assert res_data["writer_model"] == "test-model-abc"
        assert res_data["writer_api_base"] == "https://test-api.example.com"
        # 密钥应该自动被遮蔽为 ******
        assert res_data["writer_api_key"] == "******"
        assert res_data["writer_fallback_backend"] == "rule"

        # 检查内存中的设置也已即时更新
        settings = get_settings()
        assert settings.writer_backend == "openai_compatible"
        assert settings.writer_model == "test-model-abc"
        assert settings.writer_api_base == "https://test-api.example.com"
        assert settings.writer_api_key == "sk-test-key-123456"

        # 验证 fake_env 确实也被正确写入了
        with open(fake_env, "r", encoding="utf-8") as f:
            content = f.read()
        assert "LITAI_WRITER_BACKEND=openai_compatible" in content
        assert "LITAI_WRITER_MODEL=test-model-abc" in content
        assert "LITAI_WRITER_API_KEY=sk-test-key-123456" in content

        # 3. 再次获取配置，密钥被屏蔽
        response = client.get("/api/writer/settings")
        assert response.status_code == 200
        get_data = response.json()
        assert get_data["writer_api_key"] == "******"

        # 4. 如果使用 ****** 提交更新，不修改 Key
        payload_no_key_change = {
            "writer_backend": "rule",
            "writer_model": "test-model-abc",
            "writer_api_base": "https://test-api.example.com",
            "writer_api_key": "******",
            "writer_fallback_backend": "llm_stub",
        }
        response = client.post("/api/writer/settings", json=payload_no_key_change)
        assert response.status_code == 200
        res_data2 = response.json()
        assert res_data2["writer_backend"] == "rule"
        assert res_data2["writer_fallback_backend"] == "llm_stub"
        # 验证内存里 API Key 没有被改写为 ****** 而是保持原状
        assert settings.writer_api_key == "sk-test-key-123456"
