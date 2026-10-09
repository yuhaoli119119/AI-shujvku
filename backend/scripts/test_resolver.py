import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(backend_dir))

from app.config import get_settings
from app.utils.artifact_paths import resolve_persisted_artifact_path

def test_resolver():
    settings = get_settings()
    # Mock settings.storage_root to be like 'data/storage'
    settings.storage_root = Path("data/storage")
    
    test_paths = [
        ("storage/pdf/879a9890-b798-444d-bb2c-e196ddbed5e5_04_10.1038_s41598-024-56380-z.pdf", "pdf"),
        ("storage/markdown/879a9890-b798-444d-bb2c-e196ddbed5e5_04_10.1038_s41598-024-56380-z.md", "markdown"),
        ("by_id/9517f22e-7ab3-4b15-861c-54d38d002105", None)
    ]
    
    for p, cat in test_paths:
        res = resolve_persisted_artifact_path(p, category=cat, settings=settings)
        print(f"Resolving '{p}' (category={cat}) -> {res} (Exists: {res.exists() if res else False})")

if __name__ == '__main__':
    test_resolver()
