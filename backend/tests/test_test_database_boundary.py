from __future__ import annotations

import pytest

from conftest import validated_test_root_database_url


pytestmark = pytest.mark.no_test_database


@pytest.mark.parametrize("url", [
    None,
    "postgresql+psycopg://user:pass@127.0.0.1:55432/literature_ai",
    "sqlite:///tmp/literature_ai_test.db",
])
def test_test_database_url_rejects_missing_production_and_non_postgres_before_connect(url):
    with pytest.raises(RuntimeError):
        validated_test_root_database_url(url)


def test_test_database_url_accepts_only_explicit_test_database():
    url = "postgresql+psycopg://user:pass@127.0.0.1:55432/literature_ai_test"
    assert validated_test_root_database_url(url) == url
