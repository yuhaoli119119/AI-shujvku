from concurrent.futures import ThreadPoolExecutor
from time import perf_counter

from sqlalchemy import select, text

from app.db.models import Paper, RebuildVisualAsset, RebuildPaperFile
from app.services import rebuild_mutation_audit, rebuild_acceptance
from app.services.rebuild_workflow_service import prepare_visual_asset
from test_rebuild_mutation_audit import audit_env, assets
from test_rebuild_acceptance import base, freeze_body


def test_audit_snapshots_only_touched_asset_even_in_large_paper(audit_env, monkeypatch):
    env = audit_env
    with env["factory"]() as session:
        session.add_all([RebuildVisualAsset(paper_id=env["paper"], asset_key=f"old-{i}", asset_type="table", context_text="stored fixture") for i in range(600)])
        session.commit()
    calls = []
    original = rebuild_mutation_audit._snapshot
    def observed(session, paper_id, operation, *, entity_id=None):
        calls.append(entity_id)
        assert entity_id is not None, "auditing a single target must not scan whole paper"
        result = original(session, paper_id, operation, entity_id=entity_id)
        assert len(result) <= 1
        return result
    monkeypatch.setattr(rebuild_mutation_audit, "_snapshot", observed)
    start = perf_counter()
    response = env["client"].post(assets(env), json={"asset_key": "old-321", "asset_type": "table", "request_id": "one-target",
                                                  "context_text": "1.00s"})
    elapsed = perf_counter() - start
    assert response.status_code == 200, response.text
    assert len(calls) == 2  # exactly target before and target after
    print(f"single_asset_600_existing: elapsed_seconds={elapsed:.4f}, audit_entity_reads={len(calls)}")


def test_repeated_status_read_uses_stat_bound_image_hash_cache(audit_env, monkeypatch):
    env = audit_env
    crop = {"asset_key": "cached-image", "asset_type": "table", "file_id": str(env["file"]), "context_text": "s 1.00s",
            "request_id": "crop", "regions": [{"page_number": 1, "bbox": [0.0, 0.0, 1.0, 1.0]}]}
    response = env["client"].post(assets(env) + "/crop", json=crop)
    assert response.status_code == 200, response.text
    frozen = env["client"].post(base(env) + "/freeze", json=freeze_body(env))
    assert frozen.status_code == 200
    rebuild_acceptance._hash_at_stat.cache_clear()
    calls = []
    original = rebuild_acceptance._sha256
    def count(path):
        calls.append(str(path)); return original(path)
    monkeypatch.setattr(rebuild_acceptance, "_sha256", count)
    for _ in range(3):
        assert env["client"].get(base(env)).status_code == 200
    image = env["settings"].storage_root / response.json()["asset"]["image_path"]
    assert calls.count(str(image)) == 1, calls
    # Source PDF and output image now share one stat-bound cache, and the
    # reading-PDF identity is part of every revision, not merely table fallback.
    assert len(calls) == 2, calls
    image.write_bytes(image.read_bytes() + b"synthetic-new-image-version")
    assert env["client"].get(base(env)).json()["acceptance"]["stale"]
    assert calls.count(str(image)) == 2 and len(calls) == 3
    print("three_status_reads: PNG_content_reads=1; changed_file_rehash=1")


def test_acceptance_lock_is_per_paper_and_other_paper_write_proceeds(audit_env):
    env = audit_env
    with env["factory"]() as session:
        other = Paper(title="Independent fixture", paper_code="OTHER999", pdf_path="other.pdf")
        session.add(other); session.commit(); other_id = other.id
    with env["factory"]() as locked:
        rebuild_acceptance._locked_paper(locked, env["paper"])
        def update_other():
            with env["factory"]() as session:
                session.execute(text("SET LOCAL statement_timeout = '1000ms'"))
                session.scalar(select(Paper.id).where(Paper.id == other_id).with_for_update())
                session.add(RebuildVisualAsset(paper_id=other_id, asset_key="parallel", asset_type="table", context_text="independent"))
                session.commit()
                return True
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(update_other).result(timeout=3)
        locked.rollback()
    print("acceptance held on paper A: independent paper B INSERT committed within 1s DB timeout")
