# D4-7A Citation Insert Draft Gate

## Goal

Added a backend-only, read-only citation insertion draft proposal API. The endpoint helps a writing UI turn a selected citation candidate into a safe draft proposal with insertion text, warnings, and a human review checklist.

This is not a final citation inserter, not a bibliography generator, not Zotero integration, and not a verification writer.

## Modified Files

- `backend/app/api/writing.py`
- `backend/app/services/writing_citation_candidate_service.py`
- `backend/app/services/writing_citation_insertion_service.py`
- `backend/tests/test_d4_writing_citation_insertion_draft.py`
- `docs/audits/D4-7A_citation_insert_draft_gate.md`

No frontend files were changed.

## API Contract

`POST /api/writing/citation-insertion-draft`

Request fields:

- `text`: user sentence or paragraph.
- `selected_paper_id`: selected candidate paper id.
- `citation_marker`: optional draft marker supplied by the UI.
- `insertion_mode`: `parenthetical`, `narrative`, or `comment_only`.
- `citation_style`: `draft_author_year` or `placeholder`.
- `candidate_evidence_status`: client-provided context only; not trusted.
- `candidate_can_be_used_as_confirmed_citation`: client-provided context only; not trusted.
- `candidate_requires_human_verification`: client-provided context only; not trusted.
- `supporting_snippet`: optional draft context only; not upgraded to evidence.
- `user_note`: optional context only.

Response fields include:

- paper metadata
- citation marker
- insertion mode and style
- `draft_text`
- `proposal_status`
- `can_insert_as_confirmed_citation`
- `requires_human_verification`
- `evidence_status`
- `warnings`
- `human_review_checklist`
- `blocked_actions`
- `safety`

The response intentionally does not contain a final bibliography or references payload.

## Safety Classification

The endpoint re-reads current DB state for `selected_paper_id` through the D4-5 candidate safety evaluator. It does not trust client-provided candidate safety flags.

Rules:

- `safe_verified`: `proposal_status=confirmed_candidate_draft`, `can_insert_as_confirmed_citation=true`.
- `verified`: `proposal_status=verified_but_requires_safety_review`, `can_insert_as_confirmed_citation=false`.
- `pending_with_locator`, `pending_without_locator`, `unverified_extraction`, `unknown`: `proposal_status=needs_human_verification`, `can_insert_as_confirmed_citation=false`.
- `metadata_only`: `proposal_status=metadata_only_draft`, with warning that metadata-only suggestions cannot be used as evidence yet.
- missing impact factor: does not block proposal, but warns that IF completeness is not evidence quality.

Unsafe draft markers include:

`[DRAFT CITATION - VERIFY SOURCE BEFORE USE: ...]`

## Hard Blocks

The endpoint hard blocks:

- `exclude_from_citation=true`
- `citation_priority=exclude`
- missing `selected_paper_id`
- blank text

Excluded papers return `proposal_status=blocked_excluded_from_citation`, `draft_text=null`, and do not receive a normal citation draft.

## Test Results

Focused tests:

- `py -m pytest tests\test_d4_writing_citation_insertion_draft.py`: 11 passed
- `py -m pytest tests\test_d4_writing_citation_candidates.py`: 20 passed

Full validation:

- `py -m compileall app findpapers tests`: passed
- `py -m pytest`: 432 passed
- `git diff --check`: passed, with Windows LF/CRLF warnings only

## Active DB Smoke

Read-only smoke was run against:

`D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`

Flow:

- called `POST /api/writing/citation-candidates`
- selected a real unverified candidate
- called `POST /api/writing/citation-insertion-draft`
- intentionally sent a forged `candidate_can_be_used_as_confirmed_citation=true`

Result:

- `draft_status_code`: 200
- selected paper: `Revealing the 16-electron sulfur reduction reaction network in lithium sulfur (Li-S) batteries`
- selected candidate evidence status: `unverified_extraction`
- proposal status: `needs_human_verification`
- `can_insert_as_confirmed_citation`: false
- `requires_human_verification`: true
- warning included client confirmed flag being ignored

Before and after counts were identical:

- `papers_total`: 15
- `paper_impact_metadata_rows`: 0
- `paper_citation_eligibility_rows`: 0
- `review_rows`: 5
- `evidence_locator_rows`: 4
- `verified_review_rows`: 0
- `safe_verified_review_rows`: 0
- `export_eligible_dft_rows`: 0
- `writing_cards_eligible`: 0

## Boundary Report

- Active DB writes: no
- Frontend touched: no
- Papers/reviews/locators/citation eligibility/impact metadata touched: no
- Migration: no
- Registry write: no
- Artifact cleanup: no
- Paper deletion: no
- Export/writing unlock: no
- Bibliography generation: no
- `verified` or `safe_verified` writes: no

## Remaining Risks

The marker generator is a draft placeholder and is not CSL-compliant. It should remain visually marked as draft text until a later bibliography/export-safe workflow is explicitly built.
