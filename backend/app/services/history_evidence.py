"""Read a fixed operator-curated evidence directory, never a client filesystem path."""
import hashlib
import json
import os
import stat
import re
from uuid import UUID
from pathlib import Path, PurePosixPath
from pydantic import ValidationError
from app.schemas.history_reconciliation import EvidenceCatalog, HistoricalEvidence
from app.services.extraction_coverage import _digest
from app.services.rebuild_workflow_service import RebuildWorkflowError

MAX_FILE = 2 * 1024 * 1024


def _components(value):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or chr(92) in value or chr(0) in value or
            path.as_posix() != value or any(p in {'', '.', '..'} or p.startswith('.') for p in path.parts)):
        raise RebuildWorkflowError('history_evidence_path_invalid')
    if path.suffix.lower() not in {'.json', '.md', '.txt'}:
        raise RebuildWorkflowError('history_evidence_file_type_invalid')
    return path.parts


def read_file(root, relative, limit=MAX_FILE):
    parts = _components(relative)
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise RebuildWorkflowError('history_evidence_root_invalid')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *parts[:-1]):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise RebuildWorkflowError('history_evidence_file_invalid')
            chunks = []; size = 0
            while True:
                chunk = os.read(file_fd, min(65536, limit + 1 - size))
                if not chunk:break
                chunks.append(chunk); size += len(chunk)
                if size > limit:raise RebuildWorkflowError('history_evidence_file_too_large')
            after = os.fstat(file_fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or size != after.st_size:
                raise RebuildWorkflowError('history_evidence_changed_during_read')
            return b''.join(chunks)
        finally:
            os.close(file_fd)
    except OSError as exc:
        raise RebuildWorkflowError('history_evidence_unavailable_or_symlink') from exc
    finally:
        os.close(descriptor)


def _strict_json(content):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate_json_key')
            result[key]=value
        return result
    def invalid_constant(value):raise ValueError('nonfinite_json_constant')
    return json.loads(content,object_pairs_hook=unique,parse_constant=invalid_constant)


def evidence_root(settings):
    return Path(settings.storage_root).parent / 'history_evidence'


def catalog(settings):
    root = evidence_root(settings)
    try:
        content = read_file(root, 'catalog.json', limit=256 * 1024)
        parsed = EvidenceCatalog.model_validate(_strict_json(content))
        return {'status':'available', 'revision':hashlib.sha256(content).hexdigest(), 'entries':parsed.entries}
    except (RebuildWorkflowError, ValidationError, ValueError):
        return {'status':'catalog_unavailable_or_invalid', 'revision':None, 'entries':[]}


# Explicit top-level aliases and two known paper identity paths only. Do not
# traverse checks/results/subitems: a passing subcheck is not an article verdict.
VERSION_FIELDS = ('observed_output_revision', 'output_revision', 'revision')
VERDICT_FIELDS = ('scientific_verdict', 'extraction_verdict', 'verdict', 'decision')
SCIENTIFIC_STATUS_SCHEMAS = {'scientific_review_v1', 'article_acceptance_v1', 'independent_scientific_review'}
VERDICT_FAMILIES = {
    'accepted':'accepted', 'pass':'accepted', 'passed':'accepted',
    'accepted_partial':'accepted_partial', 'partial':'partial',
    'pass_with_limitations':'with_limitations', 'accepted_with_limitations':'with_limitations',
    'needs_fix':'needs_fix', 'needs_review':'needs_review', 'needs_check':'needs_review',
    'unverified':'unverified', 'failed':'failed', 'rejected':'rejected',
}


def _verdict_family(value):
    return VERDICT_FAMILIES.get(value.strip().lower()) if isinstance(value,str) else None


def original_semantics(body, evidence, paper_id):
    result = {'semantic_basis':'operator_declaration_only', 'original_contract':None,
              'original_identity_fields':[], 'original_version_fields':[], 'original_verdict_fields':[],
              'original_verdict':None, 'original_output_revision':None,
              'semantic_limitations':['Original report semantics were not established by a supported report-level structure.']}
    if not isinstance(body,dict):return result
    identities = [('paper_id',body.get('paper_id'))] if body.get('paper_id') is not None else []
    paper = body.get('paper')
    if isinstance(paper,dict):
        identities += [('paper.'+key,paper[key]) for key in ('paper_id','id') if paper.get(key) is not None]
    for name,value in identities:
        try:parsed = str(UUID(value)) if isinstance(value,str) else None
        except (ValueError,TypeError,AttributeError):parsed = None
        if parsed is None:raise RebuildWorkflowError('history_original_report_identity_invalid')
        if parsed != str(paper_id):raise RebuildWorkflowError('history_original_report_paper_mismatch')
    result['original_identity_fields'] = [name for name,_ in identities]
    versions = [(name,body[name]) for name in VERSION_FIELDS if name in body]
    nonempty = [(name,value) for name,value in versions if value is not None]
    if any(not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value) for _,value in nonempty):
        raise RebuildWorkflowError('history_original_report_version_invalid')
    if len({value for _,value in nonempty}) > 1:
        raise RebuildWorkflowError('history_original_report_version_conflict')
    if nonempty and nonempty[0][1] != evidence.observed_output_revision:
        raise RebuildWorkflowError('history_original_report_version_mismatch')
    result['original_version_fields'] = [name for name,_ in versions]
    conclusions = [(name,body[name]) for name in VERDICT_FIELDS if body.get(name) is not None]
    status_family = _verdict_family(body.get('status'))
    marked_status = any(body.get(key) in SCIENTIFIC_STATUS_SCHEMAS for key in ('format','record_type','report_type')
                        if isinstance(body.get(key),str))
    if marked_status and body.get('status') is not None:
        conclusions.append(('status',body['status']))
    known = [(name,value,_verdict_family(value)) for name,value in conclusions if _verdict_family(value)]
    # An unmarked status is a conservative constraint only, never a scientific
    # verdict source. Task states such as completed/running are not verdicts.
    families = {family for _,_,family in known}
    if status_family:families.add(status_family)
    declared = _verdict_family(evidence.historical_verdict)
    if len(families)>1 or families and declared not in families:
        raise RebuildWorkflowError('history_original_report_verdict_mismatch')
    result['original_verdict_fields'] = [name for name,_ in conclusions]
    if not identities or not conclusions or len(known)!=len(conclusions):return result
    result.update(semantic_basis='supported_original_report_fields',original_contract='report_level_aliases_v1',
        original_verdict=known[0][1],
        original_output_revision=nonempty[0][1] if nonempty and all(value is not None for _,value in versions) else None,
        semantic_limitations=['Report-level fields and bytes were checked; scientific meaning was not independently re-reviewed.'])
    return result


def load_entry(settings, entry, paper_id, snapshot):
    result = {'report_id':entry.report_id, 'paper_id':str(entry.paper_id), 'report_sha256':entry.evidence.sha256,
              'current_acceptance':'not_granted', 'scientific_acceptance':'not_implied'}
    try:
        if str(entry.paper_id) != str(paper_id):raise RebuildWorkflowError('history_report_paper_mismatch')
        root = evidence_root(settings)
        content = read_file(root, entry.evidence.path)
        digest = hashlib.sha256(content).hexdigest()
        if digest != entry.evidence.sha256:raise RebuildWorkflowError('history_report_hash_mismatch')
        evidence = HistoricalEvidence.model_validate(_strict_json(content))
        if str(evidence.paper_id) != str(paper_id):raise RebuildWorkflowError('history_report_paper_mismatch')
        original = read_file(root, evidence.original.path)
        if hashlib.sha256(original).hexdigest() != evidence.original.sha256:
            raise RebuildWorkflowError('history_original_report_hash_mismatch')
        body = _strict_json(original) if Path(evidence.original.path).suffix.lower()=='.json' else None
        semantics = original_semantics(body,evidence,paper_id)
        physical = snapshot.get('file_reading_identity', {})
        scope_status = 'unknown'; pages = {}; scope_issues = set()
        if evidence.source_scope:
            scope_status = 'full_declared'
            for scope in evidence.source_scope:
                actual = physical.get(str(scope.file_id))
                if actual is None:raise RebuildWorkflowError('history_scope_file_not_in_paper')
                if not scope.locator.strip() or len(set(scope.pages)) != len(scope.pages):
                    raise RebuildWorkflowError('history_scope_invalid')
                if actual.get('status') != 'verified':scope_issues.add('unknown'); continue
                if any(getattr(scope,k) != actual.get(k) for k in ('source_sha256','reading_pdf_sha256','role')):
                    scope_issues.add('stale'); continue
                if max(scope.pages) > actual['physical_pages']:raise RebuildWorkflowError('history_scope_pages_invalid')
                pages.setdefault(str(scope.file_id), set()).update(scope.pages)
            scope_status = 'stale' if 'stale' in scope_issues else 'unknown' if 'unknown' in scope_issues else 'full_declared'
            if scope_status == 'full_declared' and (not any(f.get('role')=='main' for f in physical.values()) or snapshot.get('unregistered_files') or any(
                    len(pages.get(fid,())) != actual.get('physical_pages') for fid,actual in physical.items())):
                scope_status = 'partial_declared'
        original_revision = semantics['original_output_revision']
        comparison = ('version_unknown' if original_revision is None else
                      'stale' if original_revision != snapshot['revision'] or scope_status == 'stale' else
                      'current_version_match_only' if scope_status in {'full_declared','partial_declared'} else 'current_version_scope_unknown')
        result.update(file_integrity='verified_hashes_only', historical_verdict_as_reported=semantics['original_verdict'],
            historical_output_revision=original_revision, operator_declared_verdict=evidence.historical_verdict,
            operator_declared_output_revision=evidence.observed_output_revision, **semantics, comparison=comparison, scope_status=scope_status,
            source_scope=[s.model_dump(mode='json') for s in evidence.source_scope], limitations=evidence.limitations,
            original_sha256=evidence.original.sha256, original_name=Path(evidence.original.path).name,
            attempt_links=[link.model_dump(mode='json') for link in evidence.attempt_links],
            identity_basis='supported_original_identity_fields' if semantics['original_identity_fields'] else 'operator_declared_only',
            scope_basis='operator_declared_scope_current_file_identity_checked_only',
            scientific_review='historical_claim_not_independently_reverified', usable_for_index=True)
    except (RebuildWorkflowError, ValidationError, ValueError, UnicodeError) as exc:
        result.update(usable_for_index=False, file_integrity='unverified', comparison='unknown', scope_status='unknown',
                      error=str(exc) if isinstance(exc,RebuildWorkflowError) else 'history_report_schema_invalid')
    result['evidence_revision'] = _digest(result)
    return result
