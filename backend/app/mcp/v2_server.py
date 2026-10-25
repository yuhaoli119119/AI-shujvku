"""MCP v2: external AI reads and writes the current rebuild data model.

No tool invokes a model, creates a conversation, or advances another paper.
Authentication and database transactions are shared with the application.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent, ToolAnnotations
from PIL import Image
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Paper
from app.db.session import session_scope
from app.mcp.auth import require_mcp_capability
from app.mcp.context import get_mcp_auth
from app.mcp.v2_contracts import (
    BeginWork, Catalog, CropFigure, FinishWork, ListRows, PaperRequest,
    PreviewRows, Quality, ReadSourceText, ReportWork, SaveAssets, SaveDataRows,
    SaveReading, SaveSections, ViewFigureImage, ViewSourcePage, Workspace,
    WriteReceipt,
)
from app.services import external_paper_work as external_work
from app.services.rebuild_workflow_service import RebuildWorkflowError

mcp_server = FastMCP(
    'Literature AI',
    instructions=(
        'Read the selected paper workspace and source files before working. '
        'The external AI itself reads and interprets the sources. Select one paper '
        'and begin_paper_work before saving. Preserve existing outputs and unknown '
        'values; every scientific claim needs its source. Use the returned '
        'work_revision as expected_revision for the next write. Reuse request_id '
        'after an uncertain response. Read back outputs before finish_paper_work. '
        'Completed work does not imply independent scientific acceptance. '
        'No tool dispatches or resumes native AI tasks.'
    ),
    streamable_http_path='/', stateless_http=True, json_response=True,
    # Authentication is enforced by the enclosing FastAPI middleware; forwarded
    # hostnames are intentionally accepted so the same app works behind relays.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _tool(*, read: bool, image: bool = False):
    def decorate(fn):
        @wraps(fn)
        async def thread_isolated(*args, **kwargs):
            # FastMCP calls synchronous tools directly on its asyncio event loop.
            # All current tools perform synchronous PostgreSQL/file I/O, so make
            # the transport execute each tool in a context-preserving worker.
            return await asyncio.to_thread(fn, *args, **kwargs)

        mcp_server.tool(
            annotations=ToolAnnotations(readOnlyHint=read, destructiveHint=False,
                                        idempotentHint=read, openWorldHint=False),
            structured_output=False if image else None,
        )(thread_isolated)
        # Keep the sync function callable by in-process integrations/tests while
        # the FastMCP registry receives the worker-isolated coroutine wrapper.
        return fn
    return decorate


def _actor(capability):
    auth = get_mcp_auth()
    if auth is None or not auth.identity_verified or not auth.source_identity:
        raise PermissionError('unauthenticated')
    require_mcp_capability(capability)
    return auth.source_identity


@contextmanager
def _database(*, write=False):
    actor = _actor('write_rebuild' if write else 'read_papers')
    try:
        with session_scope(get_settings().database_url) as session:
            if not write:
                session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            else:
                session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            yield session, actor
    except OperationalError as exc:
        from app.services.paper_db_phase import BUSY, is_lock_conflict
        if is_lock_conflict(exc):
            raise RebuildWorkflowError(BUSY) from exc
        raise


def _snapshot(session, paper_id, expected=None):
    from app.services.rebuild_acceptance import current_snapshot
    from app.services.rebuild_workflow_service import _required_paper
    _required_paper(session, paper_id)
    snapshot = current_snapshot(session, paper_id)[0]
    if expected is not None and snapshot['revision'] != expected:
        raise RebuildWorkflowError('revision_conflict')
    return snapshot


def _source(session, paper_id, file_id, source_sha256):
    from app.services.rebuild_workflow_service import get_file
    from app.services.rebuild_file_preview import _checked_source
    record = get_file(session, file_id)
    if record.paper_id != paper_id:
        raise RebuildWorkflowError('file_not_in_scope')
    if record.sha256 != source_sha256:
        raise RebuildWorkflowError('source_changed')
    # _checked_source verifies the physical file hash, not just metadata.
    _, kind = _checked_source(record, get_settings())
    return record, kind


def _image_content(path: Path, identity: dict) -> list[TextContent | ImageContent]:
    body = path.read_bytes()
    if len(body) > 16 * 1024 * 1024:
        raise RebuildWorkflowError('image_too_large_use_lower_dpi')
    with Image.open(io.BytesIO(body)) as picture:
        mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}.get(picture.format)
        if mime is None:
            raise RebuildWorkflowError('unsupported_image_format')
        picture.verify()
    metadata = {**identity, 'image_sha256': hashlib.sha256(body).hexdigest()}
    return [TextContent(type='text', text=json.dumps(metadata, ensure_ascii=False, default=str)),
            ImageContent(type='image', data=base64.b64encode(body).decode('ascii'), mimeType=mime)]


def _mutate(request, operation, fn):
    with _database(write=True) as (session, actor):
        def apply():
            from app.db.models import WorkflowJob
            from app.services.rebuild_workflow_service import get_asset
            job = session.get(WorkflowJob, request.work_id)
            allowed = set(job.payload['file_ids'])

            def references(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key == 'asset_id' and item is not None:
                            asset = get_asset(session, UUID(str(item)))
                            if asset.paper_id != request.paper_id or str(asset.file_id) not in allowed:
                                raise RebuildWorkflowError('asset_not_in_work_scope')
                        references(item)
                elif isinstance(value, list):
                    for item in value:
                        references(item)

            references(request.model_dump(mode='json'))
            if operation.startswith('assets.'):
                from app.db.models import RebuildVisualAsset
                candidates = request.assets if isinstance(request, SaveAssets) else [request.crop]
                for item in candidates:
                    existing = session.scalar(select(RebuildVisualAsset).where(
                        RebuildVisualAsset.paper_id == request.paper_id,
                        RebuildVisualAsset.asset_key == item.asset_key))
                    if existing is not None and str(existing.file_id) not in allowed:
                        raise RebuildWorkflowError('asset_not_in_work_scope')
            return fn(session, actor)

        return external_work.mutate(
            session, request.paper_id, request.work_id, actor,
            request.request_id, request.expected_revision, operation,
            request, apply,
        )


def _asset_payload(session, paper_id, item):
    """Verify original bytes before adapting the legacy DOCX-only hash field."""
    record, kind = _source(session, paper_id, item.file_id, item.source_sha256)
    data = item.model_dump(mode='json', exclude_unset=True)
    if kind == 'pdf':
        from app.services.rebuild_file_preview import reading_identity
        if data.get('page_basis') is not None or data.get('ooxml_locator') is not None:
            raise RebuildWorkflowError('pdf_has_no_ooxml_locator')
        _, identity = reading_identity(record, get_settings())
        provenance = dict(data.get('provenance') or {})
        if provenance.get('page_identity') is not None and provenance['page_identity'] != identity:
            raise RebuildWorkflowError('source_page_identity_mismatch')
        provenance['page_identity'] = identity
        data['provenance'] = provenance
        # The shared importer reserves these top-level fields for OOXML.
        # Verified PDF identity belongs in provenance, matching the crop path.
        for key in ('source_sha256', 'page_basis', 'ooxml_locator'):
            data.pop(key, None)
    elif kind != 'docx':
        raise RebuildWorkflowError('unsupported_source_format')
    return data


@_tool(read=True)
def list_libraries_and_papers(request: Catalog) -> dict:
    """Find main papers by library/title/DOI, returning stable IDs and pagination."""
    from app.services.paper_codes import main_paper_clause
    with _database() as (session, _):
        statement = select(Paper).where(main_paper_clause())
        libraries = list(session.scalars(select(Paper.library_name).where(main_paper_clause()).distinct().order_by(Paper.library_name)))
        if request.library_name is not None:
            statement = statement.where(Paper.library_name == request.library_name)
        if request.query:
            query = '%' + request.query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            statement = statement.where(or_(Paper.title.ilike(query, escape='\\'), Paper.doi.ilike(query, escape='\\'), Paper.paper_code.ilike(query, escape='\\')))
        total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        records = session.scalars(statement.order_by(Paper.id).offset(request.offset).limit(request.limit))
        items = [{'paper_id': str(p.id), 'paper_code': p.paper_code, 'title': p.title,
                  'doi': p.doi, 'library_name': p.library_name} for p in records]
        return {'libraries': libraries, 'items': items, 'total': total, 'offset': request.offset,
                'limit': request.limit, 'has_more': request.offset + len(items) < total,
                'next_offset': request.offset + len(items) if request.offset + len(items) < total else None}


@_tool(read=True)
def get_paper_workspace(request: Workspace) -> dict:
    """Read bounded current outputs, file lifecycle, write contracts and work revision."""
    from app.services.rebuild_work_package import compact_package, bounded_view
    with _database() as (session, actor):
        if request.view.startswith('delivery'):
            from app.services.paper_delivery import delivery_package
            return delivery_package(session, request.paper_id, _snapshot(session, request.paper_id), get_settings(),
                                    view=request.view.removeprefix('delivery_') if request.view != 'delivery' else 'summary',
                                    offset=request.offset, limit=request.limit, expected_revision=request.expected_revision)
        if request.offset and request.expected_revision is None:
            raise RebuildWorkflowError('revision_required')
        snapshot = _snapshot(session, request.paper_id, request.expected_revision)
        result = (compact_package(session, request.paper_id, snapshot) if request.view == 'compact' else
                  bounded_view(session, request.paper_id, view=request.view, snapshot=snapshot,
                               limit=request.limit, offset=request.offset))
        from app.services.paper_processing_status import processing_statuses
        from app.services.rebuild_queue import paper_busy
        busy = paper_busy(session, request.paper_id)
        return {**result, 'external_work': external_work.state(session, request.paper_id, actor),
                'processing_status': processing_statuses(session, [request.paper_id])[request.paper_id],
                'occupancy': {'blocked': busy is not None, 'work_id': busy.job_id if busy else None,
                              'status_as_recorded': busy.status if busy else None,
                              'native_execution': 'not_independently_verified_no_automatic_release'},
                'mcp_workflow': 'Read sources, begin one paper, save with work_revision, read back, finish; no scientific acceptance implied.'}


@_tool(read=True)
def list_paper_files(request: PaperRequest) -> dict:
    """Return active and historical main/SI files, SHA256 and file_set_revision."""
    from app.services.rebuild_file_lifecycle import file_set
    with _database() as (session, _):
        return file_set(session, request.paper_id)


@_tool(read=True)
def read_source_text(request: ReadSourceText) -> dict:
    """Read a hash-verified bounded PDF page chunk or original DOCX paragraphs/tables."""
    from app.services.rebuild_read_cache import paginated_text
    from app.services.ordinary_parsing import reading_text
    with _database() as (session, _):
        record, kind = _source(session, request.paper_id, request.file_id, request.source_sha256)
        if kind == 'docx':
            if request.reading_pdf_sha256:
                raise RebuildWorkflowError('docx_original_has_no_reading_pdf_hash')
            return reading_text(session, request.paper_id, request.file_id, get_settings(), request.offset, request.limit)
        if kind != 'pdf':
            raise RebuildWorkflowError('unsupported_source_format')
        return paginated_text(record, get_settings(), offset=request.offset, limit=request.limit,
                              expected_source=request.source_sha256, expected_reading=request.reading_pdf_sha256)


@_tool(read=True, image=True)
def view_source_page(request: ViewSourcePage) -> list[TextContent | ImageContent]:
    """Return the actual physical page as a native MCP image plus source identity."""
    from app.services.rebuild_read_cache import rendered_page
    with _database() as (session, _):
        record, kind = _source(session, request.paper_id, request.file_id, request.source_sha256)
        if kind != 'pdf':
            raise RebuildWorkflowError('unsupported_source_format')
        path, identity = rendered_page(record, get_settings(), page=request.page, dpi=request.dpi,
                                       expected_source=request.source_sha256, expected_reading=request.reading_pdf_sha256)
        return _image_content(path, {**identity, 'paper_id': str(request.paper_id),
                                    'file_id': str(request.file_id), 'physical_page': request.page})


@_tool(read=True, image=True)
def view_figure_image(request: ViewFigureImage) -> list[TextContent | ImageContent]:
    """View an existing figure belonging to this paper; filesystem paths are never accepted."""
    from app.services.rebuild_workflow_service import get_asset, resolve_asset_image_path
    with _database() as (session, _):
        asset = get_asset(session, request.asset_id)
        if asset.paper_id != request.paper_id:
            raise RebuildWorkflowError('asset_not_in_paper')
        path = resolve_asset_image_path(asset, get_settings())
        if path is None:
            raise RebuildWorkflowError('asset_image_not_found')
        return _image_content(path, {'paper_id': str(request.paper_id), 'asset_id': str(asset.id),
                                    'file_id': str(asset.file_id) if asset.file_id else None,
                                    'asset_key': asset.asset_key, 'provenance': asset.provenance})


@_tool(read=True)
def list_data_rows(request: ListRows) -> dict:
    """Read paginated current data, values, provenance, missing fields and conflicts."""
    from app.services.rebuild_workflow_service import list_rows
    with _database() as (session, _):
        if request.offset and request.expected_revision is None:
            raise RebuildWorkflowError('revision_required')
        snapshot = _snapshot(session, request.paper_id, request.expected_revision)
        return {**list_rows(session, paper_id=request.paper_id, limit=request.limit, offset=request.offset),
                'output_revision': snapshot['revision']}


@_tool(read=True)
def get_reading_context(request: PaperRequest) -> dict:
    """Read abstract translation source, guide contract and input revision; read full texts in chunks."""
    from app.services.reading_assistance import input_context
    with _database() as (session, _):
        context = input_context(session, request.paper_id, get_settings())
        # Avoid silently dumping 280k characters into a web model context. The
        # original input revision still binds the full server-verified context.
        context['documents'] = [{key: value for key, value in item.items() if key not in ('pages', 'items')}
                                for item in context['documents']]
        context['source_text_tool'] = 'read_source_text'
        return context


@_tool(read=True)
def get_quality_report(request: Quality) -> dict:
    """Read bounded quality issues; this report does not grant scientific acceptance."""
    from app.services.rebuild_quality import quality_report
    with _database() as (session, _):
        snapshot = _snapshot(session, request.paper_id, request.expected_revision)
        if request.asset_offset and request.include_asset_mechanics and request.expected_revision is None:
            raise RebuildWorkflowError('revision_required')
        result = quality_report(session, request.paper_id, get_settings(), snapshot,
                                offset=request.offset, limit=request.limit,
                                include_asset_mechanics=request.include_asset_mechanics,
                                asset_offset=request.asset_offset, asset_limit=request.asset_limit)
        if request.offset and result['coverage_plan']['plan_revision'] is not None and request.expected_coverage_revision is None:
            raise RebuildWorkflowError('coverage_read_revision_required')
        if request.expected_coverage_revision and result['quality_revision'] != request.expected_coverage_revision:
            raise RebuildWorkflowError('revision_conflict')
        return result


@_tool(read=True)
def get_data_templates() -> dict:
    """Return current reaction templates and scientific field contracts."""
    _actor('read_papers')
    from app.services.rebuild_workflow_service import reaction_templates
    return reaction_templates()


@_tool(read=False)
def begin_paper_work(request: BeginWork) -> dict:
    """Acquire one manually selected paper and file scope. Starts no model or task dispatch."""
    with _database(write=True) as (session, actor):
        return external_work.begin(session, request.paper_id, actor, request.request_id,
                                   [str(value) for value in request.file_ids], request.file_set_revision)


@_tool(read=False)
def save_figure_assets(request: SaveAssets) -> dict:
    """Save source-bound figures, table content and explanations with audit and revision checks."""
    from app.api.rebuild import _import_assets
    from app.schemas.rebuild import RebuildAssetsImportRequest
    def save(session, actor):
        payload = RebuildAssetsImportRequest(request_id=request.request_id, task_id=request.work_id,
            assets=[_asset_payload(session, request.paper_id, item) for item in request.assets])
        return _import_assets(session, request.paper_id, payload, get_settings())
    return _mutate(request, 'assets.import', save)


@_tool(read=False)
def crop_figure(request: CropFigure) -> dict:
    """Crop real source PDF regions into an immutable figure; preserve old images."""
    from app.api.rebuild import _crop_asset
    from app.schemas.rebuild import RebuildVisualAssetCropRequest
    def crop(session, actor):
        payload = RebuildVisualAssetCropRequest.model_validate(
            _asset_payload(session, request.paper_id, request.crop))
        return _crop_asset(session, request.paper_id, payload, get_settings())
    return _mutate(request, 'assets.crop', crop)


@_tool(read=False)
def preview_data_rows(request: PreviewRows) -> dict:
    """Validate proposed rows/cells in a separate transaction that is always rolled back."""
    from app.api.rebuild import _import_rows
    from app.schemas.rebuild import RebuildRowsImportRequest
    from app.services.rebuild_import_recovery import simulated_result
    _actor('write_rebuild')
    with session_scope(get_settings().database_url) as outer:
        bind = outer.get_bind()
        with Session(bind=getattr(bind, 'engine', bind), autoflush=False) as simulation:
            try:
                simulation.begin()
                return simulated_result(_import_rows(simulation, RebuildRowsImportRequest(
                    paper_id=request.paper_id, rows=request.rows)))
            finally:
                simulation.rollback()


@_tool(read=False)
def save_data_rows(request: SaveDataRows) -> dict:
    """Save source-bound rows with original precision, per-cell receipts and retained conflicts."""
    from app.api.rebuild import _import_rows
    from app.schemas.rebuild import RebuildRowsImportRequest
    payload = RebuildRowsImportRequest(paper_id=request.paper_id, request_id=request.request_id,
                                      task_id=request.work_id, rows=request.rows)
    return _mutate(request, 'rows.import', lambda session, actor: _import_rows(session, payload))


@_tool(read=False)
def save_sections(request: SaveSections) -> dict:
    """Optionally save source-bound section explanations; completeness is not an ordinary parsing gate."""
    from app.services.rebuild_sections import import_sections
    from app.schemas.rebuild import RebuildSectionsImportRequest
    payload = RebuildSectionsImportRequest(request_id=request.request_id, sections=request.sections)
    return _mutate(request, 'sections.import', lambda session, actor:
                   import_sections(session, request.paper_id, payload, get_settings(), actor=actor))


@_tool(read=False)
def save_reading_assistance(request: SaveReading) -> dict:
    """Save Chinese abstract translation and guide using this owned external work identity."""
    from app.services.reading_assistance import save
    return _mutate(request, 'reading.save', lambda session, actor:
                   save(session, request.paper_id, request.reading, get_settings(), actor))


@_tool(read=False)
def report_paper_work(request: ReportWork) -> dict:
    """Persist file/stage progress and concrete gaps; disconnecting does not finish this work."""
    with _database(write=True) as (session, actor):
        return external_work.report(session, request.paper_id, request.work_id, actor,
            request.request_id, request.expected_revision, request.status, request.message,
            [item.model_dump(mode='json') for item in request.file_stages], request.missing_items)


@_tool(read=False)
def finish_paper_work(request: FinishWork) -> dict:
    """End this work after readback as completed, partial or failed; never scientific accepted."""
    with _database(write=True) as (session, actor):
        return external_work.finish(session, request.paper_id, request.work_id, actor,
            request.request_id, request.expected_revision, request.status, request.message,
            [item.model_dump(mode='json') for item in request.file_stages], request.missing_items)


@_tool(read=True)
def get_write_receipt(request: WriteReceipt) -> dict:
    """Read this authenticated actor's stored receipt for the specified paper/work/request."""
    with _database() as (session, actor):
        return external_work.receipt(session, request.paper_id, request.work_id, actor, request.request_id)
