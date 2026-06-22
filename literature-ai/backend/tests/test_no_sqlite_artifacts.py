from pathlib import Path


def test_repository_contains_no_sqlite_database_artifacts():
    repo_root = Path(__file__).resolve().parents[2]
    blocked_suffixes = {".sqlite", ".sqlite3", ".db"}
    ignored_parts = {".git", ".pytest_cache", "node_modules", "__pycache__"}

    offenders = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in ignored_parts for part in path.parts):
            continue
        if path.suffix.lower() in blocked_suffixes:
            offenders.append(path.relative_to(repo_root).as_posix())

    assert offenders == []
