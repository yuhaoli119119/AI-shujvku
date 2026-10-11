"""Settings must advertise the new data path and fail closed if unavailable."""
import asyncio
from pathlib import Path
import pytest
from fastapi import HTTPException
from app.api import settings
from app import config

pytestmark = pytest.mark.no_test_database


def test_current_workflow_has_no_retired_protocols(monkeypatch):
    monkeypatch.setattr(config, 'PROJECT_ROOT', Path('/'))
    payload = asyncio.run(settings.get_extraction_protocols())
    assert payload['schema_version'] == 'extraction_protocols_v2'
    assert {p['key'] for p in payload['items']} == {'workflow', 'evidence', 'analysis', 'source'}
    assert len(payload['revision']) == 12
    text = '\n'.join(item['raw_text'] for item in payload['items'])
    assert '不同文章名称相同' in text
    assert '不能按提取日期' in payload['items'][3]['scope']
    assert 'system_candidate' not in text
    assert 'ai_verify_content' not in text


def test_missing_current_spec_does_not_fall_back_to_old_protocol(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'PROJECT_ROOT', tmp_path)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(settings.get_extraction_protocols())
    assert exc.value.status_code == 503
