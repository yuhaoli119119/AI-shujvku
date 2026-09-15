"""app.mcp.paper_ml_export — 单篇任务级 ML 导出（统计 + manifest + **有界产物读取**）。

工具（3 个）：
1. ``export_paper_ml_dataset(paper_id, task, ready_only=True, include_payload="none")``
   对单篇论文按任务计算 v3 训练数据集口径，**一次构建**产出 CSV / JSON / manifest 三份
   产物（来自同一份 builder 结果），把产物落到平台的受控存储位置，并返回：
   统计、完整 manifest、真实的文件获取契约，以及**不透明 artifact 引用**
   （artifact_id、每格式字节数与完整 SHA-256、有效期）。
   **默认不内联大 JSON**；仅保留「小体积 CSV / manifest 的显式内联」选项。
2. ``read_ml_export_artifact(paper_id, artifact_id, fmt, offset=0, limit_bytes=65536)``
   按 artifact 引用做**有界分块读取**：字节偏移（UTF-8 字节）、64 KiB 硬上限、
   返回 ``next_offset`` / ``has_more`` / 分块 SHA-256。读取不重新执行 v3 构建器。
3. ``list_ml_export_tasks()``
   只读列出平台真实注册的 tabular 任务，以及每个任务的下载支持矩阵。

设计约束（与 AGENTS.md 单篇流程、用户 R16 / R16.1 / R17 任务书对齐）：
1. **不另写筛选器**：完全复用 ``app.services.dft_export_service``（v3 构建器、
   CSV 列定义与序列化函数）与 ``app.domain.tabular_task_profiles``（任务注册表）。
   本模块只做统计汇总、结果整形与产物读写，不改变任何科学字段、不补标签、不放宽门槛。
2. **一次导出只计算一次**：``build_dft_ml_dataset_v3`` 只调用一次；CSV 由**同一份**
   ``payload["records"]`` 经平台的 ``_v3_csv_row`` / ``_csv_cell`` 序列化得到（因此与
   REST CSV 出口逐字节一致）；JSON / manifest 由同一份 payload 直接序列化。
3. **不绕过证据门槛**：``ready_only`` 固定为 True；显式传 False 直接报错。
4. **不冒充数据**：``evidence_gate_candidate_count``（v2 闸门候选数）与
   ``exported_rows``（本 task 正式训练行数）分开返回、命名清晰，绝不混同。
5. **如实报告文件获取链路**：
   - REST 出口**不是预生成的固定文件**，而是每次访问实时重算的响应；
   - 生产 REST 出口在 Owner 网关 **HTTP Basic** 之后 ⇒ 只有 MCP Bearer 的客户端
     无法经 REST URL 取得文件；**但 artifact 读取不需要 Owner Basic**，
     因此 Owner Basic **不是** MCP-only 执行 AI 获取文件的必需条件；
   - JSON/manifest 端点的响应模型把 ``task`` 固定为 3 值 Literal ⇒ 其余 task ``500``
     （已知平台缺陷，本工具只如实报告、不修复、不为不支持的任务给出可下载链接）。
6. **产物落盘的位置与生命周期（复用既有机制，不新建服务/表）**：
   - 存放位置 = ``settings.storage_root / "by_id" / <paper_id> / "mcp_export_artifacts" / <artifact_id>``。
     ``storage_root``（生产 ``/data/storage``）是平台既有的**受控存储位置**（
     ``ArtifactStore`` 启动即创建、宿主机 bind mount 持久化），``by_id/<paper_id>``
     是 ``app/utils/artifact_paths.py`` 明确识别的既有 per-paper 布局；
   - 不新增配置键、不新增数据库表、不新增文件服务、不创建公开分享链接；
   - 生命周期 = 侧车元数据里的 ``expires_at``（默认 TTL 24h）+ **读取时 fail-closed 判定**
     + 导出时对**同一论文**的过期产物做有界自清理。不做后台守护进程。
7. **artifact_id 不是访问凭据**：读取时仍然要求 ``export_data`` 能力、``exports_enabled``
   开关，并校验产物绑定的**调用方身份**（``MCPAuthInfo.source_identity``）。身份或论文
   不匹配一律拒绝且不返回内容。路径只能由「UUID + 32 位 hex」两个已校验分量拼出，
   不接受任何调用方提供的路径、不读取其他产物。
8. **副作用分别声明**：导出工具会写文件 ⇒ 不再标为纯只读；读取工具确实只读；
   任务清单工具只读。本模块**不写数据库、不写审计**。
9. **未知 task 明确报错**，不伪造空成功结果。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Paper
from app.db.session import session_scope
from app.domain.tabular_task_profiles import (
    TASK_PROFILE_VERSION,
    get_tabular_task_profile,
    list_tabular_task_profiles,
)
from app.mcp.auth import require_mcp_capability
from app.mcp.context import MCPAuthInfo, get_mcp_auth
from app.security.exports import require_mcp_exports_enabled
from app.services.dft_export_service import (
    DFT_ML_DATASET_V3_CSV_COLUMNS,
    _csv_cell,
    _v3_csv_row,
    build_dft_ml_dataset_v3,
)

try:  # Python 3.11+
    from mcp.types import ToolAnnotations
except Exception:  # pragma: no cover - 仅在无 mcp 包环境导入本模块时使用
    ToolAnnotations = None


SCHEMA_VERSION = "paper_ml_export_v3"

# REST 下载端点的响应模型约束：task 字段是 3 值 Literal，其余 task 会 500。
# 该限制来自 app/schemas/dft_export.py 的 TabularTaskKeyV3，属已知平台缺陷，
# 本工具只如实报告、不修复、不为不支持的任务给出可下载链接。
_V3_JSON_TASK_LITERAL = (
    "SRR_LiS:adsorption_energy",
    "SRR_LiS:reaction_barrier",
    "SRR_LiS:rds_gibbs_free_energy",
)

# 生产 Owner 网关对非 /mcp 路径的 HTTP Basic realm（该字符串出现在 401 响应头中，
# 非凭据本体；本工具只报告「需要什么身份」，绝不输出任何凭据）。
_OWNER_BASIC_REALM = "Literature AI Owner"

MAX_CATALYST_ITEMS = 50
MAX_BLOCKER_ITEMS = 20

# —— 响应内联上限：只用于「小体积 CSV / manifest 的显式内联」——
# 默认导出不内联任何大内容；本上限同时用作分块读取的单块硬上限。
MAX_INLINE_ROWS = 500
MAX_INLINE_BYTES = 65536

# —— 有界分块读取 ——
READ_BLOCK_MAX_BYTES = 65536  # 64 KiB，硬上限，**不可由参数绕过**
READ_BLOCK_DEFAULT_BYTES = 65536
OFFSET_UNIT = "utf-8-bytes"
READ_ENCODING = "utf-8"

# —— 产物位置与生命周期 ——
ARTIFACT_ROOT_DIRNAME = "mcp_export_artifacts"
ARTIFACT_METADATA_FILENAME = "artifact.json"
ARTIFACT_SCHEMA_VERSION = "paper_ml_export_artifact_v1"
ARTIFACT_TTL_HOURS = 24
ARTIFACT_TTL_MAX_HOURS = 168  # 7 天，硬上限
ARTIFACT_TTL_MIN_HOURS = 1

_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PAYLOAD_CHOICES = ("none", "csv", "manifest")
_ARTIFACT_FORMATS = ("csv", "json", "manifest")

_FORMAT_SPEC: dict[str, dict[str, str]] = {
    "csv": {"filename": "dataset.csv", "media_type": "text/csv; charset=utf-8"},
    "json": {"filename": "dataset.json", "media_type": "application/json"},
    "manifest": {"filename": "manifest.json", "media_type": "application/json"},
}


# ---------------------------------------------------------------------------
# 错误：稳定错误码前缀，便于调用方与测试精确断言（不泄露文件系统路径）
# ---------------------------------------------------------------------------
class MLExportArtifactError(ValueError):
    code = "artifact_error"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class ArtifactNotFoundError(MLExportArtifactError):
    code = "artifact_not_found"


class ArtifactIncompleteError(MLExportArtifactError):
    code = "artifact_incomplete"


class ArtifactExpiredError(MLExportArtifactError):
    code = "artifact_expired"


class ArtifactForbiddenError(MLExportArtifactError):
    code = "artifact_forbidden"


class ArtifactCorruptError(MLExportArtifactError):
    code = "artifact_corrupt"


class InvalidArtifactReferenceError(MLExportArtifactError):
    code = "invalid_artifact_reference"


class IdentityNotVerifiedError(MLExportArtifactError):
    code = "identity_not_verified"


# ---------------------------------------------------------------------------
# 基础工具函数
# ---------------------------------------------------------------------------
def _enforce_postgres_read_only_transaction(session: Any) -> bool:
    """把本查询事务设为只读，保证导出聚合零写入（与状态工具同机制）。"""
    try:
        bind = session.get_bind()
        if bind.dialect.name != "postgresql":
            return False
        session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
        return True
    except AttributeError:
        return False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _top_counter(counter: dict[str, int], limit: int) -> dict[str, int]:
    if len(counter) <= limit:
        return dict(counter)
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    head = dict(ranked[:limit])
    head["__other__"] = sum(count for _, count in ranked[limit:])
    return head


def _normalize_payload_request(value: Any) -> str:
    v = str(value if value is not None else "none").strip().lower()
    if v in ("", "none"):
        return "none"
    if v == "json":
        raise ValueError(
            "include_payload='json' is not supported: the full JSON dataset is delivered "
            "through the bounded artifact reader (read_ml_export_artifact) instead of being "
            "inlined into the response. Request the artifact and page through it in 64 KiB blocks."
        )
    if v not in _PAYLOAD_CHOICES:
        raise ValueError(
            f"Invalid include_payload: {value!r}. Allowed: {list(_PAYLOAD_CHOICES)}"
        )
    return v


def _task_supports_json_endpoint(task_key: str) -> bool:
    return task_key in _V3_JSON_TASK_LITERAL


def _derive_csv_from_payload(payload: dict[str, Any]) -> str:
    """从**同一份** v3 payload 派生 CSV（复用平台序列化函数 ⇒ 与 REST CSV 逐字节一致）。

    这里刻意不调用 ``build_dft_ml_dataset_v3_csv``：那会再跑一次 v3 构建器，
    违反「一次导出只计算一次」。改为复用平台的 ``_v3_csv_row`` / ``_csv_cell``，
    与 REST 出口走的是同一套列定义与同一套取值映射。
    """
    output = io.StringIO()
    writer = csv.DictWriter(
        output, fieldnames=DFT_ML_DATASET_V3_CSV_COLUMNS, lineterminator="\n"
    )
    writer.writeheader()
    for record in payload["records"]:
        writer.writerow({column: _csv_cell(value) for column, value in _v3_csv_row(record).items()})
    return output.getvalue()


def _rest_entry(
    path: str,
    *,
    profile_key: str,
    paper_id: UUID,
    media_type: str,
    supports_json_endpoint: bool,
    filename: str | None = None,
) -> dict[str, Any]:
    """单个 REST 出口的**真实**契约描述（不含任何凭据）。"""
    available = bool(supports_json_endpoint) if path != "/api/dft/ml-dataset-v3.csv" else True
    entry: dict[str, Any] = {
        "method": "GET",
        "url_path": path,
        "query": {"task": profile_key, "paper_id": str(paper_id), "ready_only": True},
        "media_type": media_type,
        # —— 交付契约事实 ——
        "available_for_this_task": available,
        "is_fixed_file": False,
        "regenerated_per_request": True,
        "mcp_bearer_sufficient": False,
        "requires_owner_http_basic": True,
        "owner_basic_realm": _OWNER_BASIC_REALM,
        "note": (
            "该 REST 出口不是 MCP-only 客户端的必需路径：同样的内容可通过 "
            "artifact 引用 + 分块读取取得（不需要 Owner HTTP Basic）。"
        ),
        "supported_tasks": (
            "all_registered_tasks"
            if path == "/api/dft/ml-dataset-v3.csv"
            else list(_V3_JSON_TASK_LITERAL)
        ),
    }
    if filename:
        entry["filename"] = filename
    if not available:
        entry["unavailable_reason"] = (
            "该端点的响应模型把 task 固定为 3 值 Literal；本 task 会返回 500 "
            "ResponseValidationError（已知平台缺陷，未修复）。**不提供看似可用的下载链接**。"
        )
    return entry


# ---------------------------------------------------------------------------
# 产物存储（复用既有受控存储位置，不新建服务/表）
# ---------------------------------------------------------------------------
def _paper_artifact_root(settings: Settings, paper_id: UUID) -> Path:
    return settings.storage_root / "by_id" / str(paper_id) / ARTIFACT_ROOT_DIRNAME


def _artifact_dir(settings: Settings, paper_id: UUID, artifact_id: str) -> Path:
    return _paper_artifact_root(settings, paper_id) / artifact_id


def _artifact_storage_ref(paper_id: UUID, artifact_id: str) -> str:
    """对外只暴露相对引用（平台既有 ``by_id/...`` 风格），不泄露绝对路径。"""
    return f"by_id/{paper_id}/{ARTIFACT_ROOT_DIRNAME}/{artifact_id}"


def _assert_contained(path: Path, settings: Settings) -> Path:
    """所有产物路径必须落在受控存储根之下；任何逃逸都直接拒绝。"""
    storage_root = settings.storage_root.resolve()
    resolved = path.resolve(strict=False)
    if resolved != storage_root and not resolved.is_relative_to(storage_root):
        raise ArtifactForbiddenError("artifact path escapes the controlled storage root")
    return resolved


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _materialize_export_artifact(
    *,
    settings: Settings,
    paper_id: UUID,
    task: str,
    owner: MCPAuthInfo,
    contents: dict[str, tuple[str, bytes]],
    ttl_hours: int,
    extra_metadata: dict[str, Any] | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """把一次构建产出的三份内容写成**一个** artifact（侧车最后原子落盘）。

    ``contents`` = ``{"csv": (media_type, bytes), "json": (...), "manifest": (...)}``。
    侧车 ``artifact.json`` 的**存在与否**即「该 artifact 是否完整可用」的判据：
    内容文件先写、侧车最后写入，因此中途失败不会留下「半成品却被当成有效产物」。
    """
    if owner.source_identity is None or not owner.identity_verified:
        raise IdentityNotVerifiedError(
            "export artifacts are bound to a verified MCP caller identity; "
            "the current caller has no verified identity"
        )

    ttl = int(ttl_hours)
    if ttl < ARTIFACT_TTL_MIN_HOURS or ttl > ARTIFACT_TTL_MAX_HOURS:
        raise ValueError(
            f"invalid artifact_ttl_hours {ttl}: must be within "
            f"[{ARTIFACT_TTL_MIN_HOURS}, {ARTIFACT_TTL_MAX_HOURS}]"
        )

    token = artifact_id or secrets.token_hex(16)
    if not _ARTIFACT_ID_RE.match(token):
        raise InvalidArtifactReferenceError("generated artifact id is not a 32-char hex token")

    target_dir = _assert_contained(_artifact_dir(settings, paper_id, token), settings)
    target_dir.mkdir(parents=True, exist_ok=True)

    created = _utc_now()
    expires = created + timedelta(hours=ttl)

    formats: dict[str, Any] = {}
    for fmt, (media_type, raw) in contents.items():
        spec = _FORMAT_SPEC[fmt]
        _write_bytes_atomic(target_dir / spec["filename"], raw)
        formats[fmt] = {
            "filename": spec["filename"],
            "media_type": media_type,
            "byte_count": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "encoding": READ_ENCODING,
        }

    metadata: dict[str, Any] = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_id": token,
        "paper_id": str(paper_id),
        "task": task,
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
        "ttl_hours": ttl,
        "owner_identity": owner.source_identity,
        "owner_display_name": owner.display_name,
        "owner_identity_verified": bool(owner.identity_verified),
        "producer_tool": "export_paper_ml_dataset",
        "producer_schema_version": SCHEMA_VERSION,
        "storage_ref": _artifact_storage_ref(paper_id, token),
        "offset_unit": OFFSET_UNIT,
        "block_max_bytes": READ_BLOCK_MAX_BYTES,
        "formats": formats,
        "bytes_total": sum(entry["byte_count"] for entry in formats.values()),
        "identity_binding_note": (
            "本产物绑定导出方身份；读取时按当前调用方身份重新校验。"
            "artifact_id 本身不是访问凭据。"
        ),
        "side_effects": ["写产物文件到平台受控存储（不写数据库、不写审计、不建分享链接）"],
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    _write_bytes_atomic(
        target_dir / ARTIFACT_METADATA_FILENAME,
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"),
    )
    return metadata


def _prune_expired_artifacts(settings: Settings, paper_id: UUID) -> dict[str, Any]:
    """有界自清理：只清**本论文**目录下已过期的 artifact 与无侧车的半成品。

    绝不清除其他论文的目录；其他 task 的**未过期**产物一律保留。
    不做后台守护进程，不新增目录/表。
    """
    root = _paper_artifact_root(settings, paper_id)
    summary = {"scanned": 0, "pruned_expired": [], "pruned_incomplete": [], "kept": 0}
    if not root.is_dir():
        return summary

    now = _utc_now()
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not _ARTIFACT_ID_RE.match(child.name):
            continue
        summary["scanned"] += 1
        meta_path = child / ARTIFACT_METADATA_FILENAME
        if not meta_path.is_file():
            marker = child / ".materializing"
            # 只清理「无侧车且已超过 TTL」的半成品，避免删掉正在进行中的写入。
            try:
                age_hours = (now - datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
            except OSError:
                continue
            if age_hours > ARTIFACT_TTL_HOURS and not marker.exists():
                _safe_rmtree(child, settings)
                summary["pruned_incomplete"].append(child.name)
            else:
                summary["kept"] += 1
            continue

        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            _safe_rmtree(child, settings)
            summary["pruned_incomplete"].append(child.name)
            continue

        expires = _parse_iso(metadata.get("expires_at"))
        # 仅清理「侧车可解析且已到期」的产物；expires_at 缺失/不可解析时
        # 由读取路径明确拒绝（ArtifactCorruptError），但本清理逻辑不新增删除——
        # 维持原有行为：缺到期字段视为未过期，保留目录，不自动回收。
        if expires is not None and expires <= now:
            _safe_rmtree(child, settings)
            summary["pruned_expired"].append(child.name)
        else:
            summary["kept"] += 1
    return summary


def _safe_rmtree(path: Path, settings: Settings) -> None:
    import shutil

    resolved = _assert_contained(path, settings)
    storage_root = settings.storage_root.resolve()
    if resolved == storage_root:
        raise ArtifactForbiddenError("refusing to remove the storage root itself")
    shutil.rmtree(resolved, ignore_errors=True)


def load_artifact_metadata(
    settings: Settings, *, paper_id: UUID, artifact_id: str
) -> dict[str, Any]:
    """定位并校验 artifact 是否存在/完整/未过期/身份匹配。**不执行任何导出**。"""
    if not _ARTIFACT_ID_RE.match(str(artifact_id or "")):
        raise InvalidArtifactReferenceError(
            "artifact_id must be a 32-character lowercase hex token"
        )
    directory = _assert_contained(_artifact_dir(settings, paper_id, artifact_id), settings)
    if not directory.is_dir():
        raise ArtifactNotFoundError(
            "no artifact with this artifact_id for this paper_id "
            "(artifacts are never silently regenerated and are never reused under a new id)"
        )
    meta_path = directory / ARTIFACT_METADATA_FILENAME
    if not meta_path.is_file():
        raise ArtifactIncompleteError(
            "artifact directory exists but its metadata sidecar is missing; "
            "the artifact is not usable"
        )
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ArtifactCorruptError(f"artifact metadata is unreadable: {type(exc).__name__}") from exc

    if str(metadata.get("paper_id")) != str(paper_id):
        raise ArtifactNotFoundError("artifact does not belong to the requested paper_id")

    expires = _parse_iso(metadata.get("expires_at"))
    if expires is None:
        # fail-closed：侧车存在但 expires_at 缺失或不可解析时，绝不放行。
        raise ArtifactCorruptError(
            "artifact metadata has no usable expires_at; refusing to serve an artifact whose "
            "expiry cannot be evaluated. Re-export to obtain a NEW artifact id "
            "(artifact ids are never reused under a new id)"
        )
    if expires <= _utc_now():
        raise ArtifactExpiredError(
            f"artifact expired at {metadata.get('expires_at')}; "
            "call export_paper_ml_dataset again to obtain a NEW artifact id "
            "(expired artifacts are never re-exported under the old id)"
        )
    return metadata


def read_artifact_chunk(
    settings: Settings,
    *,
    paper_id: UUID,
    artifact_id: str,
    fmt: str,
    offset: int,
    limit_bytes: int,
    owner: MCPAuthInfo,
) -> dict[str, Any]:
    """有界分块读取。偏移单位为 **UTF-8 字节**；块边界对齐到字符边界 ⇒ 无损重组。"""
    fmt_key = str(fmt or "").strip().lower()
    if fmt_key not in _ARTIFACT_FORMATS:
        raise ValueError(f"invalid_format: fmt must be one of {list(_ARTIFACT_FORMATS)}")

    try:
        offset_value = int(offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_offset: offset must be an integer byte offset") from exc
    if offset_value < 0:
        raise ValueError("invalid_offset: offset must be >= 0")

    try:
        limit_value = int(limit_bytes)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_limit: limit_bytes must be an integer") from exc
    if limit_value <= 0:
        raise ValueError("invalid_limit: limit_bytes must be >= 1")
    if limit_value > READ_BLOCK_MAX_BYTES:
        raise ValueError(
            f"invalid_limit: limit_bytes {limit_value} exceeds the hard block cap "
            f"{READ_BLOCK_MAX_BYTES}; the cap cannot be raised or bypassed by any parameter"
        )

    if owner.source_identity is None or not owner.identity_verified:
        raise IdentityNotVerifiedError(
            "artifact reads require a verified MCP caller identity"
        )

    metadata = load_artifact_metadata(settings, paper_id=paper_id, artifact_id=artifact_id)

    if metadata.get("owner_identity") != owner.source_identity:
        # 不泄露任何内容、也不区分「存在但不是你的」为可枚举信息。
        raise ArtifactForbiddenError(
            "this artifact is bound to a different MCP identity; "
            "artifact ids are not transferable credentials"
        )

    entry = (metadata.get("formats") or {}).get(fmt_key)
    if not isinstance(entry, dict):
        raise ArtifactCorruptError(f"artifact metadata has no entry for format {fmt_key!r}")

    directory = _assert_contained(_artifact_dir(settings, paper_id, artifact_id), settings)
    file_path = _assert_contained(directory / str(entry.get("filename")), settings)
    if not file_path.is_file():
        raise ArtifactCorruptError(f"artifact content file for {fmt_key!r} is missing")

    byte_count = int(entry.get("byte_count") or 0)
    actual_size = file_path.stat().st_size
    if actual_size != byte_count:
        raise ArtifactCorruptError(
            f"artifact content size {actual_size} does not match the recorded {byte_count}; "
            "refusing to serve a changed artifact"
        )

    if offset_value > byte_count:
        raise ValueError(
            f"invalid_offset: offset {offset_value} is past the end of the artifact ({byte_count} bytes)"
        )

    # 多读 4 字节以完成跨块字符的边界回退（最多回退 3 字节）。
    with file_path.open("rb") as handle:
        handle.seek(offset_value)
        window = handle.read(limit_value + 4)

    chunk_len = _utf8_chunk_end(window, limit_value)
    chunk = window[:chunk_len]
    next_offset = offset_value + chunk_len
    has_more = next_offset < byte_count

    try:
        text = chunk.decode(READ_ENCODING)
    except UnicodeDecodeError as exc:  # pragma: no cover - 边界对齐后不应发生
        raise ArtifactCorruptError(
            f"artifact content is not valid {READ_ENCODING} at offset {offset_value}"
        ) from exc

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": metadata.get("artifact_id"),
        "paper_id": metadata.get("paper_id"),
        "task": metadata.get("task"),
        "fmt": fmt_key,
        "media_type": entry.get("media_type"),
        "offset": offset_value,
        "offset_unit": OFFSET_UNIT,
        "limit_bytes": limit_value,
        "block_max_bytes": READ_BLOCK_MAX_BYTES,
        "chunk_byte_count": len(chunk),
        "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
        "byte_count": byte_count,
        "content_sha256": entry.get("sha256"),
        "next_offset": next_offset,
        "has_more": has_more,
        "is_last_chunk": not has_more,
        "content": text,
        "encoding": READ_ENCODING,
        "artifact_expires_at": metadata.get("expires_at"),
        "owner_identity": metadata.get("owner_identity"),
        "recomputed": False,
        "v3_builder_invocations": 0,
        "side_effects": [],
        "read_only": True,
        "notes": [
            "偏移单位是 **UTF-8 字节**；块边界已对齐到字符边界，"
            "因此按 next_offset 顺序拼接各块 content 可无损还原全文。",
            "读取 **不** 重新执行 v3 构建器：内容在导出时已固化在受控存储中，"
            "重复读取同一块返回相同 chunk_sha256。",
            "把各块的 content 拼接后计算 SHA-256，应与 content_sha256（整份产物的 SHA-256）一致。",
        ],
    }


def _utf8_chunk_end(window: bytes, limit: int) -> int:
    """返回 `window` 中 <= limit 的最大 UTF-8 字符边界。

    若 ``window[0]`` 处的字符本身就长于 ``limit``，则返回该字符的完整长度：
    这样 ``next_offset`` 一定向前推进（保证分页一定终止）。
    """
    size = len(window)
    if limit >= size:
        return size
    index = limit
    while index > 0 and (window[index] & 0xC0) == 0x80:
        index -= 1
    if index > 0:
        return index
    index = 1
    while index < size and (window[index] & 0xC0) == 0x80:
        index += 1
    return index


# ---------------------------------------------------------------------------
# 导出口径组装
# ---------------------------------------------------------------------------
def build_paper_ml_export(
    session: Session,
    *,
    paper_id: UUID,
    task: str,
    ready_only: bool,
    include_payload: str = "none",
    artifacts_enabled: bool = True,
    artifact_ttl_hours: int = ARTIFACT_TTL_HOURS,
    settings: Settings | None = None,
    owner: MCPAuthInfo | None = None,
) -> dict[str, Any]:
    """组装单篇任务级 ML 导出口径。所有计数均为当前数据库实时计算。"""
    if ready_only is not True:
        raise ValueError(
            "ready_only=false is not supported: the formal training-data exit must not "
            "bypass the evidence gate (R16)."
        )

    requested_payload = _normalize_payload_request(include_payload)
    runtime_settings = settings or get_settings()
    caller = owner if owner is not None else get_mcp_auth()

    # 未知 / 暂不支持的 task → 明确错误（不伪造空成功结果）。
    try:
        profile = get_tabular_task_profile(task)
    except KeyError as exc:
        raise ValueError(
            f"Unknown or unsupported task: {task!r}. "
            "Call list_ml_export_tasks() for the registered task set."
        ) from exc

    paper = session.get(Paper, paper_id)
    if paper is None:
        raise ValueError("Paper not found")

    # —— 唯一一次 v3 构建 ——
    payload = build_dft_ml_dataset_v3(
        session,
        task=task,
        ready_only=True,
        paper_id=paper_id,
        limit=None,
    )
    manifest = payload["manifest"]
    records = payload["records"]

    # CSV / JSON / manifest 三份产物都来自上面这**同一份** payload。
    csv_text = _derive_csv_from_payload(payload)
    json_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str)

    catalyst_rows: dict[str, int] = {}
    catalyst_names: dict[str, str | None] = {}
    for record in records:
        catalyst = record.get("catalyst") or {}
        sample_id = catalyst.get("catalyst_sample_id")
        key = str(sample_id) if sample_id else "__unbound__"
        catalyst_rows[key] = catalyst_rows.get(key, 0) + 1
        if key not in catalyst_names:
            catalyst_names[key] = catalyst.get("name")

    catalyst_items = [
        {
            "catalyst_sample_id": key,
            "name": catalyst_names.get(key),
            "rows": rows,
        }
        for key, rows in sorted(catalyst_rows.items(), key=lambda item: (-item[1], item[0]))
    ]

    exported_rows = len(records)
    csv_supported = True
    json_supported = _task_supports_json_endpoint(profile.key)

    # 零合格行时给出显式原因（不返回含糊的空成功）。
    zero_reason: str | None = None
    if exported_rows == 0:
        if (manifest.get("source_candidate_count") or 0) == 0:
            zero_reason = (
                "evidence gate delivered 0 candidates for this paper: no DFT records are eligible "
                "at all, so there is nothing to export"
            )
        elif (manifest.get("candidate_count") or 0) == 0:
            zero_reason = (
                "all evidence-gate candidates were excluded by the task rules; see excluded_counts"
            )
        elif (manifest.get("task_candidate_count") or 0) == 0:
            zero_reason = (
                "candidates exist but none are simultaneously label_ready and tabular_ready; "
                "see excluded_counts and completeness"
            )
        else:
            zero_reason = "ready_only removed all remaining candidates; see excluded_counts"

    rest_downloads = {
        "csv": _rest_entry(
            "/api/dft/ml-dataset-v3.csv",
            profile_key=profile.key,
            paper_id=paper_id,
            media_type="text/csv; charset=utf-8",
            supports_json_endpoint=True,
            filename=f"dft_ml_dataset_v3_{profile.key.replace(':', '_')}.csv",
        ),
        "json": _rest_entry(
            "/api/dft/ml-dataset-v3",
            profile_key=profile.key,
            paper_id=paper_id,
            media_type="application/json",
            supports_json_endpoint=json_supported,
        ),
        "manifest": _rest_entry(
            "/api/dft/ml-dataset-v3/manifest",
            profile_key=profile.key,
            paper_id=paper_id,
            media_type="application/json",
            supports_json_endpoint=json_supported,
        ),
    }

    identity_ok = bool(caller and caller.source_identity and caller.identity_verified)
    artifact_block: dict[str, Any]
    prune_summary: dict[str, Any] | None = None
    if artifacts_enabled and identity_ok:
        assert caller is not None  # narrowing for type checkers
        prune_summary = _prune_expired_artifacts(runtime_settings, paper_id)
        metadata = _materialize_export_artifact(
            settings=runtime_settings,
            paper_id=paper_id,
            task=profile.key,
            owner=caller,
            contents={
                "csv": ("text/csv; charset=utf-8", csv_text.encode("utf-8")),
                "json": ("application/json", json_text.encode("utf-8")),
                "manifest": ("application/json", manifest_text.encode("utf-8")),
            },
            ttl_hours=artifact_ttl_hours,
            extra_metadata={
                "record_count": exported_rows,
                "source_snapshot_fingerprint": manifest.get("source_snapshot_fingerprint"),
                "manifest_created_at": manifest.get("created_at"),
                "csv_derived_from": "same build payload (no second v3 invocation)",
            },
        )
        artifact_block = {
            "available": True,
            "artifact_id": metadata["artifact_id"],
            "paper_id": metadata["paper_id"],
            "task": metadata["task"],
            "created_at": metadata["created_at"],
            "expires_at": metadata["expires_at"],
            "ttl_hours": metadata["ttl_hours"],
            "storage_ref": metadata["storage_ref"],
            "storage_location": "平台既有受控存储（storage_root/by_id/<paper_id>/mcp_export_artifacts）",
            "owner_bound": True,
            "owner_identity": metadata["owner_identity"],
            "formats": {
                fmt: {
                    "byte_count": entry["byte_count"],
                    "sha256": entry["sha256"],
                    "media_type": entry["media_type"],
                }
                for fmt, entry in metadata["formats"].items()
            },
            "bytes_total": metadata["bytes_total"],
            "read_tool": "read_ml_export_artifact",
            "read_params": {
                "paper_id": metadata["paper_id"],
                "artifact_id": metadata["artifact_id"],
                "fmt": list(_ARTIFACT_FORMATS),
                "offset": 0,
                "limit_bytes": READ_BLOCK_MAX_BYTES,
            },
            "offset_unit": OFFSET_UNIT,
            "block_max_bytes": READ_BLOCK_MAX_BYTES,
            "requires_owner_http_basic": False,
            "mcp_bearer_sufficient": True,
            "pruned_expired_artifacts": (prune_summary or {}).get("pruned_expired", []),
            "pruned_incomplete_artifacts": (prune_summary or {}).get("pruned_incomplete", []),
            "note": (
                "产物来自**同一次** v3 构建（CSV 由同一份 payload 派生，未二次调用构建器）。"
                "读取用 read_ml_export_artifact 按 64 KiB 块分页，**不需要 Owner HTTP Basic**；"
                "artifact 绑定调用方身份，读取时重新校验。过期或缺失一律明确报错，"
                "不会偷偷重新导出并沿用原 ID。"
            ),
        }
    elif artifacts_enabled:
        artifact_block = {
            "available": False,
            "reason_if_unavailable": "caller_identity_not_verified",
            "note": "产物绑定已验证的 MCP 调用方身份；当前调用方没有可验证身份，故不落盘。",
        }
    else:
        artifact_block = {
            "available": False,
            "reason_if_unavailable": "artifacts_disabled_by_caller",
        }

    inline_block = _inline_payload_block(
        records=records,
        requested=requested_payload,
        csv_text=csv_text,
        manifest_text=manifest_text,
    )

    downloads = {
        "delivery_contract": {
            "is_fixed_file": False,
            "regenerated_per_request": True,
            "rest_endpoints_require": "HTTP Basic（Owner 网关 auth_basic \"Literature AI Owner\"）",
            "mcp_bearer_sufficient_for_rest_download": False,
            "mcp_bearer_sufficient_for_artifact_read": True,
            "owner_basic_required_for_mcp_only_executor": False,
            "note": (
                "既有 REST 出口**不是预生成的固定文件**，而是每次访问实时重算的响应；"
                "生产上这些路径落在 Owner 网关 location / 的 HTTP Basic 之后（仅 /mcp、"
                "/api/health、/oauth/、/.well-known/ 例外）。MCP-only 执行 AI 取数的路径是"
                "「导出生成 artifact → read_ml_export_artifact 分块读取」，**该路径不需要 "
                "Owner Basic**，因此 Owner Basic 不是 MCP-only 执行 AI 获取文件的必需条件。"
                "本工具不新建文件服务/存储、不创建公开分享链接。"
            ),
        },
        "rest": rest_downloads,
        "artifact": artifact_block,
        "mcp_native_inline": inline_block,
    }

    snapshot_semantics = {
        "output_is_fixed_snapshot": False,
        "output_is_realtime_recompute": True,
        "artifact_is_fixed_snapshot": True,
        "artifact_snapshot_scope": (
            "artifact 是导出那一刻的字节固化副本：导出之后源数据再变化，**既有 artifact 的字节不变**；"
            "需要新数据必须重新导出并取得**新的 artifact_id**。"
        ),
        "source_snapshot_fingerprint": manifest.get("source_snapshot_fingerprint"),
        "source_snapshot_fingerprint_is_data_hash": False,
        "source_snapshot_fingerprint_scope": (
            "源 PDF 清单 / 评审范围完整度指纹（DFTCompletenessService 产出），"
            "**不是导出行的内容哈希**，不能用作「不可变快照 / 内容未被篡改」的校验；"
            "artifact 的完整性请用 artifact 的 sha256 校验。"
        ),
        "created_at_changes_per_build": True,
        "csv_contains_timestamp": False,
        "note": (
            "REST 出口每次访问都重算；CSV 无时间戳 ⇒ 同一库状态下重复构建字节稳定；"
            "JSON/manifest 含 created_at（每次构建都变）⇒ 两次构建字节不同。"
            "artifact 则把某一次构建的字节固化下来，TTL 内字节不变。"
        ),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": _utc_now_iso(),
        "read_only_transaction": True,
        "caller": {
            "identity": caller.source_identity if caller else None,
            "display_name": caller.display_name if caller else None,
            "identity_verified": bool(caller.identity_verified) if caller else False,
            "capabilities_hint": sorted(caller.capabilities) if caller else [],
        },
        "paper": {
            "paper_id": str(paper.id),
            "paper_code": paper.paper_code,
            "title": paper.title,
            "doi": paper.doi,
            "year": paper.year,
            "library_name": paper.library_name,
        },
        "task": profile.key,
        "task_input": task,
        "task_status": profile.status,
        "task_profile_version": profile.version,
        "reaction_profile": profile.reaction_type,
        "reaction_profile_version": manifest.get("reaction_profile_version"),
        "contract": {
            "schema_version": manifest.get("schema_version"),
            "dataset_version": manifest.get("dataset_version"),
            "source_schema_version": manifest.get("source_schema_version"),
            "source_dataset_version": manifest.get("source_dataset_version"),
            "normalization_version": manifest.get("normalization_version"),
            "identity_version": manifest.get("identity_version"),
            "created_at": manifest.get("created_at"),
        },
        "manifest": manifest,
        "counts": {
            "evidence_gate_candidate_count": manifest.get("source_candidate_count"),
            "post_exclusion_candidate_count": manifest.get("candidate_count"),
            "label_ready_count": manifest.get("label_ready_count"),
            "tabular_ready_count": manifest.get("tabular_ready_count"),
            "exported_rows": exported_rows,
            "catalyst_count": len([k for k in catalyst_rows if k != "__unbound__"]),
        },
        "result": {
            "status": "ok" if exported_rows > 0 else "zero_rows",
            "reason_if_zero": zero_reason,
        },
        "excluded_counts": _top_counter(
            {str(k): int(v) for k, v in (manifest.get("excluded_counts") or {}).items()},
            MAX_BLOCKER_ITEMS,
        ),
        "completeness": {
            "lifecycle_reconciled": manifest.get("lifecycle_reconciled"),
            "review_scope_complete": manifest.get("review_scope_complete"),
            "is_complete": manifest.get("is_complete"),
            "exported_verified_rows": manifest.get("exported_verified_rows"),
            "excluded_rows": manifest.get("excluded_rows"),
            "completeness_blockers": manifest.get("completeness_blockers") or [],
            "review_scope_blockers": manifest.get("review_scope_blockers") or [],
            "source_snapshot_fingerprint": manifest.get("source_snapshot_fingerprint"),
        },
        "catalysts": {
            "count": len([k for k in catalyst_rows if k != "__unbound__"]),
            "returned": min(len(catalyst_items), MAX_CATALYST_ITEMS),
            "has_more": len(catalyst_items) > MAX_CATALYST_ITEMS,
            "items": catalyst_items[:MAX_CATALYST_ITEMS],
            "note": "催化剂按 catalyst.catalyst_sample_id 分组；__unbound__ 表示记录无催化剂绑定",
        },
        "download_matrix": {
            "csv": {"rest_available": csv_supported, "artifact_available": True, "inline_available": True},
            "json": {"rest_available": json_supported, "artifact_available": True, "inline_available": False},
            "manifest": {
                "rest_available": json_supported,
                "artifact_available": True,
                "inline_available": True,
            },
            "note": (
                "artifact_available=True 表示可用 artifact 引用 + 分块读取经 MCP 取得（推荐路径，"
                "无需 Owner Basic）；rest_available 表示该 REST 端点的响应模型是否支持本 task；"
                "inline_available 仅针对**小体积**内容（上限 "
                f"{MAX_INLINE_BYTES} B），大 JSON 一律走 artifact 读取。"
            ),
        },
        "downloads": downloads,
        "snapshot_semantics": snapshot_semantics,
        "notes": [
            "evidence_gate_candidate_count 是 v2 证据闸门候选数，不是本 task 的训练行数；"
            "exported_rows 才是本 task 的正式训练数据行数。两者口径不同，不得混同。",
            "默认响应只含统计 + manifest + 获取方式，**不内联大 JSON**。"
            "完整 CSV/JSON/manifest 通过 artifact_id + read_ml_export_artifact 分块取得。",
            "REST 出口是实时重算、且需 Owner HTTP Basic；artifact 读取不需要，"
            "因此 Owner Basic 不是 MCP-only 执行 AI 获取文件的必需条件。",
            "source_snapshot_fingerprint 是源 PDF/评审范围指纹，**不是**导出内容哈希；"
            "artifact 的完整性请用 artifact 自身的 sha256。",
            "本工具会**写产物文件**到平台既有受控存储（因此不再声明为纯只读）；"
            "仍不写数据库、不写审计、不建分享链接。读取工具无副作用。",
            "result.status / result.reason_if_zero 显式声明是否零行以及原因，不返回含糊的空成功。",
            "未见任何绕过证据门槛的参数：ready_only 恒为 True。",
            "task_profile.status 为 candidate（平台任务表当前状态），非人工科研验收结论。",
        ],
    }


def _inline_payload_block(
    *,
    records: list[dict[str, Any]],
    requested: str,
    csv_text: str,
    manifest_text: str,
) -> dict[str, Any]:
    """按需内联**小体积**内容（默认不内联；大 JSON 已从选项中移除）。"""
    blocks: dict[str, Any] = {
        "requested": requested,
        "delivered": None,
        "available": False,
        "reason_if_unavailable": None,
        "media_type": None,
        "row_count": len(records),
        "byte_count": None,
        "sha256": None,
        "content": None,
        "cap": {"max_rows": MAX_INLINE_ROWS, "max_bytes": MAX_INLINE_BYTES},
        "note": (
            "内联仅用于**小体积**内容（CSV / manifest）。完整数据集请用 artifact 引用 + "
            "read_ml_export_artifact 分块读取；大 JSON 不再内联（该选项已移除）。"
        ),
    }
    if requested == "none":
        blocks["reason_if_unavailable"] = "not_requested"
        return blocks

    if len(records) > MAX_INLINE_ROWS:
        blocks["reason_if_unavailable"] = (
            f"row_count {len(records)} exceeds inline row cap {MAX_INLINE_ROWS}; "
            "refusing to silently truncate —请改用 artifact 分块读取（无需 Owner Basic）"
        )
        return blocks

    if requested == "csv":
        text, media_type = csv_text, "text/csv; charset=utf-8"
    else:  # manifest
        text, media_type = manifest_text, "application/json"

    raw = text.encode("utf-8")
    blocks["byte_count"] = len(raw)
    if len(raw) > MAX_INLINE_BYTES:
        blocks["reason_if_unavailable"] = (
            f"byte_count {len(raw)} exceeds inline cap {MAX_INLINE_BYTES}; "
            "refusing to silently truncate —请改用 artifact 分块读取（无需 Owner Basic）"
        )
        return blocks

    blocks.update(
        {
            "delivered": requested,
            "available": True,
            "media_type": media_type,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content": text,
        }
    )
    return blocks


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------
def register_paper_ml_export_tools(server: Any) -> dict[str, str]:
    """把单篇任务级 ML 导出工具注册到给定 FastMCP 实例。"""

    export_description = (
        "Task-scoped machine-learning export for ONE paper, reusing the platform v3 tabular "
        "builder (no new filter). ONE export computes the dataset ONCE and materializes "
        "csv/json/manifest from that SAME build into a bounded, TTL'd artifact; the response "
        "returns summary counts, the FULL manifest, and the real acquisition contract plus the "
        "opaque artifact reference (artifact_id, per-format byte_count + full sha256, expiry). "
        "By default NOTHING large is inlined. Key facts: (a) the REST download endpoints are NOT "
        "pre-generated fixed files — they are recomputed per request, and on production they sit "
        "behind the Owner gateway HTTP Basic, so an MCP-Bearer-only client cannot fetch them via "
        "URL; (b) the MCP-only path is: export -> read_ml_export_artifact, which pages through the "
        "artifact in <=64 KiB blocks and requires NO Owner HTTP Basic — Owner Basic is therefore "
        "NOT a prerequisite for an MCP-only executor; (c) the JSON and manifest REST endpoints only "
        "support 3 tasks by their response model (a known platform limitation) — for other tasks "
        "they are reported as unavailable, never as usable links; (d) artifacts live in the "
        "platform's existing controlled storage and expire (default 24h); expired or missing ids "
        "error out explicitly and are never silently re-exported under the old id. "
        "evidence_gate_candidate_count (v2 gate candidates) is reported SEPARATELY from "
        "exported_rows (actual task training rows) and must not be conflated. "
        "source_snapshot_fingerprint is a source-PDF/review-scope fingerprint, NOT a data hash. "
        "ready_only is fixed to true — there is no option to bypass the evidence gate. "
        "Unknown task returns a clear error. SIDE EFFECT: writes artifact files to controlled "
        "storage (no DB writes, no audit rows, no share links). Requires capability: export_data."
    )
    read_description = (
        "Read a previously exported ML dataset artifact in BOUNDED blocks. Call "
        "export_paper_ml_dataset first; it returns artifact_id + expires_at. Then page through the "
        "artifact: offsets are UTF-8 BYTE offsets, each block is at most 65536 bytes (hard cap, not "
        "raisable by any parameter), and the chunk boundary is aligned to a UTF-8 character "
        "boundary so concatenating the returned `content` values in next_offset order reproduces "
        "the file losslessly. Response includes next_offset, has_more, chunk_sha256 (identical on "
        "repeat reads) and content_sha256 (the whole artifact's sha256, recorded at export time). "
        "Reading does NOT re-run the v3 builder and does not modify the artifact. The artifact is "
        "bound to the exporting MCP identity: reads re-check the export capability + identity, and "
        "an artifact_id is not a bearer credential (a different identity is refused). Unknown, "
        "incomplete, expired or changed artifacts raise explicit errors and are never re-exported "
        "under the same id. Read-only, no side effects. Requires capability: export_data."
    )
    list_description = (
        "List the tabular ML tasks actually registered on the platform, with the fields each task "
        "requires (required_features / optional_features), allowed target properties, allowed "
        "units, split group keys, task status, profile version, and a per-task download matrix "
        "(CSV supported for all tasks; JSON/manifest REST endpoints limited to 3 tasks by their "
        "response model — a known platform limitation). The matrix distinguishes REST availability "
        "from artifact-read availability and from inline availability. Read-only, in-memory; does "
        "not touch the database. Requires capability: read_papers."
    )

    read_only_annotations = None
    writing_annotations = None
    if ToolAnnotations is not None:
        read_only_annotations = ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
        # 导出工具会写产物文件 ⇒ 不再声明为只读；每次调用产生**新的** artifact ⇒ 非幂等。
        writing_annotations = ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )

    @server.tool(
        name="export_paper_ml_dataset",
        title="单篇任务级机器学习数据导出",
        description=export_description,
        annotations=writing_annotations,
    )
    def export_paper_ml_dataset(
        paper_id: str,
        task: str,
        ready_only: bool = True,
        include_payload: Literal["none", "csv", "manifest"] = "none",
        artifact_ttl_hours: int = ARTIFACT_TTL_HOURS,
    ) -> dict[str, Any]:
        require_mcp_capability("export_data")
        require_mcp_exports_enabled()
        try:
            pid = UUID(str(paper_id or "").strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid paper_id: expected a UUID string") from exc
        settings = get_settings()
        with session_scope(settings.database_url) as session:
            _enforce_postgres_read_only_transaction(session)
            return build_paper_ml_export(
                session,
                paper_id=pid,
                task=str(task or "").strip(),
                ready_only=ready_only,
                include_payload=include_payload,
                artifact_ttl_hours=artifact_ttl_hours,
                settings=settings,
                owner=get_mcp_auth(),
            )

    @server.tool(
        name="read_ml_export_artifact",
        title="分块读取已导出的 ML 数据集产物",
        description=read_description,
        annotations=read_only_annotations,
    )
    def read_ml_export_artifact(
        paper_id: str,
        artifact_id: str,
        fmt: Literal["csv", "json", "manifest"],
        offset: int = 0,
        limit_bytes: int = READ_BLOCK_DEFAULT_BYTES,
    ) -> dict[str, Any]:
        # 每次读取都重新校验权限与开关（artifact_id 不是访问凭据）。
        require_mcp_capability("export_data")
        require_mcp_exports_enabled()
        try:
            pid = UUID(str(paper_id or "").strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid paper_id: expected a UUID string") from exc
        settings = get_settings()
        return read_artifact_chunk(
            settings,
            paper_id=pid,
            artifact_id=str(artifact_id or "").strip(),
            fmt=fmt,
            offset=offset,
            limit_bytes=limit_bytes,
            owner=get_mcp_auth(),
        )

    @server.tool(
        name="list_ml_export_tasks",
        title="列出已注册的任务级导出任务",
        description=list_description,
        annotations=read_only_annotations,
    )
    def list_ml_export_tasks() -> dict[str, Any]:
        require_mcp_capability("read_papers")
        tasks = []
        for profile in list_tabular_task_profiles():
            json_ok = _task_supports_json_endpoint(profile.key)
            tasks.append(
                {
                    "task": profile.key,
                    "version": profile.version,
                    "status": profile.status,
                    "reaction_type": profile.reaction_type,
                    "allowed_target_properties": sorted(profile.allowed_target_properties),
                    "allowed_units": sorted(profile.allowed_units),
                    "required_features": list(profile.required_features),
                    "optional_features": list(profile.optional_features),
                    "split_group_keys": list(profile.split_group_keys),
                    "csv_download_supported": True,
                    "json_manifest_download_supported": json_ok,
                    "artifact_read_supported": True,
                    "inline_supported": True,
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "as_of": _utc_now_iso(),
            "task_profile_version": TASK_PROFILE_VERSION,
            "task_count": len(tasks),
            "tasks": tasks,
            "download_matrix": {
                "csv": {"rest_supported_tasks": "all_registered_tasks", "artifact": True, "inline": True},
                "json": {
                    "rest_supported_tasks": list(_V3_JSON_TASK_LITERAL),
                    "artifact": True,
                    "inline": False,
                },
                "manifest": {
                    "rest_supported_tasks": list(_V3_JSON_TASK_LITERAL),
                    "artifact": True,
                    "inline": True,
                },
                "rest_auth": (
                    "生产 REST 出口在 Owner 网关 HTTP Basic 之后（auth_basic "
                    "\"Literature AI Owner\"）；MCP-only 执行 AI 请用 artifact 引用 + "
                    "read_ml_export_artifact 分块读取（不需要 Owner Basic）。"
                ),
                "block_max_bytes": READ_BLOCK_MAX_BYTES,
                "offset_unit": OFFSET_UNIT,
                "note": (
                    "JSON/manifest REST 端点仅支持 3 个 task（响应模型 Literal 限制，已知平台缺陷）；"
                    "CSV REST 端点支持全部 task。artifact 读取对全部 task、全部格式可用。"
                ),
            },
        }

    return {
        "export_paper_ml_dataset": "registered",
        "read_ml_export_artifact": "registered",
        "list_ml_export_tasks": "registered",
    }
