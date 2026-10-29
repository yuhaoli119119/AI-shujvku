"""Authenticated, paper-scoped dataset records and content-addressed files."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.dataset_models import DatasetFile, DatasetSource, DatasetValueSource
from app.db.models import Paper
from app.db.session import get_db_session, session_scope
from app.security.session_auth import SessionState, csrf_token_valid, origin_allowed, require_session
from app.services.paper_db_phase import is_lock_conflict

router = APIRouter(prefix='/api/papers/{paper_id}/datasets', tags=['datasets'])
MAX_FILE_SIZE = 512 * 1024 * 1024


async def _session(request: Request):
    return await require_session(request, settings=get_settings())


class DatasetCreate(BaseModel):
    dataset_key: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=2000)
    metadata: dict = Field(default_factory=dict)


def _write_auth(request: Request, auth: SessionState):
    if not origin_allowed(request, get_settings()) or not csrf_token_valid(request, auth):
        raise HTTPException(403, 'CSRF validation failed')


def _database_error(exc: OperationalError):
    if is_lock_conflict(exc):
        raise HTTPException(409, 'dataset_lock_busy_retry_same_request') from exc
    raise exc


def _file_json(f: DatasetFile):
    return {'id': str(f.id), 'filename': f.filename, 'mime_type': f.mime_type,
            'size_bytes': f.size_bytes, 'sha256': f.sha256, 'metadata': f.metadata_}


def _dataset_json(d: DatasetSource, files):
    return {'id': str(d.id), 'paper_id': str(d.paper_id), 'dataset_key': d.dataset_key,
            'version': d.version, 'title': d.title, 'metadata': d.metadata_,
            'revision': d.revision, 'files': [_file_json(f) for f in files]}


def _get_dataset(session: Session, paper_id: UUID, dataset_id: UUID):
    d = session.scalar(select(DatasetSource).where(DatasetSource.id == dataset_id, DatasetSource.paper_id == paper_id))
    if d is None:
        raise HTTPException(404, 'dataset not found')
    return d


@router.get('')
def list_datasets(paper_id: UUID, _auth: SessionState = Depends(_session), session: Session = Depends(get_db_session)):
    if session.get(Paper, paper_id) is None:
        raise HTTPException(404, 'paper not found')
    datasets = session.scalars(select(DatasetSource).where(DatasetSource.paper_id == paper_id).order_by(DatasetSource.created_at)).all()
    files = session.scalars(select(DatasetFile).where(DatasetFile.paper_id == paper_id).order_by(DatasetFile.filename)).all()
    return [_dataset_json(d, [f for f in files if f.dataset_id == d.id]) for d in datasets]


@router.post('', status_code=201)
def register_dataset(paper_id: UUID, body: DatasetCreate, request: Request,
                     auth: SessionState = Depends(_session), session: Session = Depends(get_db_session)):
    _write_auth(request, auth)
    try:
        session.execute(text("SET LOCAL lock_timeout = '3s'"))
        if session.get(Paper, paper_id) is None:
            raise HTTPException(404, 'paper not found')
        identity = (DatasetSource.paper_id == paper_id, DatasetSource.dataset_key == body.dataset_key, DatasetSource.version == body.version)
        existing = session.scalar(select(DatasetSource).where(*identity))
        if existing:
            if existing.title != body.title or existing.metadata_ != body.metadata:
                raise HTTPException(409, 'dataset identity already has different metadata')
            return _dataset_json(existing, [])
        record = DatasetSource(paper_id=paper_id, dataset_key=body.dataset_key, version=body.version, title=body.title, metadata_=body.metadata)
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            existing = session.scalar(select(DatasetSource).where(*identity))
            if existing is None or existing.title != body.title or existing.metadata_ != body.metadata:
                raise HTTPException(409, 'dataset identity conflict')
            record = existing
        return _dataset_json(record, [])
    except OperationalError as exc:
        session.rollback()
        _database_error(exc)


def _digest(path: Path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _safe_object_path(root: Path, paper_id: UUID, sha: str):
    # No caller-supplied paths or filenames are used in storage keys.
    base = (root / 'datasets').resolve()
    path = base / str(paper_id) / sha
    if not path.resolve().is_relative_to(base):
        raise HTTPException(400, 'invalid storage path')
    return path


def _check_dataset_before_upload(paper_id: UUID, dataset_id: UUID):
    try:
        with session_scope(get_settings().database_url) as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            _get_dataset(session, paper_id, dataset_id)
    except OperationalError as exc:
        _database_error(exc)


def _register_file_after_upload(paper_id: UUID, dataset_id: UUID, sha256: str, filename: str,
                                size_bytes: int, mime_type: str):
    def fetch(session):
        return session.scalar(select(DatasetFile).where(DatasetFile.dataset_id == dataset_id,
                                                       DatasetFile.paper_id == paper_id,
                                                       DatasetFile.sha256 == sha256))
    def validate(f):
        if f.filename != filename or f.size_bytes != size_bytes or f.mime_type != mime_type:
            raise HTTPException(409, 'file metadata conflict')
        return _file_json(f)
    try:
        with session_scope(get_settings().database_url) as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            _get_dataset(session, paper_id, dataset_id)
            f = fetch(session)
            if f:
                return validate(f)
            f = DatasetFile(dataset_id=dataset_id, paper_id=paper_id, filename=filename, mime_type=mime_type,
                            size_bytes=size_bytes, sha256=sha256, object_key=f'{paper_id}/{sha256}', metadata_={})
            session.add(f)
            session.flush()
            return _file_json(f)
    except IntegrityError:
        with session_scope(get_settings().database_url) as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            f = fetch(session)
            if f is None:
                raise HTTPException(409, 'file conflict')
            return validate(f)
    except OperationalError as exc:
        _database_error(exc)


@router.put('/{dataset_id}/files/{sha256}')
async def put_file(paper_id: UUID, dataset_id: UUID, sha256: str, request: Request,
                   filename: str, size_bytes: int, mime_type: str = 'application/octet-stream',
                   auth: SessionState = Depends(_session)):
    _write_auth(request, auth)
    if not re.fullmatch(r'[0-9a-f]{64}', sha256) or size_bytes < 0 or size_bytes > MAX_FILE_SIZE:
        raise HTTPException(400, 'invalid digest or size')
    if not filename or len(filename) > 255 or Path(filename).name != filename or any(c in filename for c in ('\\', '\x00', '\r', '\n')):
        raise HTTPException(400, 'invalid filename')
    await anyio.to_thread.run_sync(_check_dataset_before_upload, paper_id, dataset_id)
    root = get_settings().storage_root
    path = _safe_object_path(root, paper_id, sha256)
    await anyio.to_thread.run_sync(lambda: path.parent.mkdir(parents=True, exist_ok=True))
    fd, temp_name = await anyio.to_thread.run_sync(lambda: tempfile.mkstemp(prefix='.upload-', dir=path.parent))
    actual = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, 'wb') as out:
            async for chunk in request.stream():
                total += len(chunk)
                if total > size_bytes:
                    raise HTTPException(400, 'size mismatch')
                actual.update(chunk)
                await anyio.to_thread.run_sync(out.write, chunk)
            await anyio.to_thread.run_sync(out.flush)
            await anyio.to_thread.run_sync(os.fsync, out.fileno())
        if total != size_bytes or actual.hexdigest() != sha256:
            raise HTTPException(400, 'file size or hash mismatch')
        def publish():
            try:
                os.link(temp_name, path)
            except FileExistsError:
                if path.stat().st_size != size_bytes or _digest(path) != sha256:
                    raise HTTPException(409, 'stored object hash mismatch')
        await anyio.to_thread.run_sync(publish)
        return await anyio.to_thread.run_sync(_register_file_after_upload, paper_id, dataset_id, sha256,
                                             filename, size_bytes, mime_type)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@router.get('/{dataset_id}/files/{file_id}')
def download_file(paper_id: UUID, dataset_id: UUID, file_id: UUID, _auth: SessionState = Depends(_session),
                  session: Session = Depends(get_db_session)):
    dataset = _get_dataset(session, paper_id, dataset_id)
    f = session.scalar(select(DatasetFile).where(DatasetFile.id == file_id, DatasetFile.dataset_id == dataset_id, DatasetFile.paper_id == paper_id))
    if f is None:
        raise HTTPException(404, 'file not found')
    path = _safe_object_path(get_settings().storage_root, paper_id, f.sha256)
    if not path.is_file() or path.stat().st_size != f.size_bytes or _digest(path) != f.sha256:
        raise HTTPException(409, 'file integrity check failed')
    headers = {'X-Content-Type-Options': 'nosniff'}
    if (dataset.metadata_ or {}).get('access_restriction'):
        headers['X-Dataset-Access-Restriction'] = 'license-unconfirmed; see-dataset-metadata'
    return FileResponse(path, media_type=f.mime_type, filename=f.filename, headers=headers)


@router.get('/{dataset_id}/rows.csv')
def export_dataset_rows(paper_id: UUID, dataset_id: UUID, _auth: SessionState = Depends(_session),
                        session: Session = Depends(get_db_session)):
    """Export persisted dataset-sourced cells with their provenance, never other papers."""
    import csv
    import io
    import json
    from fastapi.responses import Response
    from app.db.models import RebuildDataRow, RebuildDataValue, RebuildValueSource
    from app.security.exports import require_exports_enabled

    require_exports_enabled()
    _get_dataset(session, paper_id, dataset_id)
    cells = session.execute(
        select(RebuildDataRow, RebuildDataValue, RebuildValueSource, DatasetFile)
        .join(RebuildDataValue, RebuildDataValue.row_id == RebuildDataRow.id)
        .join(RebuildValueSource, RebuildValueSource.value_id == RebuildDataValue.id)
        .join(DatasetValueSource, DatasetValueSource.source_id == RebuildValueSource.id)
        .join(DatasetFile, DatasetFile.id == DatasetValueSource.dataset_file_id)
        .where(RebuildDataRow.paper_id == paper_id, RebuildDataValue.paper_id == paper_id,
               RebuildValueSource.paper_id == paper_id, DatasetFile.paper_id == paper_id,
               DatasetFile.dataset_id == dataset_id, RebuildValueSource.source_kind == 'dataset')
        .order_by(RebuildDataRow.row_key, RebuildDataValue.field_name, RebuildValueSource.source_key)
    )
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(['row_key', 'material', 'field_name', 'raw_value', 'unit', 'value_type',
                     'filename', 'sha256', 'locator_json', 'estimate_basis'])
    for row, value, source, file in cells:
        writer.writerow([row.row_key, row.material, value.field_name, value.raw_value, value.unit,
                         value.value_type, file.filename, file.sha256,
                         json.dumps(source.dataset_binding.dataset_locator, ensure_ascii=False, sort_keys=True), source.estimate_basis])
    return Response(buffer.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="dataset-{dataset_id}.csv"',
                             'X-Content-Type-Options': 'nosniff'})
