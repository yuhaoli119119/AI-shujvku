from __future__ import annotations
import json
from uuid import uuid4
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from app.config import get_settings
from app.db.models import AuditLog, DFTResult, Paper, PaperFigure, PaperSection, PaperTable
from app.review_v2.executor import PaperReviewV2Service
from app.review_v2.figure_types import REGISTRY, public_registry, resolve_query
from app.review_v2.models import PaperReviewBatchRequest
from app.review_v2.search import FigureSearchService
from app.review_v2.state_machine import assert_contract

pytestmark = pytest.mark.postgres

def _typing(page=1):
    return {
        "figure_type_primary": "projected_density_of_states",
        "figure_types": ["projected_density_of_states", "density_of_states"],
        "panel_types": [{"label": "(b)", "figure_types": ["projected_density_of_states"]}],
        "figure_role": "electronic structure evidence",
        "type_confidence": 0.93,
        "type_evidence": [{"page": page, "quote": "PDOS panel", "evidence_type": "caption"}],
    }

def _seed(factory):
    with factory.begin() as db:
        paper=Paper(paper_code="T-V2", title="V2 test", authors=[], pdf_path="missing.pdf", year=2026)
        db.add(paper); db.flush()
        figure=PaperFigure(paper_id=paper.id, figure_label="Fig. 1", page=1, caption="PDOS panel", figure_role="legacy_custom_role", write_version=1)
        table=PaperTable(paper_id=paper.id, page=2, caption="Table 1", markdown_content="|a|\n|-|")
        db.add_all([figure,table]); db.flush()
        return paper.id,figure.id,table.id

def _request(task, *, request_id="review-v2-test-001", failing_create=False):
    figure=task["figures"][0]; table=task["tables"][0]
    actions=[]
    if failing_create:
        actions.append({"action":"CREATE","source_paper_id":task["paper_id"],"page":1,"bbox_norm":[0,0,1,1],"reason":"exercise savepoint"})
    return PaperReviewBatchRequest.model_validate({
        "paper_id":task["paper_id"],"request_id":request_id,"task_fingerprint":task["task_fingerprint"],
        "figure_actions":actions,
        "table_actions":[{"action":"KEEP","table_id":table["table_id"],"expected_object_version":table["object_version"],"reason":"verified"}],
        "figure_readings":[{
            "figure_id":figure["figure_id"],"source_paper_id":figure["source_paper_id"],"expected_object_version":figure["object_version"],
            "summary_zh":"该图展示投影态密度。","detailed_explanation_zh":"该复合图用能量轴上的轨道投影态密度比较电子结构，子图(b)明确属于PDOS；图注证据支持类型判断。",
            "evidence_locators":[{"page":1,"quote":"PDOS panel","evidence_type":"caption"}],"subfigures":[{"label":"(b)","description":"子图(b)展示能量轴上的轨道投影态密度，用于识别费米能级附近的轨道贡献。"}],"typing":_typing(),
        }],
    })

@pytest.mark.no_test_database
@pytest.mark.unit
def test_registry_aliases_and_extension_point():
    assert resolve_query("台阶图")=={"free_energy_diagram"}
    assert resolve_query("PDOS")=={"projected_density_of_states"}
    assert resolve_query("充放电循环图")=={"charge_discharge_profile","cycling_performance"}
    assert "other" in REGISTRY
    assert public_registry()["version"].startswith("figure-types-")

@pytest.mark.no_test_database
@pytest.mark.unit
def test_flat_schema_has_no_conditional_maze_and_flexible_reading():
    schema=PaperReviewBatchRequest.model_json_schema()
    def keys(value):
        if isinstance(value,dict):
            yield from value.keys()
            for child in value.values(): yield from keys(child)
        elif isinstance(value,list):
            for child in value: yield from keys(child)
    assert not ({"allOf","if","then"} & set(keys(schema)))
    reading=schema["$defs"]["FigureReadingInput"]
    assert "summary_zh" in reading["properties"] and "detailed_explanation_zh" in reading["properties"]
    assert "subfigures" not in reading["required"]

@pytest.mark.no_test_database
@pytest.mark.unit
def test_completed_contract():
    assert_contract("completed",0)
    with pytest.raises(ValueError,match="completed_requires"):
        assert_contract("completed",1)

def test_postgres_batch_savepoints_receipt_versions_search_and_stale(setup_test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LITAI_STORAGE_ROOT",str(tmp_path/"storage")); get_settings.cache_clear()
    factory=sessionmaker(bind=setup_test_db,autoflush=False,autocommit=False,future=True)
    paper_id,figure_id,table_id=_seed(factory)
    with factory.begin() as db:
        service=PaperReviewV2Service(db,get_settings()); task=service.get_task(paper_id)
        assert task["figures"][0]["legacy_type_mapping"]["status"]=="unmapped"
        req=_request(task,failing_create=True)
        dft_before=db.scalar(select(func.count()).select_from(DFTResult))
        result=service.apply(req)
        assert len(result["rejected"])==1
        assert len(result["applied"])==1
        assert len(result["unchanged"])==1
        assert result["authoritative_readback"]["stage_status"]=="completed_with_issues"
        assert result["authoritative_readback"]["unresolved_count"]==1
        assert db.scalar(select(func.count()).select_from(DFTResult))==dft_before
        found=FigureSearchService(db).search(query="PDOS")
        assert found["count"]==1
        assert found["items"][0]["matched_panel_types"][0]["label"]=="(b)"
        receipt_id=result["receipt_audit_id"]
        replay=service.apply(req)
        assert replay["idempotent_replay"] is True and replay["receipt_audit_id"]==receipt_id
        changed=req.model_copy(deep=True); changed.notes=["different"]
        with pytest.raises(ValueError,match="request_id_payload_conflict"): service.apply(changed)
    with factory.begin() as db:
        task=PaperReviewV2Service(db,get_settings()).get_task(paper_id)
        assert task["status"]["chart_review_status"]=="completed_with_issues"
        fig=db.get(PaperFigure,figure_id); fig.caption="changed source"; db.add(fig)
    with factory.begin() as db:
        task=PaperReviewV2Service(db,get_settings()).get_task(paper_id)
        assert task["status"]["chart_review_status"]=="stale"

def test_old_task_fingerprint_and_object_version_rejected(setup_test_db,tmp_path,monkeypatch):
    monkeypatch.setenv("LITAI_STORAGE_ROOT",str(tmp_path/"storage")); get_settings.cache_clear()
    factory=sessionmaker(bind=setup_test_db,autoflush=False,autocommit=False,future=True)
    paper_id,figure_id,table_id=_seed(factory)
    with factory.begin() as db:
        service=PaperReviewV2Service(db,get_settings()); task=service.get_task(paper_id)
        req=_request(task,request_id="review-v2-test-002")
        req.task_fingerprint="0"*64
        with pytest.raises(ValueError,match="stale_task_fingerprint"): service.apply(req)
    with factory.begin() as db:
        service=PaperReviewV2Service(db,get_settings()); task=service.get_task(paper_id)
        req=_request(task,request_id="review-v2-test-003"); req.figure_readings[0].expected_object_version="999"
        with pytest.raises(ValueError,match="figure_version_conflict"): service.apply(req)

def test_false_positive_delete_keeps_audit_snapshot(setup_test_db,tmp_path,monkeypatch):
    monkeypatch.setenv("LITAI_STORAGE_ROOT",str(tmp_path/"storage")); get_settings.cache_clear()
    factory=sessionmaker(bind=setup_test_db,autoflush=False,autocommit=False,future=True)
    paper_id,figure_id,table_id=_seed(factory)
    with factory.begin() as db:
        service=PaperReviewV2Service(db,get_settings()); task=service.get_task(paper_id)
        f=task["figures"][0]; t=task["tables"][0]
        req=PaperReviewBatchRequest.model_validate({"paper_id":str(paper_id),"request_id":"review-v2-test-004","task_fingerprint":task["task_fingerprint"],
          "figure_actions":[{"action":"HOLD","figure_id":f["figure_id"],"expected_object_version":f["object_version"],"reason":"not reviewed"}],
          "table_actions":[{"action":"DELETE_FALSE_POSITIVE","table_id":t["table_id"],"expected_object_version":t["object_version"],"reason":"reference list","evidence":[{"page":2,"quote":"References","evidence_type":"body_text"}]}],
          "final_state":"completed_with_issues"})
        result=service.apply(req)
        assert db.get(PaperTable,table_id) is None
        audit=db.scalar(select(AuditLog).where(AuditLog.action=="paper_review_v2_delete_false_positive",AuditLog.target_id==str(table_id)))
        assert audit.payload["before"]["caption"]=="Table 1"
        assert str(table_id) not in result["authoritative_readback"]["active_table_order"]


@pytest.mark.no_test_database
@pytest.mark.unit
def test_all_v2_state_contracts_are_distinct():
    from app.review_v2.state_machine import VALID_STATES, final_state
    assert VALID_STATES == {"in_progress","completed","completed_with_issues","blocked","stale","not_required"}
    assert final_state(item_count=2,held=0,rejected=0,requested="completed")=="completed"
    assert final_state(item_count=2,held=1,rejected=0,requested="completed")=="completed_with_issues"
    assert final_state(item_count=2,held=0,rejected=0,requested="blocked")=="blocked"
    assert final_state(item_count=0,held=0,rejected=0,requested="completed")=="not_required"

def test_compose_pages_order_provenance_and_readability(setup_test_db,tmp_path,monkeypatch):
    import fitz
    from PIL import Image
    monkeypatch.setenv("LITAI_STORAGE_ROOT",str(tmp_path/"storage")); get_settings.cache_clear()
    pdf=tmp_path/"two-pages.pdf"; doc=fitz.open()
    for label in ("FIRST PAGE ROWS","SECOND PAGE ROWS"):
        page=doc.new_page(width=300,height=400); page.insert_text((40,80),label,fontsize=18)
        page.draw_rect(fitz.Rect(35,45,265,180),color=(0,0,0),fill=(0.9,0.9,0.9))
        page.insert_text((45,130),"scientific panels",fontsize=14)
    doc.save(pdf); doc.close()
    factory=sessionmaker(bind=setup_test_db,autoflush=False,autocommit=False,future=True)
    with factory.begin() as db:
        paper=Paper(paper_code="T-COMPOSE",title="compose",authors=[],pdf_path=str(pdf))
        db.add(paper); db.flush()
        fig=PaperFigure(paper_id=paper.id,figure_label="Figure S35",page=2,caption="two-page figure",write_version=1)
        db.add(fig); db.flush(); pid,fid=paper.id,fig.id
    with factory.begin() as db:
        svc=PaperReviewV2Service(db,get_settings()); task=svc.get_task(pid); f=task["figures"][0]
        req=PaperReviewBatchRequest.model_validate({"paper_id":str(pid),"request_id":"review-v2-compose-001","task_fingerprint":task["task_fingerprint"],"figure_actions":[{"action":"COMPOSE_PAGES","figure_id":str(fid),"expected_object_version":f["object_version"],"pages":[{"page":1,"bbox_norm":[0.05,0.05,0.95,0.50]},{"page":2,"bbox_norm":[0.05,0.05,0.95,0.50]}],"reason":"logical figure spans two pages"}]})
        result=svc.apply(req)
        assert result["authoritative_readback"]["stage_status"]=="completed"
        row=db.get(PaperFigure,fid); assert row.crop_status=="composed_pages"
        assert row.prov[-1]["page_range"]==[1,2]
        out=get_settings().storage_paths["figures"]/row.image_path
        image=Image.open(out); assert image.width>400 and image.height>600


@pytest.mark.no_test_database
@pytest.mark.unit
def test_compose_pages_rejects_duplicate_or_reverse_page_order():
    base = {
        "paper_id": str(uuid4()),
        "request_id": "review-v2-order-001",
        "task_fingerprint": "0" * 64,
        "figure_actions": [{
            "action": "COMPOSE_PAGES",
            "figure_id": str(uuid4()),
            "expected_object_version": "1",
            "pages": [
                {"page": 2, "bbox_norm": [0, 0, 1, 1]},
                {"page": 2, "bbox_norm": [0, 0, 1, 1]},
            ],
            "reason": "invalid order",
        }],
    }
    with pytest.raises(ValueError, match="strictly increasing"):
        PaperReviewBatchRequest.model_validate(base)


def test_referenced_body_text_change_marks_reading_stale(setup_test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LITAI_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    paper_id, figure_id, _ = _seed(factory)
    with factory.begin() as db:
        section = PaperSection(
            paper_id=paper_id,
            section_title="Results",
            section_type="results",
            text="As shown in Figure 1, the orbital response is resolved.",
            page_start=1,
            page_end=1,
        )
        db.add(section)
    with factory.begin() as db:
        service = PaperReviewV2Service(db, get_settings())
        task = service.get_task(paper_id)
        result = service.apply(_request(task, request_id="review-v2-body-001"))
        assert result["authoritative_readback"]["stage_status"] == "completed"
    with factory.begin() as db:
        task = PaperReviewV2Service(db, get_settings()).get_task(paper_id)
        assert task["status"]["chart_review_status"] == "completed"
        section = db.scalar(select(PaperSection).where(PaperSection.paper_id == paper_id))
        section.text = "As shown in Figure 1, the corrected orbital response differs."
        db.add(section)
    with factory.begin() as db:
        task = PaperReviewV2Service(db, get_settings()).get_task(paper_id)
        assert task["status"]["chart_review_status"] == "stale"
        assert task["status"]["figure_reading_coverage"] == {"completed": 0, "total": 1}


@pytest.mark.no_test_database
@pytest.mark.unit
def test_business_services_are_registry_driven_without_per_type_branches():
    import inspect
    from app.review_v2 import executor, search
    business_source = inspect.getsource(executor) + inspect.getsource(search)
    for key in (item for item in REGISTRY if item != "other"):
        assert ('"' + key + '"') not in business_source
        assert ("'" + key + "'") not in business_source


@pytest.mark.no_test_database
@pytest.mark.unit
def test_subfigure_descriptions_are_chinese_nonempty_and_match_panel_labels():
    base = {
        "figure_id": str(uuid4()), "source_paper_id": str(uuid4()), "expected_object_version": "1",
        "summary_zh": "复合图摘要。", "detailed_explanation_zh": "复合图详细说明。",
        "evidence_locators": [{"page": 1}], "typing": _typing(),
    }
    with pytest.raises(ValueError, match="subfigure labels must exactly match"):
        PaperReviewBatchRequest.model_validate({"paper_id": str(uuid4()), "request_id": "subfigure-test-001", "task_fingerprint": "0" * 64, "figure_readings": [base]})
    bad = {**base, "subfigures": [{"label": "(b)", "description": "PDOS only"}]}
    with pytest.raises(ValueError, match="must contain non-empty Chinese"):
        PaperReviewBatchRequest.model_validate({"paper_id": str(uuid4()), "request_id": "subfigure-test-002", "task_fingerprint": "0" * 64, "figure_readings": [bad]})
    wrong = {**base, "subfigures": [{"label": "(a)", "description": "子图说明与类型标签不一致。"}]}
    with pytest.raises(ValueError, match="subfigure labels must exactly match"):
        PaperReviewBatchRequest.model_validate({"paper_id": str(uuid4()), "request_id": "subfigure-test-003", "task_fingerprint": "0" * 64, "figure_readings": [wrong]})


def test_database_image_path_missing_marks_task_and_readback_stale(setup_test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LITAI_STORAGE_ROOT", str(tmp_path / "storage")); get_settings.cache_clear()
    factory=sessionmaker(bind=setup_test_db,autoflush=False,autocommit=False,future=True)
    with factory.begin() as db:
        paper=Paper(paper_code="T-MISSING-ASSET",title="missing asset",authors=[],pdf_path="missing.pdf")
        db.add(paper); db.flush()
        figure=PaperFigure(paper_id=paper.id,figure_label="Fig. 2",page=2,caption="single panel",image_path=f"{paper.id}/missing.png",write_version=1)
        db.add(figure); db.flush(); paper_id,figure_id=paper.id,figure.id
    with factory.begin() as db:
        service=PaperReviewV2Service(db,get_settings()); task=service.get_task(paper_id)
        assert task["status"]["chart_review_status"]=="stale"
        assert task["status"]["asset_status"]=="asset_missing"
        assert task["status"]["missing_image_count"]==1
        assert task["status"]["missing_image_labels"]==["Fig. 2"]
        figure_task=task["figures"][0]
        req=PaperReviewBatchRequest.model_validate({"paper_id":str(paper_id),"request_id":"missing-asset-001","task_fingerprint":task["task_fingerprint"],"final_state":"completed_with_issues","figure_readings":[{"figure_id":str(figure_id),"source_paper_id":str(paper_id),"expected_object_version":figure_task["object_version"],"summary_zh":"单图摘要。","detailed_explanation_zh":"该图用于验证缺失图片状态。","evidence_locators":[{"page":2}],"typing":{"figure_type_primary":"performance_comparison","figure_types":["performance_comparison"],"panel_types":[],"type_confidence":0.9,"type_evidence":[{"page":2}]}}]})
        result=service.apply(req)
        assert result["authoritative_readback"]["stage_status"]=="stale"
        assert result["authoritative_readback"]["missing_image_count"]==1
        assert result["authoritative_readback"]["missing_image_labels"]==["Fig. 2"]
