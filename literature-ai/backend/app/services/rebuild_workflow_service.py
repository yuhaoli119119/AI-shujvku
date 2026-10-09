from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from pydantic import ValidationError
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.exc import DataError, IntegrityError

from app.config import Settings
from app.db.models import (
    Paper,
    RebuildAnalysisRun,
    RebuildDataRow,
    RebuildDataValue,
    RebuildPaperFile,
    RebuildValueSource,
    RebuildVisualAsset,
)
from app.schemas.rebuild import (
    RebuildAnalysisRequest,
    RebuildDataRowInput,
    RebuildDataValueInput,
    RebuildValueCorrectionRequest,
    RebuildRowIdentityCorrectionRequest,
    RebuildVisualAssetRequest,
)


TEMPLATE_VERSION = "rebuild_v1"

COMMON_TEMPLATE_FIELDS: list[dict[str, str]] = [
    {"name": "surface_area_m2_g", "label": "比表面积", "unit": "m²/g", "kind": "numeric"},
    {"name": "pore_volume_cm3_g", "label": "孔体积", "unit": "cm³/g", "kind": "numeric"},
    {"name": "graphyne_derivative", "label": "石墨炔衍生物", "unit": "", "kind": "boolean"},
    {"name": "graphyne_substitution", "label": "石墨炔取代/官能团", "unit": "", "kind": "text"},
    {"name": "structure_model", "label": "结构模型", "unit": "", "kind": "text"},
    {"name": "coordination_number", "label": "配位数", "unit": "", "kind": "numeric"},
    {"name": "electrical_conductivity_s_m", "label": "电导率", "unit": "S/m", "kind": "numeric"},
    {"name": "band_gap_ev", "label": "带隙", "unit": "eV", "kind": "numeric"},
    {"name": "pdos_hybridization_descriptor", "label": "pDOS 杂化描述", "unit": "", "kind": "text"},
    {"name": "charge_transfer_descriptor", "label": "电荷转移描述", "unit": "", "kind": "text"},
    {"name": "work_function_ev", "label": "功函数", "unit": "eV", "kind": "numeric"},
    {"name": "ni_2p3_2_ev", "label": "Ni 2p₃/₂", "unit": "eV", "kind": "numeric"},
    {"name": "ag_3d5_2_ev", "label": "Ag 3d₅/₂", "unit": "eV", "kind": "numeric"},
    {"name": "d_band_center_ev", "label": "d 带中心", "unit": "eV", "kind": "numeric"},
    {"name": "charge_density_difference_e", "label": "电荷密度差", "unit": "e", "kind": "numeric"},
    {"name": "adsorbate", "label": "吸附物种", "unit": "", "kind": "text"},
    {"name": "adsorption_site", "label": "吸附位点", "unit": "", "kind": "text"},
    {"name": "adsorption_free_energy_ev", "label": "吸附自由能", "unit": "eV", "kind": "numeric"},
    {"name": "binding_energy_ev", "label": "结合能", "unit": "eV", "kind": "numeric"},
    {"name": "stability_descriptor", "label": "稳定性描述", "unit": "", "kind": "text"},
    {"name": "adsorption_energy_ev", "label": "吸附能", "unit": "eV", "kind": "numeric"},
    {"name": "formation_energy_ev", "label": "形成能", "unit": "eV", "kind": "numeric"},
    {"name": "dissolution_potential_v", "label": "溶解电位", "unit": "V", "kind": "numeric"},
    {"name": "migration_barrier_ev", "label": "迁移势垒", "unit": "eV", "kind": "numeric"},
    {"name": "double_layer_capacitance_mf_cm2", "label": "双电层电容", "unit": "mF/cm²", "kind": "numeric"},
    {"name": "sulfur_poisoning_tolerant", "label": "硫中毒耐受性", "unit": "", "kind": "boolean"},
    {"name": "sulfur_poisoning_species", "label": "硫物种", "unit": "", "kind": "text"},
    {"name": "sulfur_adsorption_energy_ev", "label": "硫吸附能", "unit": "eV", "kind": "numeric"},
    {"name": "sulfur_poisoning_notes", "label": "硫中毒说明", "unit": "", "kind": "text"},
]

REACTION_TEMPLATE_FIELDS: dict[str, list[dict[str, str]]] = {
    "SRR": [
        {"name": "onset_potential_v", "label": "起始电位", "unit": "V", "kind": "numeric"},
        {"name": "peak_potential_v", "label": "峰电位", "unit": "V", "kind": "numeric"},
        {"name": "faradaic_efficiency_percent", "label": "法拉第效率", "unit": "%", "kind": "numeric"},
        {"name": "current_density_ma_cm2", "label": "电流密度", "unit": "mA/cm²", "kind": "numeric"},
        {"name": "selectivity_percent", "label": "选择性", "unit": "%", "kind": "numeric"},
        {"name": "h2o2_production_rate_mol_g_h", "label": "H₂O₂ 产率", "unit": "mol/g/h", "kind": "numeric"},
        {"name": "stability_hours", "label": "稳定性时长", "unit": "h", "kind": "numeric"},
    ],
    "HER": [
        {"name": "overpotential_mv_10", "label": "10 mA/cm² 过电位", "unit": "mV", "kind": "numeric"},
        {"name": "overpotential_mv_100", "label": "100 mA/cm² 过电位", "unit": "mV", "kind": "numeric"},
        {"name": "tafel_slope_mv_dec", "label": "Tafel 斜率", "unit": "mV/dec", "kind": "numeric"},
        {"name": "exchange_current_density_ma_cm2", "label": "交换电流密度", "unit": "mA/cm²", "kind": "numeric"},
        {"name": "stability_hours", "label": "稳定性时长", "unit": "h", "kind": "numeric"},
    ],
    "OER": [
        {"name": "overpotential_mv_10", "label": "10 mA/cm² 过电位", "unit": "mV", "kind": "numeric"},
        {"name": "overpotential_mv_100", "label": "100 mA/cm² 过电位", "unit": "mV", "kind": "numeric"},
        {"name": "tafel_slope_mv_dec", "label": "Tafel 斜率", "unit": "mV/dec", "kind": "numeric"},
        {"name": "current_density_ma_cm2", "label": "电流密度", "unit": "mA/cm²", "kind": "numeric"},
        {"name": "stability_hours", "label": "稳定性时长", "unit": "h", "kind": "numeric"},
    ],
    "ORR": [
        {"name": "onset_potential_v", "label": "起始电位", "unit": "V", "kind": "numeric"},
        {"name": "half_wave_potential_v", "label": "半波电位", "unit": "V", "kind": "numeric"},
        {"name": "limiting_current_density_ma_cm2", "label": "极限电流密度", "unit": "mA/cm²", "kind": "numeric"},
        {"name": "electron_transfer_number", "label": "电子转移数", "unit": "e⁻", "kind": "numeric"},
        {"name": "h2o2_yield_percent", "label": "H₂O₂ 产率", "unit": "%", "kind": "numeric"},
        {"name": "stability_hours", "label": "稳定性时长", "unit": "h", "kind": "numeric"},
    ],
    "CO2RR": [
        {"name": "onset_potential_v", "label": "起始电位", "unit": "V", "kind": "numeric"},
        {"name": "potential_at_10_ma_cm2_v", "label": "10 mA/cm² 电位", "unit": "V vs RHE", "kind": "numeric"},
        {"name": "current_density_ma_cm2", "label": "电流密度", "unit": "mA/cm²", "kind": "numeric"},
        {"name": "co_partial_current_density_ma_cm2", "label": "CO 分电流密度", "unit": "mA/cm²", "kind": "numeric"},
        {"name": "tafel_slope_mv_dec", "label": "Tafel 斜率", "unit": "mV/dec", "kind": "numeric"},
        {"name": "charge_transfer_resistance_ohm", "label": "电荷转移电阻", "unit": "Ω", "kind": "numeric"},
        {"name": "oh_adsorption_potential_v", "label": "OH 吸附电位", "unit": "V vs RHE", "kind": "numeric"},
        {"name": "co2_adsorption_capacity_mmol_g", "label": "CO₂ 吸附量", "unit": "mmol/g", "kind": "numeric"},
        {"name": "gibbs_free_energy_cooh_ev", "label": "*COOH 生成自由能", "unit": "eV", "kind": "numeric"},
        {"name": "limiting_potential_difference_v", "label": "极限电位差 UL(CO₂)-UL(H₂)", "unit": "V vs RHE", "kind": "numeric"},
        {"name": "fe_co_percent", "label": "CO 法拉第效率", "unit": "%", "kind": "numeric"},
        {"name": "fe_h2_percent", "label": "H₂ 法拉第效率", "unit": "%", "kind": "numeric"},
        {"name": "fe_formate_percent", "label": "甲酸盐法拉第效率", "unit": "%", "kind": "numeric"},
        {"name": "fe_ethanol_percent", "label": "乙醇法拉第效率", "unit": "%", "kind": "numeric"},
        {"name": "stability_hours", "label": "稳定性时长", "unit": "h", "kind": "numeric"},
    ],
}

IDENTITY_FIELDS = (
    "reaction",
    "material",
    "support",
    "active_site_type",
    "active_site",
    "configuration",
    "material_family",
    "data_type",
)


class RebuildWorkflowError(ValueError):
    pass


def reaction_templates() -> dict[str, Any]:
    return {
        "version": TEMPLATE_VERSION,
        "active_site_types": [
            {"value": "single_atom", "label": "单原子"},
            {"value": "dual_atom", "label": "双原子"},
            {"value": "cluster", "label": "团簇"},
            {"value": "support", "label": "载体"},
            {"value": "bulk", "label": "体相"},
            {"value": "other", "label": "其他"},
        ],
        "data_types": [
            {"value": "experimental", "label": "实验"},
            {"value": "dft", "label": "DFT"},
        ],
        "common_fields": COMMON_TEMPLATE_FIELDS,
        "reactions": {
            reaction: {"label": label, "fields": fields}
            for reaction, (label, fields) in {
                "SRR": ("硫氧化还原反应", REACTION_TEMPLATE_FIELDS["SRR"]),
                "HER": ("析氢反应", REACTION_TEMPLATE_FIELDS["HER"]),
                "OER": ("析氧反应", REACTION_TEMPLATE_FIELDS["OER"]),
                "ORR": ("氧还原反应", REACTION_TEMPLATE_FIELDS["ORR"]),
                "CO2RR": ("二氧化碳还原反应", REACTION_TEMPLATE_FIELDS["CO2RR"]),
            }.items()
        },
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_hash(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(f"{prefix}|{_canonical_json(value)}".encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _required_paper(session: Session, paper_id: UUID) -> Paper:
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise RebuildWorkflowError("paper_not_found")
    return paper


def _paper_counts(session: Session, paper_id: UUID) -> dict[str, int]:
    return {
        "files": session.scalar(select(func.count()).select_from(RebuildPaperFile).where(RebuildPaperFile.paper_id == paper_id)) or 0,
        "assets": session.scalar(select(func.count()).select_from(RebuildVisualAsset).where(RebuildVisualAsset.paper_id == paper_id)) or 0,
        "rows": session.scalar(select(func.count()).select_from(RebuildDataRow).where(RebuildDataRow.paper_id == paper_id)) or 0,
    }


def serialize_paper(session: Session, paper: Paper) -> dict[str, Any]:
    return {
        "id": str(paper.id),
        "paper_code": paper.paper_code,
        "serial_number": paper.serial_number,
        "title": paper.title,
        "doi": paper.doi,
        "year": paper.year,
        "journal": paper.journal,
        "library_name": paper.library_name,
        "has_pdf": bool(paper.pdf_path),
        **_paper_counts(session, paper.id),
    }


def list_papers(
    session: Session,
    *,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    statement = select(Paper).order_by(Paper.serial_number.asc().nullslast(), Paper.created_at.desc())
    if query:
        needle = f"%{query.strip()}%"
        statement = statement.where(
            or_(
                Paper.title.ilike(needle),
                Paper.doi.ilike(needle),
                Paper.journal.ilike(needle),
                Paper.paper_code.ilike(needle),
                Paper.abstract.ilike(needle),
            )
        )
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    papers = session.scalars(statement.limit(limit).offset(offset)).all()
    return {"total": total, "items": [serialize_paper(session, paper) for paper in papers]}


def get_paper(session: Session, paper_id: UUID) -> dict[str, Any]:
    return serialize_paper(session, _required_paper(session, paper_id))


def _relative_storage_path(path: Path, settings: Settings) -> str:
    root = settings.storage_root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise RebuildWorkflowError("stored_file_outside_storage_root")
    return resolved.relative_to(root).as_posix()


def resolve_file_path(file_record: RebuildPaperFile, settings: Settings) -> Path:
    root = settings.storage_root.resolve()
    candidate = (root / file_record.storage_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise RebuildWorkflowError("stored_file_not_found")
    return candidate


def resolve_paper_source_pdf(paper: Paper, settings: Settings) -> Path:
    from app.utils.artifact_paths import resolve_paper_pdf_path

    path = resolve_paper_pdf_path(paper.pdf_path, settings.storage_root)
    if path is None:
        raise RebuildWorkflowError("paper_pdf_not_found")
    return path


def associate_pdf_file(
    session: Session,
    *,
    paper_id: UUID,
    role: str,
    saved_path: Path,
    original_filename: str,
    sha256: str,
    file_size: int,
    settings: Settings,
) -> tuple[RebuildPaperFile, bool]:
    if role not in {"main", "si"}:
        raise RebuildWorkflowError("invalid_file_role")
    _required_paper(session, paper_id)
    existing = session.scalar(
        select(RebuildPaperFile).where(
            RebuildPaperFile.paper_id == paper_id,
            RebuildPaperFile.role == role,
            RebuildPaperFile.sha256 == sha256,
        )
    )
    if existing is not None:
        return existing, False
    record = RebuildPaperFile(
        paper_id=paper_id,
        role=role,
        storage_path=_relative_storage_path(saved_path, settings),
        original_filename=original_filename,
        sha256=sha256,
        file_size=file_size,
    )
    session.add(record)
    session.flush()
    return record, True


def register_paper_source_files(session: Session, main: Paper, si: Paper, settings: Settings) -> None:
    """Register only the explicitly associated paper pair; never scan other papers."""
    for paper, role in ((main, "main"), (si, "si")):
        if not paper.pdf_path:
            continue
        try:
            path = resolve_paper_source_pdf(paper, settings)
        except RebuildWorkflowError as exc:
            raise ValueError(f"{role}_source_pdf_not_found") from exc
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        associate_pdf_file(session, paper_id=main.id, role=role, saved_path=path,
            original_filename=path.name, sha256=digest.hexdigest(),
            file_size=path.stat().st_size, settings=settings)


def serialize_file(record: RebuildPaperFile) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "paper_id": str(record.paper_id),
        "role": record.role,
        "original_filename": record.original_filename,
        "sha256": record.sha256,
        "file_size": record.file_size,
        "upload_status": record.upload_status,
        "storage_path": record.storage_path,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def list_files(session: Session, paper_id: UUID) -> list[dict[str, Any]]:
    _required_paper(session, paper_id)
    records = session.scalars(
        select(RebuildPaperFile).where(RebuildPaperFile.paper_id == paper_id).order_by(RebuildPaperFile.created_at)
    ).all()
    return [serialize_file(record) for record in records]


def get_file(session: Session, file_id: UUID) -> RebuildPaperFile:
    record = session.get(RebuildPaperFile, file_id)
    if record is None:
        raise RebuildWorkflowError("file_not_found")
    return record


def _normalize_image_path(value: str | None, settings: Settings) -> str | None:
    if value is None or not value.strip():
        return None
    raw = Path(value.strip())
    if any(part in {"", ".", ".."} for part in raw.parts):
        raise RebuildWorkflowError("invalid_image_path")
    root = settings.storage_root.resolve()
    if raw.is_absolute():
        resolved = raw.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise RebuildWorkflowError("image_outside_storage_root")
        return resolved.relative_to(root).as_posix()
    return raw.as_posix()


def resolve_asset_image_path(asset: RebuildVisualAsset, settings: Settings) -> Path | None:
    if not asset.image_path:
        return None
    root = settings.storage_root.resolve()
    candidate = (root / asset.image_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise RebuildWorkflowError("asset_image_not_found")
    return candidate


def _reading_text(reading: dict, *keys: str) -> str:
    return next((reading[key].strip() for key in keys if isinstance(reading.get(key), str) and reading[key].strip()), "")


def _placeholder_reading(reading: dict) -> bool:
    return _reading_text(reading, "detailed_explanation_zh", "detailed_explanation", "core_interpretation") == "已由 AI 从原 PDF 逐个子图裁切并汇总；可在图表资料页逐个子图核对。"


def _structured_reading(reading: Any) -> bool:
    return isinstance(reading, dict) and not _placeholder_reading(reading) and bool(_reading_text(reading, "summary_zh", "summary")) and bool(_reading_text(reading, "detailed_explanation_zh", "detailed_explanation", "core_interpretation"))


def reading_coverage(assets: list[dict]) -> dict:
    def provenance(asset): return asset.get("provenance") if isinstance(asset.get("provenance"), dict) else {}
    current = [asset for asset in assets if not (provenance(asset).get("lifecycle") == "superseded" or provenance(asset).get("superseded_by_asset_id"))]
    rich, missing = [], []
    for asset in current:
        reading = asset.get("reading_explanation") or provenance(asset).get("reading_explanation") or provenance(asset).get("scientific_reading")
        (rich if _structured_reading(reading) and not reading.get("is_stale") and all(key not in reading or str(reading[key]) == str(asset.get(target)) for key, target in (("paper_id", "paper_id"), ("file_id", "file_id"), ("asset_id", "id"))) else missing).append(asset["id"])
    return {"current_assets": len(current), "structured_readings": len(rich), "missing_or_partial_asset_ids": missing,
            "status": "pending_independent_review" if current and not missing else "incomplete", "scientific_acceptance": "pending"}


def _validate_asset_reading(session, paper_id, existing, file_id, reading):
    if not _structured_reading(reading):
        raise RebuildWorkflowError("reading_explanation_requires_summary_and_real_detail")
    if file_id is None:
        raise RebuildWorkflowError("reading_explanation_requires_registered_source_file")
    file = get_file(session, file_id)
    if file.paper_id != paper_id:
        raise RebuildWorkflowError("reading_file_belongs_to_other_paper")
    # Existing rich records need no migration/version gate. Validate bindings if a new caller supplies them.
    for key, expected in (("paper_id", str(paper_id)), ("file_id", str(file_id)), ("file_sha256", file.sha256)):
        if key in reading and str(reading[key]) != expected:
            raise RebuildWorkflowError("reading_" + key + "_mismatch")
    if "asset_id" in reading and (existing is None or str(reading["asset_id"]) != str(existing.id)):
        raise RebuildWorkflowError("reading_asset_id_mismatch")
    for key in ("subfigures", "evidence_locators", "uncertainties_zh", "uncertainties", "methods"):
        if key in reading and not isinstance(reading[key], list):
            raise RebuildWorkflowError("reading_" + key + "_must_be_array")
    for sub in reading.get("subfigures", []):
        if not isinstance(sub, dict) or not isinstance(sub.get("label"), str) or not sub["label"].strip() or not isinstance(sub.get("description"), str) or not sub["description"].strip():
            raise RebuildWorkflowError("reading_subfigure_incomplete")
        if any(key in sub and not isinstance(sub[key], (str, list)) for key in ("methods", "findings")):
            raise RebuildWorkflowError("reading_subfigure_fields_invalid")
    locators = reading.get("evidence_locators", [])
    if not locators:
        raise RebuildWorkflowError("reading_evidence_required")
    normalised_locators = []
    for loc in locators:
        if not isinstance(loc, dict) or type(loc.get("page")) is not int or loc["page"] < 1:
            raise RebuildWorkflowError("reading_evidence_page_required")
        if "paper_id" in loc and str(loc["paper_id"]) != str(paper_id):
            raise RebuildWorkflowError("reading_evidence_wrong_paper")
        try:
            source_file = get_file(session, UUID(str(loc["file_id"]))) if loc.get("file_id") else file
        except (ValueError, TypeError, AttributeError) as exc:
            raise RebuildWorkflowError("reading_evidence_file_id_invalid") from exc
        if source_file.paper_id != paper_id:
            raise RebuildWorkflowError("reading_evidence_wrong_file")
        if not (isinstance(loc.get("quote"), str) and loc["quote"].strip() or loc.get("evidence_type") == "visual" and isinstance(loc.get("visual_observation"), str) and loc["visual_observation"].strip()):
            raise RebuildWorkflowError("reading_evidence_text_required")
        normalised_locators.append({**loc, "paper_id": str(paper_id), "file_id": str(source_file.id)})
    return {**reading, "paper_id": str(paper_id), "file_id": str(file_id), "file_sha256": file.sha256,
            "evidence_locators": normalised_locators, "is_stale": False}


def prepare_visual_asset(
    session: Session,
    *,
    paper_id: UUID,
    payload: RebuildVisualAssetRequest,
    settings: Settings,
    force_source_changed: bool = False,
) -> tuple[RebuildVisualAsset | None, dict]:
    _required_paper(session, paper_id)
    if payload.file_id is not None:
        file_record = get_file(session, payload.file_id)
        if file_record.paper_id != paper_id:
            raise RebuildWorkflowError("file_belongs_to_other_paper")
    existing = session.scalar(
        select(RebuildVisualAsset).where(
            RebuildVisualAsset.paper_id == paper_id,
            RebuildVisualAsset.asset_key == payload.asset_key,
        )
    )
    data = payload.model_dump(exclude_unset=True)
    explicit_present = "reading_explanation" in data
    explicit_reading = data.pop("reading_explanation", None)
    old_provenance = existing.provenance if existing is not None and isinstance(existing.provenance, dict) else {}
    supplied = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
    provenance = {**old_provenance, **supplied} if data.get("provenance") is not None else dict(old_provenance)
    declared = [supplied[key] for key in ("reading_explanation", "scientific_reading") if key in supplied]
    if explicit_present:
        declared.insert(0, explicit_reading)
    if declared and (any(not isinstance(value, dict) or not value for value in declared) or any(value != declared[0] for value in declared[1:])):
        raise RebuildWorkflowError("reading_explanation_empty_or_conflicting_declarations")
    incoming = declared[0] if declared else None
    old_reading = old_provenance.get("reading_explanation") or old_provenance.get("scientific_reading")
    source_changed = existing is not None and (force_source_changed or any(key in data and data[key] != getattr(existing, key) for key in ("file_id", "image_path", "page_numbers", "bbox")))
    changed_reading = incoming is not None and (incoming != old_reading or source_changed)
    if explicit_present and incoming is not None:
        changed_reading = True
    if changed_reading:
        incoming = _validate_asset_reading(session, paper_id, existing, data.get("file_id", getattr(existing, "file_id", None)), incoming)
        provenance["reading_explanation"] = incoming
    elif source_changed:
        for key in ("reading_explanation", "scientific_reading"):
            if isinstance(provenance.get(key), dict):
                provenance[key] = {**provenance[key], "is_stale": True, "stale_reason": "asset_source_changed"}
    if "provenance" in data or explicit_present or source_changed and old_reading:
        data["provenance"] = provenance
    if "image_path" in data:
        data["image_path"] = _normalize_image_path(data.get("image_path"), settings)
    return existing, data


def upsert_visual_asset(session: Session, *, paper_id: UUID, payload: RebuildVisualAssetRequest, settings: Settings, prepared: tuple | None = None) -> tuple[RebuildVisualAsset, bool]:
    existing, data = prepared if prepared is not None else prepare_visual_asset(session, paper_id=paper_id, payload=payload, settings=settings)
    if existing is None:
        record = RebuildVisualAsset(paper_id=paper_id, **data)
        session.add(record)
        session.flush()
        return record, True
    for key, value in data.items():
        setattr(existing, key, value)
    existing.version += 1
    session.flush()
    return existing, False


def serialize_asset(asset: RebuildVisualAsset) -> dict[str, Any]:
    return {
        "id": str(asset.id),
        "paper_id": str(asset.paper_id),
        "file_id": str(asset.file_id) if asset.file_id else None,
        "asset_key": asset.asset_key,
        "asset_type": asset.asset_type,
        "logical_group_key": asset.logical_group_key,
        "figure_label": asset.figure_label,
        "subfigure_label": asset.subfigure_label,
        "caption": asset.caption,
        "page_numbers": asset.page_numbers or [],
        "bbox": asset.bbox,
        "image_path": asset.image_path,
        "image_url": f"/api/rebuild/assets/{asset.id}" if asset.image_path else None,
        "x_axis_unit": asset.x_axis_unit,
        "y_axis_unit": asset.y_axis_unit,
        "material_mapping": asset.material_mapping,
        "context_text": asset.context_text,
        "explanation": asset.explanation,
        "reading_explanation": (asset.provenance if isinstance(asset.provenance, dict) else {}).get("reading_explanation") or (asset.provenance if isinstance(asset.provenance, dict) else {}).get("scientific_reading"),
        "unreadable_fields": asset.unreadable_fields or [],
        "provenance": asset.provenance,
        "status": asset.status,
        "version": asset.version,
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
        "updated_at": asset.updated_at.isoformat() if asset.updated_at else None,
    }


def list_assets(session: Session, paper_id: UUID) -> list[dict[str, Any]]:
    _required_paper(session, paper_id)
    records = session.scalars(
        select(RebuildVisualAsset)
        .where(RebuildVisualAsset.paper_id == paper_id)
        .order_by(RebuildVisualAsset.figure_label, RebuildVisualAsset.subfigure_label)
    ).all()
    return [serialize_asset(asset) for asset in records]


def get_asset(session: Session, asset_id: UUID) -> RebuildVisualAsset:
    asset = session.get(RebuildVisualAsset, asset_id)
    if asset is None:
        raise RebuildWorkflowError("asset_not_found")
    return asset


def _row_identity(data: dict[str, Any]) -> dict[str, Any]:
    return {field: data.get(field) for field in IDENTITY_FIELDS}


def _row_condition(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("condition") or {}


def _source_key(data: dict[str, Any]) -> str:
    identity = {
        "file_id": str(data.get("file_id") or ""),
        "asset_id": str(data.get("asset_id") or ""),
        "source_kind": data.get("source_kind"),
        "page_number": data.get("page_number"),
        "label": data.get("label"),
        "table_row": data.get("table_row"),
        "table_column": data.get("table_column"),
        "quote": data.get("quote"),
        "estimate_basis": data.get("estimate_basis"),
    }
    return _stable_hash("source", identity)


def _source_is_sufficient(data: dict[str, Any]) -> bool:
    # Empty strings and an isolated table coordinate do not locate evidence.
    located = bool(data.get("page_number") or data.get("asset_id") or
                   str(data.get("label") or "").strip() or str(data.get("quote") or "").strip())
    return located


def _validate_value(payload: dict[str, Any]) -> None:
    kind = payload.get("value_type", "explicit")
    raw, number = payload.get("raw_value"), payload.get("numeric_value")
    if kind == "missing":
        if raw is not None or number is not None or not str(payload.get("missing_reason") or "").strip():
            raise RebuildWorkflowError("missing_value_must_be_empty_and_explained")
        return
    if raw is None or not str(raw).strip():
        raise RebuildWorkflowError("raw_value_required")
    sources = payload.get("sources") or []
    if not sources:
        raise RebuildWorkflowError("source_required_for_value")
    if number is not None and not math.isfinite(number):
        raise RebuildWorkflowError("numeric_value_must_be_finite")
    # The analysis column is binary64: compare its exact projection, not a
    # Decimal(str(float)) round trip that rejects legitimate high-precision text.
    plain_number = re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", str(raw).strip())
    if number is not None:
        if not plain_number:
            raise RebuildWorkflowError("numeric_value_requires_plain_numeric_raw:omit_numeric_for_text_range_or_inequality")
        try:
            parsed = Decimal(str(raw).strip())
            projected = float(parsed)
        except (InvalidOperation, OverflowError):
            raise RebuildWorkflowError("numeric_value_out_of_range_for_raw")
        if projected != number or (projected == 0 and parsed != 0):
            raise RebuildWorkflowError("numeric_value_disagrees_with_raw")
    if kind == "estimated":
        try:
            decimal = Decimal(raw)
            if not decimal.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            raise RebuildWorkflowError("estimated_raw_value_must_be_numeric")
        if decimal.as_tuple().exponent < -2 or (payload.get("precision_digits") or 0) > 2:
            raise RebuildWorkflowError("estimated_precision_exceeds_two_decimals")
        if number is not None and Decimal(str(number)) != decimal:
            raise RebuildWorkflowError("estimated_numeric_value_disagrees_with_raw")
    if kind in {"estimated", "derived"}:
        if not all(str(source.get("estimate_basis") or "").strip() for source in sources):
            raise RebuildWorkflowError("estimate_or_derivation_basis_required_for_each_source")
    if kind == "explicit" and any(source.get("source_kind") in {"estimated", "derived"} for source in sources):
        raise RebuildWorkflowError("explicit_value_requires_explicit_evidence")


def _validate_value_for_paper(session: Session, paper_id: UUID, payload: dict[str, Any]) -> None:
    """Shared preflight for cell writes and promotion of cell declarations to identity."""
    _validate_value(payload)
    # PostgreSQL text cannot contain NUL; inspect strings rather than JSON escapes.
    queue = [payload]
    while queue:
        item = queue.pop()
        if isinstance(item, str) and chr(0) in item:
            raise RebuildWorkflowError("database_constraint_rejected")
        if isinstance(item, dict):
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item)
    for source in payload.get("sources", []):
        if not _source_is_sufficient(source):
            raise RebuildWorkflowError("source_requires_page_label_quote_or_asset")
        if source.get("file_id") is not None and get_file(session, source["file_id"]).paper_id != paper_id:
            raise RebuildWorkflowError("source_file_belongs_to_other_paper")
        if source.get("asset_id") is not None and get_asset(session, source["asset_id"]).paper_id != paper_id:
            raise RebuildWorkflowError("source_asset_belongs_to_other_paper")


def _candidate(payload: dict[str, Any], source_ids: list[str]) -> dict[str, Any]:
    fields = ("raw_value", "numeric_value", "unit", "value_type", "precision_digits", "missing_reason")
    return {**{key: payload.get(key) for key in fields}, "source_ids": sorted(set(source_ids))}


def _retain_candidate(candidates: list[dict[str, Any]], incoming: dict[str, Any]) -> None:
    identity = {key: value for key, value in incoming.items() if key != "source_ids"}
    for old in candidates:
        if {key: value for key, value in old.items() if key != "source_ids"} == identity:
            old["source_ids"] = sorted(set(old.get("source_ids", [])) | set(incoming["source_ids"]))
            return
    candidates.append(incoming)


def _upsert_source(
    session: Session,
    *,
    value: RebuildDataValue,
    row: RebuildDataRow,
    data: dict[str, Any],
) -> tuple[RebuildValueSource, bool]:
    if not _source_is_sufficient(data):
        raise RebuildWorkflowError("source_requires_page_label_quote_or_asset")
    source_key = _source_key(data)
    existing = session.scalar(
        select(RebuildValueSource).where(
            RebuildValueSource.value_id == value.id,
            RebuildValueSource.source_key == source_key,
        )
    )
    if existing is not None:
        return existing, False
    for legacy in session.scalars(select(RebuildValueSource).where(RebuildValueSource.value_id == value.id)):
        if _source_key(serialize_source(legacy)) == source_key:
            return legacy, False
    source = RebuildValueSource(
        value_id=value.id,
        row_id=row.id,
        paper_id=row.paper_id,
        source_key=source_key,
        **{key: value for key, value in data.items() if key not in {"source_key"}},
    )
    session.add(source)
    session.flush()
    return source, True


def _value_rank(value_type: str) -> int:
    return {"explicit": 3, "derived": 2, "estimated": 1, "missing": 0}.get(value_type, 0)


def _value_signature(data: dict[str, Any]) -> str:
    raw = data.get("raw_value")
    # Compare exact decimals, never round or convert units. Raw text is still retained.
    try:
        number = Decimal(raw) if raw is not None else None
        if number is not None and number.is_finite():
            digits = list(number.as_tuple().digits)
            exponent = number.as_tuple().exponent
            while digits and digits[-1] == 0:
                digits.pop()
                exponent += 1
            raw = [number.as_tuple().sign if digits else 0, digits, exponent if digits else 0]
    except InvalidOperation:
        pass
    return _canonical_json([raw, data.get("numeric_value"), data.get("unit") or ""])


def _same_value(existing: RebuildDataValue, incoming: dict[str, Any]) -> bool:
    return _value_signature({key: getattr(existing, key) for key in
                             ("raw_value", "numeric_value", "unit")}) == _value_signature(incoming)


def _import_value(
    session: Session,
    *,
    row: RebuildDataRow,
    payload: dict[str, Any],
) -> dict[str, Any]:
    field_name = payload["field_name"]
    value_type = payload.get("value_type") or "explicit"
    raw_value = payload.get("raw_value")
    numeric_value = payload.get("numeric_value")
    unit = payload.get("unit")
    missing_reason = payload.get("missing_reason")
    sources = payload.get("sources") or []
    _validate_value_for_paper(session, row.paper_id, payload)

    existing = session.scalar(
        select(RebuildDataValue).where(
            RebuildDataValue.row_id == row.id,
            RebuildDataValue.field_name == field_name,
        )
    )
    if existing is None:
        value = RebuildDataValue(
            row_id=row.id,
            paper_id=row.paper_id,
            field_name=field_name,
            raw_value=raw_value,
            numeric_value=numeric_value,
            unit=unit,
            value_type=value_type,
            precision_digits=payload.get("precision_digits"),
            is_estimated=value_type == "estimated",
            missing_reason=missing_reason,
        )
        session.add(value)
        session.flush()
    else:
        value = existing

    # Capture legacy state before attaching new sources so provenance never crosses candidates.
    old_sources = session.scalars(select(RebuildValueSource).where(RebuildValueSource.value_id == value.id)).all()
    conflict = dict(value.conflict or {})
    candidates = [dict(item) for item in conflict.get("candidates", [])]
    if existing is not None and not candidates:
        # Old conflict records mixed all sources together. Preserve that uncertainty.
        legacy_ids = [str(source.id) for source in old_sources]
        if conflict:
            conflict["unattributed_legacy_source_ids"] = legacy_ids
        _retain_candidate(candidates, _candidate(
            {key: getattr(value, key) for key in ("raw_value", "numeric_value", "unit", "value_type", "precision_digits", "missing_reason")},
            [] if conflict else legacy_ids,
        ))
    # Legacy candidates must participate in both display and conflict state.
    # A legacy unresolved flag is sticky on ordinary import, even when some
    # candidate details were already lost by an earlier implementation.
    if conflict.get("status") in {"value_conflict", "unit_conflict"} and (
        "existing" in conflict or "incoming" in conflict or not conflict.get("candidates")
    ):
        conflict["legacy_unresolved"] = conflict["status"]
    if conflict.get("status") != "corrected_previous_retained":
        for name in ("existing", "incoming"):
            legacy = conflict.get(name)
            if isinstance(legacy, dict) and legacy.get("raw_value") is not None:
                _retain_candidate(candidates, _candidate(legacy, []))
    created_sources = 0
    incoming_ids = []
    for source_data in sources:
        if source_data.get("file_id") is not None:
            if get_file(session, source_data["file_id"]).paper_id != row.paper_id:
                raise RebuildWorkflowError("source_file_belongs_to_other_paper")
        if source_data.get("asset_id") is not None:
            if get_asset(session, source_data["asset_id"]).paper_id != row.paper_id:
                raise RebuildWorkflowError("source_asset_belongs_to_other_paper")
        source, created = _upsert_source(session, value=value, row=row, data=source_data)
        incoming_ids.append(str(source.id))
        created_sources += int(created)
    before = _canonical_json(candidates)
    _retain_candidate(candidates, _candidate(payload, incoming_ids))
    candidate_changed = before != _canonical_json(candidates)
    same = _same_value(value, payload)
    promote = existing is not None and (
        value.value_type == "missing" and value_type != "missing" or
        (value.unit or "") == (unit or "") and value_type == "explicit" and value.value_type == "estimated"
    )
    if existing is None:
        status = "created"
    elif promote:
        status = "filled" if value.value_type == "missing" else "explicit_value_promoted"
    elif same:
        status = "evidence_added" if candidate_changed or created_sources else "deduplicated"
    elif value_type == "missing":
        status = "existing_value_retained"
    elif (value.unit or "") != (unit or ""):
        status = "unit_conflict_preserved"
    elif value.value_type == "explicit" and value_type == "estimated":
        status = "lower_priority_source_retained"
    else:
        status = "value_conflict_preserved"
    if promote:
        for key in ("raw_value", "numeric_value", "unit", "value_type", "precision_digits", "missing_reason"):
            setattr(value, key, payload.get(key))
        value.is_estimated = value_type == "estimated"
    if (existing is None or promote) and incoming_ids:
        value.preferred_source_id = UUID(incoming_ids[0])
    # Recompute conflict from ALL candidates, so an innocuous retry cannot clear a conflict.
    nonempty = [c for c in candidates if c["value_type"] != "missing"]
    units = {c.get("unit") or "" for c in nonempty}
    active = [c for c in nonempty if not (value.value_type == "explicit" and c["value_type"] == "estimated")]
    fingerprints = {_value_signature(c) for c in active}
    conflict_status = "unit_conflict" if len(units) > 1 else "value_conflict" if len(fingerprints) > 1 else "evidence_history"
    if conflict.get("legacy_unresolved") in {"value_conflict", "unit_conflict"}:
        conflict_status = ("unit_conflict" if "unit_conflict" in
                           {conflict_status, conflict["legacy_unresolved"]} else "value_conflict")
    value.conflict = {**conflict, "status": conflict_status, "candidates": candidates}
    session.flush()
    return {"field": field_name, "status": status, "sources_added": created_sources,
            "outcome": "conflict" if conflict_status in {"unit_conflict", "value_conflict"} else
                       "written" if existing is None or promote or candidate_changed or created_sources else "duplicate",
            "value_id": str(value.id), "persisted": True}


DIMENSION_FIELDS = ("adsorbate", "intermediate", "reaction_step", "adsorption_site", "identity_context")
IMPORT_META = "_rebuild_import"


def row_identity_state(row: RebuildDataRow) -> dict[str, Any]:
    """Raw persisted identity for review, including provisional identity markers."""
    return {
        "identity": {field: getattr(row, field) for field in IDENTITY_FIELDS},
        "condition": row.condition or {},
        "pending_identity_fields": (row.properties or {}).get(IMPORT_META, {}).get("pending_identity_fields", []),
    }


def row_identity_revision(session: Session, row: RebuildDataRow) -> str:
    # Include redundant identity declarations and the correction generation so
    # an ABA correction or a concurrent identity-cell import cannot pass CAS.
    values = session.scalars(select(RebuildDataValue).where(
        RebuildDataValue.row_id == row.id, RebuildDataValue.field_name.in_(DIMENSION_FIELDS)
    ).order_by(RebuildDataValue.field_name)).all()
    meta = (row.properties or {}).get(IMPORT_META, {})
    state = {
        "row_id": str(row.id), "paper_id": str(row.paper_id), "row_key": row.row_key,
        **row_identity_state(row),
        "dimension_properties": {key: (row.properties or {}).get(key) for key in DIMENSION_FIELDS},
        "dimension_values": [{"field_name": value.field_name, "raw_value": value.raw_value,
                              "value_type": value.value_type} for value in values],
        "correction_requests": [item["request_id"] for item in meta.get("identity_corrections", [])],
    }
    return hashlib.sha256(_canonical_json(state).encode("utf-8")).hexdigest()


def _ambiguous_identity_collision(session: Session, other: RebuildDataRow, target: dict[str, Any]) -> bool:
    """An inconsistent legacy dimension cannot prove a target is distinct.

    Compare only rows with the same core identity. A known difference in a
    shared scientific condition or in all known dimension candidates proves
    separation; missing declarations remain unknown rather than equal.
    """
    if _row_identity(row_identity_state(other)["identity"]) != target["identity"]:
        return False
    condition = other.condition or {}
    desired = target["condition"]
    for key in (set(condition) & set(desired)) - set(DIMENSION_FIELDS):
        if condition[key] != desired[key]:
            return False
    values = session.scalars(select(RebuildDataValue).where(
        RebuildDataValue.row_id == other.id, RebuildDataValue.field_name.in_(DIMENSION_FIELDS)
    )).all()
    for field in DIMENSION_FIELDS:
        declarations = [condition.get(field), (other.properties or {}).get(field)]
        declarations.extend(value.raw_value for value in values
                            if value.field_name == field and value.value_type != "missing")
        known = [value for value in declarations if isinstance(value, str) and value.strip()]
        invalid = any(value is not None and (not isinstance(value, str) or not value.strip())
                      for value in declarations)
        if not invalid and known and desired.get(field) is not None and desired[field] not in known:
            return False
    return True


def correct_row_identity(
    session: Session, *, row_id: UUID, payload: RebuildRowIdentityCorrectionRequest, actor: str
) -> dict[str, Any]:
    """Audit a confirmed identity correction; never replace or merge a sample."""
    # Same Paper lock as import_data_row serializes both code paths and checks
    # target collisions atomically, despite identity having no unique DB index.
    if session.scalar(select(Paper.id).where(Paper.id == payload.paper_id).with_for_update()) is None:
        raise RebuildWorkflowError("paper_not_found")
    row = session.scalar(select(RebuildDataRow).where(
        RebuildDataRow.id == row_id, RebuildDataRow.paper_id == payload.paper_id
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise RebuildWorkflowError("row_not_found")
    if row.row_key != payload.row_key:
        raise RebuildWorkflowError("canonical_row_key_mismatch")
    properties = dict(row.properties or {})
    meta = dict(properties.get(IMPORT_META) or {})
    history = list(meta.get("identity_corrections", []))
    request_id = str(payload.request_id)
    fingerprint = hashlib.sha256(_canonical_json(payload.model_dump(mode="json", exclude_unset=True)).encode("utf-8")).hexdigest()
    current_revision = row_identity_revision(session, row)
    for entry in history:
        if entry["request_id"] == request_id:
            if entry["request_fingerprint"] != fingerprint:
                raise RebuildWorkflowError("identity_request_id_reused")
            return {"status": "replayed", "request_id": request_id,
                    "applied_revision": entry["applied_revision"],
                    "current_revision": current_revision, "row": serialize_row(session, row)}
    if current_revision != payload.expected_revision:
        raise RebuildWorkflowError("identity_revision_conflict")
    if meta.get("pending_identity_fields"):
        raise RebuildWorkflowError("identity_pending:recover_via_rows_import")
    before = row_identity_state(row)
    changes = payload.changes.model_dump(exclude_unset=True)
    merged = {**before["identity"], "condition": before["condition"], **changes}
    # Reuse the established enum/type validation and identity-dimension checks.
    validated = RebuildDataRowInput.model_validate(merged).model_dump()
    if not validated["material"].strip():
        raise RebuildWorkflowError("material_must_not_be_blank")
    dimension_values = session.scalars(select(RebuildDataValue).where(
        RebuildDataValue.row_id == row.id, RebuildDataValue.field_name.in_(DIMENSION_FIELDS)
    )).all()
    condition = _identity_condition({**validated, "properties": properties,
        "values": [{"field_name": value.field_name, "raw_value": value.raw_value,
                    "value_type": value.value_type} for value in dimension_values]})
    target = {"identity": _row_identity(validated), "condition": condition}
    if target == {"identity": before["identity"], "condition": before["condition"]}:
        raise RebuildWorkflowError("identity_no_change")
    others = session.scalars(select(RebuildDataRow).where(
        RebuildDataRow.paper_id == row.paper_id, RebuildDataRow.id != row.id
    ).execution_options(populate_existing=True)).all()
    for other in others:
        try:
            other_identity = _stored_identity(session, other)
        except RebuildWorkflowError:
            if _ambiguous_identity_collision(session, other, target):
                raise RebuildWorkflowError("identity_target_ambiguous_collision:" + str(other.id))
            continue
        if other_identity == target:
            raise RebuildWorkflowError("identity_target_collision:" + str(other.id))
    evidence = []
    for source in payload.evidence:
        if source.file_id and get_file(session, source.file_id).paper_id != row.paper_id:
            raise RebuildWorkflowError("source_file_belongs_to_other_paper")
        if source.asset_id and get_asset(session, source.asset_id).paper_id != row.paper_id:
            raise RebuildWorkflowError("source_asset_belongs_to_other_paper")
        evidence.append(source.model_dump(mode="json"))
    for field in IDENTITY_FIELDS:
        setattr(row, field, target["identity"][field])
    row.condition = condition
    entry = {"request_id": request_id, "request_fingerprint": fingerprint,
             "expected_revision": payload.expected_revision, "before": before,
             "after": row_identity_state(row), "reason": payload.correction_reason,
             "evidence": evidence, "actor": actor,
             "corrected_at": datetime.now(timezone.utc).isoformat()}
    history.append(entry)
    meta["identity_corrections"] = history
    properties[IMPORT_META] = meta
    row.properties = properties
    applied_revision = row_identity_revision(session, row)
    # Finish the receipt before the first flush; SQLAlchemy does not track
    # nested mutation after a JSON value has been flushed. No self-hash.
    entry["applied_revision"] = applied_revision
    row.properties = {**properties, IMPORT_META: {**meta, "identity_corrections": [*history]}}
    session.flush()
    return {"status": "corrected", "request_id": request_id,
            "applied_revision": applied_revision, "current_revision": applied_revision,
            "row": serialize_row(session, row)}


def _identity_condition(data: dict[str, Any]) -> dict[str, Any]:
    condition = dict(data.get("condition") or {})
    properties = data.get("properties") or {}
    for field in DIMENSION_FIELDS:
        declarations = [data.get(field), condition.get(field), properties.get(field)]
        for value in data.get("values", []):
            if isinstance(value, dict) and value.get("field_name") == field:
                if value.get("value_type", "explicit") != "missing":
                    declarations.append(value.get("raw_value"))
        known = [item for item in declarations if item is not None]
        if any(not isinstance(item, str) or not item.strip() for item in known):
            raise RebuildWorkflowError(f"invalid_identity_dimension:{field}")
        if len(set(known)) > 1:
            raise RebuildWorkflowError(f"contradictory_identity_dimension:{field}")
        if known:
            condition[field] = known[0]
    return condition


def _stored_identity(session: Session, row: RebuildDataRow) -> dict[str, Any]:
    values = session.scalars(select(RebuildDataValue).where(RebuildDataValue.row_id == row.id)).all()
    data = {field: getattr(row, field) for field in IDENTITY_FIELDS}
    data.update(condition=row.condition, properties=row.properties,
                values=[{"field_name": v.field_name, "raw_value": v.raw_value, "value_type": v.value_type} for v in values])
    return {"identity": _row_identity(data), "condition": _identity_condition(data)}


def _same_submitted_facts(session: Session, row: RebuildDataRow, data: dict[str, Any]) -> bool:
    """Legacy fallback: only identical facts AND evidence prove an unscoped retry."""
    stored = session.scalars(select(RebuildDataValue).where(RebuildDataValue.row_id == row.id)).all()
    if not stored or len(stored) != len(data["values"]):
        return False
    by_field = {value.field_name: value for value in stored}
    for raw in data["values"]:
        try:
            incoming = RebuildDataValueInput.model_validate(raw).model_dump()
        except ValidationError:
            return False
        value = by_field.get(incoming["field_name"])
        if value is None or not _same_value(value, incoming) or value.value_type != incoming["value_type"]:
            return False
        sources = session.scalars(select(RebuildValueSource).where(RebuildValueSource.value_id == value.id)).all()
        if {_source_key(serialize_source(source)) for source in sources} != {_source_key(source) for source in incoming["sources"]}:
            return False
    return True


def import_data_row(session: Session, paper_id: UUID, payload: RebuildDataRowInput) -> dict[str, Any]:
    # Serialize imports per paper, including different caller keys for the same fact.
    if session.scalar(select(Paper.id).where(Paper.id == paper_id).with_for_update()) is None:
        raise RebuildWorkflowError("paper_not_found")
    data = payload.model_dump()
    if not data["material"].strip():
        raise RebuildWorkflowError("material_must_not_be_blank")
    if IMPORT_META in data["properties"]:
        raise RebuildWorkflowError("reserved_property:_rebuild_import")
    dimension_errors = {}
    identity_values = []
    valid_dimensions = set()
    for index, raw in enumerate(data["values"]):
        field = raw.get("field_name") if isinstance(raw, dict) else None
        if field in DIMENSION_FIELDS:
            try:
                validated = RebuildDataValueInput.model_validate(raw).model_dump()
                _validate_value_for_paper(session, paper_id, validated)
                if validated["value_type"] != "missing":
                    valid_dimensions.add(field)
                identity_values.append(validated)
            except (ValidationError, RebuildWorkflowError) as exc:
                dimension_errors[index] = {"field": field, "error": str(exc)}
    condition = _identity_condition({**data, "values": identity_values})
    identity = {"identity": _row_identity(data), "condition": condition}
    fingerprint = _stable_hash("row", {"paper_id": str(paper_id), **identity})
    requested_key = data.get("row_key")
    # Absent dimensions are unknown, not a claim that two samples are identical.
    # A stable key/context asserts the fact; identical submissions are always retries.
    submission = _stable_hash("submission", {key: value for key, value in data.items() if key != "row_key"})
    rows = session.scalars(select(RebuildDataRow).where(RebuildDataRow.paper_id == paper_id)).all()
    canonical = [r for r in rows if requested_key and r.row_key == requested_key]
    aliases = [r for r in rows if requested_key and requested_key in
               (r.properties or {}).get(IMPORT_META, {}).get("aliases", [])]
    targeted = canonical or aliases
    if len(targeted) > 1:
        raise RebuildWorkflowError("ambiguous_row_alias:retry_with_canonical_row_key:" +
                                   ",".join(sorted(r.row_key for r in targeted)))
    matching = []
    recovering = False
    for candidate in targeted or rows:
        meta = (candidate.properties or {}).get(IMPORT_META, {})
        pending = set(meta.get("pending_identity_fields", []))
        try:
            stored = _stored_identity(session, candidate)
            same = stored == identity
            # Only an explicit key may resolve a row that THIS importer marked
            # provisional. No normal/history row may have its identity rewritten.
            added = set(condition) - set(stored["condition"])
            recoverable = bool(targeted and pending and not dimension_errors and
                               _row_identity(data) == stored["identity"] and
                               all(condition.get(k) == v for k, v in stored["condition"].items()) and
                               added <= pending and pending <= valid_dimensions)
        except RebuildWorkflowError:
            same, recoverable = False, False
        if targeted and not (same or recoverable):
            raise RebuildWorkflowError("row_key_identity_conflict:use_a_distinct_key_for_a_distinct_fact")
        # Do not contaminate a confirmed row or attach a provisional row to a
        # different sample just because both currently omit the bad dimension.
        pending_compatible = bool(pending) == bool(dimension_errors)
        if targeted and dimension_errors and not pending:
            raise RebuildWorkflowError("invalid_identity_for_confirmed_row:correct_dimension_and_retry_same_key")
        if targeted or (same and pending_compatible and (
            (not pending and condition.get("identity_context")) or
            submission in meta.get("submissions", []) or
            (not pending and _same_submitted_facts(session, candidate, data))
        )):
            matching.append(candidate)
            recovering = recoverable
    if len(matching) > 1:
        raise RebuildWorkflowError("ambiguous_legacy_rows:retry_with_canonical_row_key:" +
                                   ",".join(sorted(r.row_key for r in matching)))
    created = not matching
    if created:
        generated_key = (fingerprint if condition.get("identity_context") and not dimension_errors
                         else _stable_hash("row", [fingerprint, submission]))
        row = RebuildDataRow(paper_id=paper_id, row_key=requested_key or generated_key,
                            **_row_identity(data), condition=condition, properties={}, notes=data.get("notes"))
        session.add(row)
        session.flush()
    else:
        row = matching[0]
    properties = dict(row.properties or {})
    meta = dict(properties.get(IMPORT_META) or {})
    pending = set(meta.get("pending_identity_fields", []))
    if recovering:
        row.condition = condition
        pending.clear()
    pending.update(detail["field"] for detail in dimension_errors.values())
    meta["pending_identity_fields"] = sorted(pending)
    if dimension_errors:
        history = list(meta.get("identity_rejections", []))
        receipt = {"submission": submission, "errors": list(dimension_errors.values())}
        if receipt not in history:
            history.append(receipt)
        meta["identity_rejections"] = history
    aliases = set(meta.get("aliases", []))
    if requested_key:
        aliases.add(requested_key)
    meta["aliases"] = sorted(aliases)
    meta["submissions"] = sorted(set(meta.get("submissions", [])) | {submission})
    conflicts = dict(meta.get("property_conflicts") or {})
    property_results = []
    for key, incoming in data["properties"].items():
        if key not in properties:
            properties[key] = incoming
            property_results.append({"field": key, "outcome": "written"})
        elif properties[key] != incoming:
            candidates = list(conflicts.get(key, [properties[key]]))
            if incoming not in candidates:
                candidates.append(incoming)
            conflicts[key] = candidates
            property_results.append({"field": key, "outcome": "conflict", "candidates": candidates})
        else:
            property_results.append({"field": key, "outcome": "duplicate"})
    meta["property_conflicts"] = conflicts
    notes_changed = False
    if data.get("notes") is not None:
        notes = list(meta.get("notes_history", []))
        if row.notes is not None and row.notes not in notes:
            notes.append(row.notes)
        if data["notes"] not in notes:
            notes.append(data["notes"])
            notes_changed = True
        meta["notes_history"] = notes
        if row.notes is None:
            row.notes = data["notes"]
    properties[IMPORT_META] = meta
    row.properties = properties
    session.flush()
    results = []
    for index, raw in enumerate(data["values"]):
        if index in dimension_errors:
            results.append({**dimension_errors[index], "field_index": index,
                            "status": "rejected", "outcome": "rejected", "persisted": False})
            continue
        try:
            with session.begin_nested():
                validated = RebuildDataValueInput.model_validate(raw).model_dump()
                result = _import_value(session, row=row, payload=validated)
            if pending:
                result = {**result, "status": "identity_pending", "outcome": "pending",
                          "error": "identity_pending:correct_dimension_using_returned_row_key"}
            results.append({**result, "field_index": index})
        except (ValidationError, RebuildWorkflowError, DataError, IntegrityError) as exc:
            results.append({"field_index": index, "field": raw.get("field_name") if isinstance(raw, dict) else None,
                            "status": "rejected", "outcome": "rejected", "persisted": False,
                            "error": "database_constraint_rejected" if isinstance(exc, (DataError, IntegrityError)) else str(exc)})
    outcomes = [v["outcome"] for v in results + property_results]
    incomplete = bool(pending) or any(outcome in {"rejected", "conflict", "pending"} for outcome in outcomes)
    changed = created or notes_changed or "written" in outcomes
    return {"row_key": row.row_key, "row_id": str(row.id),
            "status": "partial" if incomplete else "created" if created else "merged",
            "outcome": "partial" if incomplete else "written" if changed else "duplicate",
            "complete": not incomplete, "values": results, "properties": property_results,
            "identity_status": "pending" if pending else "confirmed",
            "pending_identity_fields": sorted(pending)}


def update_data_value(
    session: Session,
    *,
    value_id: UUID,
    payload: RebuildValueCorrectionRequest,
) -> RebuildDataValue:
    value = session.get(RebuildDataValue, value_id)
    if value is None:
        raise RebuildWorkflowError("value_not_found")
    row = session.get(RebuildDataRow, value.row_id)
    if row is None:
        raise RebuildWorkflowError("row_not_found")

    data = payload.model_dump()
    value_type = data.get("value_type") or "explicit"
    previous = {
        "raw_value": value.raw_value,
        "numeric_value": value.numeric_value,
        "unit": value.unit,
        "value_type": value.value_type,
        "precision_digits": value.precision_digits,
        "missing_reason": value.missing_reason,
    }
    source_payload = data.get("source")
    _validate_value({**data, "sources": [source_payload] if source_payload else []})

    previous_conflict = dict(value.conflict or {})
    history = list(previous_conflict.get("history") or [])
    if previous_conflict:
        history.append(
            {
                "status": previous_conflict.get("status"),
                "previous": previous_conflict.get("previous"),
                "recorded_at": value.updated_at.isoformat() if value.updated_at else None,
            }
        )

    candidates = [dict(c) for c in previous_conflict.get("candidates", [])]
    if not candidates:
        old_sources = session.scalars(select(RebuildValueSource).where(RebuildValueSource.value_id == value.id)).all()
        legacy_ids = [str(source.id) for source in old_sources]
        if previous_conflict:
            previous_conflict["unattributed_legacy_source_ids"] = legacy_ids
        _retain_candidate(candidates, _candidate(previous, [] if previous_conflict else legacy_ids))
    preferred_source_id = value.preferred_source_id
    if value_type != "missing":
        source_data = source_payload
        if source_data.get("file_id") is not None:
            file_record = get_file(session, source_data["file_id"])
            if file_record.paper_id != value.paper_id:
                raise RebuildWorkflowError("source_file_belongs_to_other_paper")
        if source_data.get("asset_id") is not None:
            asset = get_asset(session, source_data["asset_id"])
            if asset.paper_id != value.paper_id:
                raise RebuildWorkflowError("source_asset_belongs_to_other_paper")
        source, _ = _upsert_source(session, value=value, row=row, data=source_data)
        preferred_source_id = source.id
    else:
        preferred_source_id = None

    _retain_candidate(candidates, _candidate(data, [str(preferred_source_id)] if preferred_source_id else []))
    value.raw_value = data.get("raw_value")
    value.numeric_value = data.get("numeric_value")
    value.unit = data.get("unit")
    value.value_type = value_type
    value.precision_digits = data.get("precision_digits")
    value.is_estimated = value_type == "estimated"
    value.missing_reason = data.get("missing_reason") if value_type == "missing" else None
    value.preferred_source_id = preferred_source_id
    value.conflict = {
        **previous_conflict,
        "candidates": candidates,
        "status": "corrected_previous_retained",
        "previous": previous,
        "history": history,
        "correction_reason": data.get("correction_reason"),
    }
    session.flush()
    return value


def serialize_source(source: RebuildValueSource) -> dict[str, Any]:
    return {
        "id": str(source.id),
        "source_key": source.source_key,
        "source_kind": source.source_kind,
        "file_id": str(source.file_id) if source.file_id else None,
        "asset_id": str(source.asset_id) if source.asset_id else None,
        "page_number": source.page_number,
        "label": source.label,
        "table_row": source.table_row,
        "table_column": source.table_column,
        "quote": source.quote,
        "estimate_basis": source.estimate_basis,
    }


def serialize_value(value: RebuildDataValue, sources: list[RebuildValueSource]) -> dict[str, Any]:
    return {
        "id": str(value.id),
        "field_name": value.field_name,
        "raw_value": value.raw_value,
        "numeric_value": value.numeric_value,
        "unit": value.unit,
        "value_type": value.value_type,
        "precision_digits": value.precision_digits,
        "is_estimated": value.is_estimated,
        "missing_reason": value.missing_reason,
        "conflict": value.conflict,
        "preferred_source_id": str(value.preferred_source_id) if value.preferred_source_id else None,
        "sources": [serialize_source(source) for source in sources],
    }


def serialize_row(session: Session, row: RebuildDataRow) -> dict[str, Any]:
    values = session.scalars(
        select(RebuildDataValue).where(RebuildDataValue.row_id == row.id).order_by(RebuildDataValue.field_name)
    ).all()
    source_map: dict[UUID, list[RebuildValueSource]] = {}
    if values:
        sources = session.scalars(
            select(RebuildValueSource).where(RebuildValueSource.row_id == row.id).order_by(RebuildValueSource.created_at)
        ).all()
        for source in sources:
            source_map.setdefault(source.value_id, []).append(source)
    paper = session.get(Paper, row.paper_id)
    return {
        "id": str(row.id),
        "row_key": row.row_key,
        "paper_id": str(row.paper_id),
        "paper_code": paper.paper_code if paper else None,
        **{field: getattr(row, field) for field in IDENTITY_FIELDS},
        "condition": row.condition or {},
        "identity_revision": row_identity_revision(session, row),
        "identity_status": "pending" if (row.properties or {}).get(IMPORT_META, {}).get("pending_identity_fields") else "confirmed",
        "properties": row.properties or {},
        "notes": row.notes,
        "values": [serialize_value(value, source_map.get(value.id, [])) for value in values],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_rows(
    session: Session,
    *,
    reaction: str | None = None,
    material: str | None = None,
    material_family: str | None = None,
    active_site_type: str | None = None,
    data_type: str | None = None,
    paper_id: UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    statement = select(RebuildDataRow).order_by(RebuildDataRow.created_at.desc())
    if reaction:
        statement = statement.where(RebuildDataRow.reaction == reaction)
    if material:
        statement = statement.where(RebuildDataRow.material.ilike(f"%{material.strip()}%"))
    if material_family:
        statement = statement.where(RebuildDataRow.material_family == material_family)
    if active_site_type:
        statement = statement.where(RebuildDataRow.active_site_type == active_site_type)
    if data_type:
        statement = statement.where(RebuildDataRow.data_type == data_type)
    if paper_id:
        statement = statement.where(RebuildDataRow.paper_id == paper_id)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = session.scalars(statement.limit(limit).offset(offset)).all()
    return {"total": total, "items": [serialize_row(session, row) for row in rows]}


def _linear_regression(points: list[dict[str, float]]) -> dict[str, Any] | None:
    if len(points) < 2:
        return None
    x_values = [point["x"] for point in points]
    y_values = [point["y"] for point in points]
    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(y_values) / len(y_values)
    x_variance = sum((value - x_mean) ** 2 for value in x_values)
    y_variance = sum((value - y_mean) ** 2 for value in y_values)
    if x_variance <= 0 or y_variance <= 0:
        return None
    covariance = sum((x_values[i] - x_mean) * (y_values[i] - y_mean) for i in range(len(points)))
    slope = covariance / x_variance
    intercept = y_mean - slope * x_mean
    correlation = covariance / math.sqrt(x_variance * y_variance)
    return {
        "slope": slope,
        "intercept": intercept,
        "pearson_r": correlation,
        "r_squared": correlation * correlation,
    }


def _condition_signature(condition: dict[str, Any], keys: list[str]) -> tuple[Any, ...]:
    return tuple(_canonical_json(condition.get(key)) for key in keys)


def _row_control_signature(row: RebuildDataRow, comparison_fields: set[str]) -> tuple[Any, ...]:
    controlled_fields = (
        "reaction",
        "data_type",
        "material_family",
        "support",
        "active_site_type",
        "active_site",
        "configuration",
    )
    return tuple(
        _canonical_json(getattr(row, field))
        for field in controlled_fields
        if field not in comparison_fields
    )


def analyze(session: Session, payload: RebuildAnalysisRequest) -> dict[str, Any]:
    x_value = aliased(RebuildDataValue)
    y_value = aliased(RebuildDataValue)
    statement = (
        select(RebuildDataRow, x_value, y_value)
        .join(x_value, (x_value.row_id == RebuildDataRow.id) & (x_value.field_name == payload.x_field))
        .join(y_value, (y_value.row_id == RebuildDataRow.id) & (y_value.field_name == payload.y_field))
        .where(x_value.numeric_value.is_not(None), y_value.numeric_value.is_not(None))
    )
    if payload.paper_id:
        _required_paper(session, payload.paper_id)
        statement = statement.where(RebuildDataRow.paper_id == payload.paper_id)
    if payload.reaction:
        statement = statement.where(RebuildDataRow.reaction == payload.reaction)
    if payload.material:
        statement = statement.where(RebuildDataRow.material.ilike(f"%{payload.material.strip()}%"))
    if payload.material_family:
        statement = statement.where(RebuildDataRow.material_family == payload.material_family)
    if payload.active_site_type:
        statement = statement.where(RebuildDataRow.active_site_type == payload.active_site_type)
    if payload.data_type:
        statement = statement.where(RebuildDataRow.data_type == payload.data_type)
    if not payload.include_estimated:
        statement = statement.where(x_value.value_type != "estimated", y_value.value_type != "estimated")
    statement = statement.where(
        func.coalesce(RebuildDataRow.properties[IMPORT_META]["pending_identity_fields"].as_string(), "[]") == "[]"
    )
    records = session.execute(statement.limit(payload.max_samples)).all()
    warnings: list[str] = []

    comparison_fields = set(payload.comparison_fields or ["material"])
    comparison_condition_keys = set(payload.comparison_condition_keys or [])
    required_condition_keys = set(payload.required_condition_keys or [])
    invalid_condition_keys = required_condition_keys & comparison_condition_keys
    if invalid_condition_keys:
        raise RebuildWorkflowError("condition_key_cannot_be_both_controlled_and_compared")

    condition_keys = sorted(
        {
            key
            for row, _, _ in records
            for key in (row.condition or {})
            if key not in comparison_condition_keys
        }
        | required_condition_keys
    )
    controlled_fields = [
        field
        for field in (
            "reaction",
            "data_type",
            "material_family",
            "support",
            "active_site_type",
            "active_site",
            "configuration",
        )
        if field not in comparison_fields
    ]
    comparison_keys = set(payload.comparison_condition_keys or [])
    comparison_groups: dict[tuple[Any, ...], list[Any]] = {}
    for record in records:
        row, _, _ = record
        signature = (
            _row_control_signature(row, comparison_fields),
            _condition_signature(row.condition or {}, condition_keys),
        )
        comparison_groups.setdefault(signature, []).append(record)
    selected_signature = max(comparison_groups, key=lambda key: len(comparison_groups[key]), default=((), ()))
    selected_records = comparison_groups.get(selected_signature, [])
    excluded_for_conditions = len(records) - len(selected_records)
    if len(comparison_groups) > 1:
        warnings.append(
            f"已排除 {excluded_for_conditions} 条不可比样本；仅保留控制字段 {controlled_fields or '无'} "
            f"和条件键 {condition_keys or '空条件'} 相同的样本。"
        )

    unit_pairs: dict[tuple[str, str], int] = {}
    for _, x_record, y_record in selected_records:
        pair = (x_record.unit or "", y_record.unit or "")
        unit_pairs[pair] = unit_pairs.get(pair, 0) + 1
    selected_pair = max(unit_pairs, key=unit_pairs.get, default=("", ""))
    excluded_for_units = len(selected_records) - unit_pairs.get(selected_pair, 0)
    if len(unit_pairs) > 1:
        warnings.append(
            f"已排除 {excluded_for_units} 条单位不一致样本；仅保留 x={selected_pair[0] or '无单位'}、y={selected_pair[1] or '无单位'} 的样本。"
        )
    points: list[dict[str, Any]] = []
    for row, x_record, y_record in selected_records:
        if (x_record.unit or "", y_record.unit or "") != selected_pair:
            continue
        points.append(
            {
                "row_id": str(row.id),
                "paper_id": str(row.paper_id),
                "material": row.material,
                "reaction": row.reaction,
                "data_type": row.data_type,
                "condition": row.condition or {},
                "x": float(x_record.numeric_value),
                "y": float(y_record.numeric_value),
                "x_value_type": x_record.value_type,
                "y_value_type": y_record.value_type,
            }
        )
    if not payload.include_estimated:
        warnings.append("默认排除估读值；可在页面显式勾选后纳入。")
    regression = _linear_regression(points)
    if len(points) < 2:
        warnings.append("有效配对样本不足 2 条，无法计算回归或相关性。")
    elif regression is None:
        warnings.append("至少一个字段为常量，无法计算回归或相关性。")
    result = {
        "x_field": payload.x_field,
        "y_field": payload.y_field,
        "x_unit": selected_pair[0] or None,
        "y_unit": selected_pair[1] or None,
        "sample_count": len(points),
        "excluded_count": excluded_for_conditions + excluded_for_units,
        "controlled_fields": controlled_fields,
        "comparison_fields": sorted(comparison_fields),
        "condition_keys": condition_keys,
        "comparison_condition_keys": sorted(comparison_keys),
        "comparison_groups": [
            {
                "controlled_fields": controlled_fields,
                "controlled_values": list(signature[0]),
                "condition_keys": condition_keys,
                "condition_values": list(signature[1]),
                "count": len(group),
            }
            for signature, group in sorted(
                comparison_groups.items(), key=lambda item: (-len(item[1]), item[0])
            )
        ],
        "unit_groups": [
            {"x_unit": pair[0] or None, "y_unit": pair[1] or None, "count": count}
            for pair, count in sorted(unit_pairs.items())
        ],
        "regression": regression,
        "points": points,
        "warnings": warnings,
    }
    spec = payload.model_dump(mode="json")
    spec_hash = hashlib.sha256(_canonical_json(spec).encode("utf-8")).hexdigest()
    run = session.scalar(select(RebuildAnalysisRun).where(RebuildAnalysisRun.spec_hash == spec_hash))
    if run is None:
        run = RebuildAnalysisRun(spec_hash=spec_hash, spec=spec)
        session.add(run)
    run.spec = spec
    run.result = result
    run.sample_count = len(points)
    run.warnings = warnings
    session.flush()
    return {"id": str(run.id), **result}


def get_analysis_run(session: Session, run_id: UUID) -> RebuildAnalysisRun:
    run = session.get(RebuildAnalysisRun, run_id)
    if run is None:
        raise RebuildWorkflowError("analysis_not_found")
    return run
