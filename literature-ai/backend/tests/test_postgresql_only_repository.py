from pathlib import Path
import subprocess


def test_repository_is_postgresql_only():
    project_root = Path(__file__).resolve().parents[2]
    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    )
    blocked_token = "sql" + "ite"
    blocked_suffixes = {"." + blocked_token, "." + blocked_token + "3", ".db"}
    offenders: list[str] = []

    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    for relative in result.stdout.decode("utf-8").split("\0"):
        if not relative:
            continue
        path = repo_root / relative
        if not path.is_file():
            continue
        if path.suffix.lower() in blocked_suffixes or blocked_token in relative.lower():
            offenders.append(relative)
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if blocked_token in content.lower():
            offenders.append(relative)

    assert offenders == []


def test_removed_database_file_endpoints_are_not_registered():
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/api/system/switch-db" not in paths
    assert "/api/system/upload-db" not in paths


def test_runtime_database_info_has_no_file_database_fields():
    from app.utils.active_database import get_active_database_info

    info = get_active_database_info()
    removed_keys = {
        "db_path",
        "configured_db_path",
        "active_library_db_path",
        "matches_active_library_db_path",
        "configured_matches_active_library_db_path",
        "effective_db_path",
        "effective_matches_active_library_db_path",
        "recovered_from_candidate_scan",
    }
    assert removed_keys.isdisjoint(info)
