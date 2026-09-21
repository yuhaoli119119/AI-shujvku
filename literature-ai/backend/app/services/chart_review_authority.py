"""Single authoritative reader for the whole-paper chart-review stage.

Background
----------
Three different sources have historically claimed to describe "is the chart
review of this paper finished?":

* ``EvidenceReviewBundleService.get_review_task(paper_id)`` -- the legacy
  chart-review task, which since the V2 rollout projects the V2 state machine
  onto legacy readers.
* ``ReviewTaskBuilder.build(paper_id)["status"]["chart_review_status"]`` -- the
  V2 state machine itself (``paper_review_v2_receipt`` receipts).
* ``papers.comprehensive_analysis.manual_review_progress`` -- a compatibility
  flag written by ``EvidenceReviewBundleService._mark_figures_review_completed``
  from **one** legacy code path only.

The compatibility flag is strictly weaker than the task: it is written by a
single legacy writer, so a paper finished through the V2 pipeline (or through
an ``external_analysis_run``-scoped legacy run) keeps an empty flag forever even
though its authoritative task says ``completed``.

This module exposes exactly one reading of the authoritative state so that the
review-center list, the paper detail gate, the DFT export gate and the
compatibility-flag writer cannot disagree:

* the scope is always the **whole paper** (``run_id=None``); a run-scoped
  ``external_analysis_run`` batch can therefore never be read as whole-paper
  completion;
* ``figures_completed`` is true only for ``completed`` / ``not_required`` *and*
  only while the completed snapshot fingerprint still equals the current one;
* ``manual_review_progress`` is never consulted here -- it is an output-only
  compatibility field.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import AuditLog, Paper
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService

#: Stage values that mean "this paper's chart review is finished".
FIGURE_COMPLETE_STAGES = frozenset({"completed", "not_required"})

#: Stage values that permit entering the DFT stage (V2's ``dft_gate_allowed``).
DFT_GATE_STAGES = frozenset({"completed", "completed_with_issues", "not_required"})

SCHEMA_VERSION = "chart_review_authority_v1"

#: Legacy audit actions that make up the whole-paper legacy task state.
LEGACY_TASK_ACTIONS = frozenset({"offline_evidence_review_applied", "offline_evidence_review_partial"})

V2_RECEIPT_ACTION = "paper_review_v2_receipt"


def _utc_iso() -> str:
    from app.db.models import utcnow

    return utcnow().isoformat()


@dataclass(frozen=True)
class ChartReviewAuthorityProjection:
    """Normalized, JSON-serializable view of the authoritative chart stage."""

    payload: dict[str, Any]

    @property
    def stage_status(self) -> str:
        return str(self.payload.get("stage_status") or "unknown")

    @property
    def figures_completed(self) -> bool:
        return bool(self.payload.get("figures_completed"))

    def as_payload(self) -> dict[str, Any]:
        return dict(self.payload)


class ChartReviewReadResult:
    """Return value of :meth:`ChartReviewAuthority.read_many`."""

    def __init__(self, stages: dict[str, dict[str, Any]], errors: dict[str, str] | None = None) -> None:
        self.stages = stages
        self.errors = errors or {}


class ChartReviewAuthority:
    """Read the authoritative whole-paper chart-review stage."""

    #: Hard cap so a list endpoint can never fan out over an unbounded row set.
    MAX_BATCH = 50

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self._service = EvidenceReviewBundleService(session, settings)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def read(self, paper_id: UUID) -> ChartReviewAuthorityProjection:
        """Authoritative whole-paper chart stage for one paper."""
        task = self._service.get_review_task(paper_id)
        return self.project(task, paper_id=paper_id)

    #: Parallel readers used for a page-sized batch. Each worker gets its own
    #: session, so this only widens read concurrency.
    MAX_WORKERS = 8

    def read_many(self, paper_ids: Iterable[UUID], *, parallel: bool = True) -> ChartReviewReadResult:
        """Authoritative stage for several papers, capped at :attr:`MAX_BATCH`.

        One paper costs roughly 300 ms because the authoritative task re-derives
        content fingerprints for every figure and table.  A page-sized batch is
        therefore read concurrently over independent sessions; ordering of the
        returned mapping is irrelevant to callers.
        """
        ordered: list[UUID] = []
        seen: set[str] = set()
        for paper_id in paper_ids:
            key = str(paper_id)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > self.MAX_BATCH:
                raise ValueError(f"chart_review_authority_batch_too_large:max={self.MAX_BATCH}")
            ordered.append(paper_id)

        stages: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        factory = self._session_factory() if parallel and len(ordered) > 1 else None
        if factory is None:
            for paper_id in ordered:
                self._read_into(paper_id, stages, errors, reader=self.read)
            return ChartReviewReadResult(stages, errors)

        def work(paper_id: UUID) -> tuple[str, dict[str, Any] | None, str | None]:
            worker_session = factory()
            try:
                reader = ChartReviewAuthority(worker_session, self.settings).read
                payload: dict[str, Any] | None = None
                failure: str | None = None
                try:
                    payload = reader(paper_id).as_payload()
                except LookupError as exc:
                    failure = str(exc)
                except Exception as exc:  # noqa: BLE001 - surfaced per paper
                    failure = f"{type(exc).__name__}: {exc}"
                return str(paper_id), payload, failure
            finally:
                worker_session.close()

        with ThreadPoolExecutor(max_workers=min(self.MAX_WORKERS, len(ordered))) as pool:
            for key, payload, failure in pool.map(work, ordered):
                if payload is not None:
                    stages[key] = payload
                elif failure is not None:
                    errors[key] = failure
        return ChartReviewReadResult(stages, errors)

    @staticmethod
    def _read_into(paper_id: UUID, stages: dict[str, Any], errors: dict[str, str], *, reader: Any) -> None:
        key = str(paper_id)
        try:
            stages[key] = reader(paper_id).as_payload()
        except LookupError as exc:
            errors[key] = str(exc)
        except Exception as exc:  # noqa: BLE001 - degrade one row, never the page
            errors[key] = f"{type(exc).__name__}: {exc}"

    def _session_factory(self) -> Any:
        """Session factory bound to the caller's engine, or ``None`` to stay serial."""
        try:
            bind = self.session.get_bind()
            engine = getattr(bind, "engine", bind)
            if engine is None:
                return None
            return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
        except Exception:  # noqa: BLE001 - any failure falls back to the serial path
            return None

    def project(self, task: dict[str, Any], *, paper_id: UUID) -> ChartReviewAuthorityProjection:
        """Project a ``chart_review_task_v1`` payload into the authority shape."""
        stage_status = str(task.get("stage_status") or "unknown")
        current = task.get("current_snapshot_fingerprint")
        completed = task.get("completed_snapshot_fingerprint")
        unresolved_count = int(task.get("unresolved_count") or 0)
        fingerprint_matches = bool(completed) and completed == current
        scope_completion = task.get("scope_completion") if isinstance(task.get("scope_completion"), dict) else {}
        scope_complete = bool(scope_completion.get("complete"))
        source = self._source_for(paper_id)
        figures_completed = (
            stage_status in FIGURE_COMPLETE_STAGES
            and fingerprint_matches
            and unresolved_count == 0
            and scope_complete
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "paper_id": str(paper_id),
            "scope_type": "paper",
            "run_id": None,
            "authoritative": True,
            "source": source,
            "stage_status": stage_status,
            "figures_completed": figures_completed,
            "unresolved_count": unresolved_count,
            "dft_gate_allowed": (
                stage_status in DFT_GATE_STAGES
                and unresolved_count == 0
                and fingerprint_matches
                and scope_complete
            ),
            "figure_reading_coverage": task.get("figure_reading_coverage") or {"completed": 0, "total": 0},
            "current_snapshot_fingerprint": current,
            "completed_snapshot_fingerprint": completed,
            "snapshot_fingerprint_matches": fingerprint_matches,
            "scope_completion": scope_completion,
            "reviewed_at": task.get("reviewed_at"),
            "latest_review_run_id": task.get("latest_review_run_id"),
            "blocking_errors": task.get("blocking_errors") or [],
            "computed_at": _utc_iso(),
        }
        return ChartReviewAuthorityProjection(payload)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _source_for(self, paper_id: UUID) -> str:
        """Which state machine produced the stage (informational, not authoritative).

        The stage itself always comes from the task reader above; this only tells
        operators whether a paper is V2-native, legacy-only, or untouched.
        """
        has_v2 = self.session.scalar(
            select(AuditLog.id)
            .where(AuditLog.paper_id == paper_id, AuditLog.action == V2_RECEIPT_ACTION)
            .limit(1)
        )
        if has_v2 is not None:
            return "paper_review_v2"
        has_legacy = self.session.scalar(
            select(AuditLog.id)
            .where(AuditLog.paper_id == paper_id, AuditLog.action.in_(sorted(LEGACY_TASK_ACTIONS)))
            .limit(1)
        )
        return "legacy_evidence_review" if has_legacy is not None else "none"


def sync_figures_review_completed(
    session: Session,
    paper_id: UUID,
    *,
    reviewer: str,
    stage_status: str,
    unresolved_count: int,
    snapshot_fingerprint_matches: bool,
    legacy_source_paper_scope: bool = True,
    scope_complete: bool | None = None,
) -> bool:
    """Write the ``manual_review_progress.figures`` compatibility flag.

    This is the *only* sanctioned writer of that compatibility flag.  It refuses
    to write unless the caller proves that the **whole paper** scope is complete
    on a **fresh** snapshot -- so a run-scoped ``external_analysis_run`` batch, a
    stale snapshot, or a task with unresolved actions can never mark the paper
    complete.  Returns whether the flag was written.

    ``scope_complete`` may be passed by a caller that already computed the
    expected/reviewed figure+table sets.  When it is ``None`` the whole-paper
    authority is re-read here, which is the strongest available proof and also
    covers "the snapshot moved between the caller's read and this write".
    """
    if not legacy_source_paper_scope:
        return False
    if str(stage_status or "") not in FIGURE_COMPLETE_STAGES:
        return False
    if int(unresolved_count or 0) != 0:
        return False
    if not snapshot_fingerprint_matches:
        return False
    if scope_complete is None:
        try:
            from app.config import get_settings

            authority = ChartReviewAuthority(session, get_settings()).read(paper_id)
        except Exception:  # noqa: BLE001 - an unverifiable scope is never written
            return False
        if not authority.figures_completed:
            return False
    elif not scope_complete:
        return False
    from app.services.evidence_review_bundle_service import _normalize_progress_entry, _utc_iso as _iso

    paper = session.get(Paper, paper_id)
    if paper is None:
        return False
    analysis = dict(paper.comprehensive_analysis or {})
    raw_progress = analysis.get("manual_review_progress") if isinstance(analysis.get("manual_review_progress"), dict) else {}
    progress = {module: _normalize_progress_entry(raw_progress, module) for module in ("content", "figures", "dft")}
    current = progress["figures"]
    if current.get("completed") and current.get("updated_by") == reviewer:
        return False
    progress["figures"] = {"completed": True, "updated_at": _iso(), "updated_by": reviewer}
    analysis["manual_review_progress"] = progress
    paper.comprehensive_analysis = analysis
    session.add(paper)
    return True
