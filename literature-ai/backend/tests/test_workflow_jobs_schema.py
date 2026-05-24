import tempfile
from pathlib import Path

from sqlalchemy import inspect

from app.db import session as db_session


def test_init_db_creates_workflow_jobs_table():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "schema_check.db"
        db_url = f"sqlite:///{db_path}"

        db_session.init_db(db_url)

        engine = db_session.get_engine(db_url)
        inspector = inspect(engine)
        assert "workflow_jobs" in inspector.get_table_names()

        engine.dispose()
        db_session._session_factories.pop(db_url, None)
        db_session._engines.pop(db_url, None)
