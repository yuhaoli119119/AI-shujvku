# D4-4A Paper Filtering & Citation Eligibility Gate

Date: 2026-05-28

## Scope

D4-4A adds backend-only support for screening papers before writing and marking papers as excluded from citation without deleting them.

## Added backend capability

- `GET /api/library/papers/filter`
- `POST /api/library/papers/{paper_id}/citation-eligibility`
- `POST /api/library/papers/citation-eligibility/bulk`
- `PaperFilterService`
- `CitationEligibilityService`

## Citation eligibility metadata

Stored in side table `paper_citation_eligibility`:

- `included_for_writing`
- `exclude_from_citation`
- `exclude_reason`
- `citation_priority`: `high`, `medium`, `low`, `exclude`
- `user_note`
- `updated_at`

Default behavior: papers without an eligibility row are treated as included candidates with `citation_priority=medium` and `exclude_from_citation=false`.

## Impact factor metadata

Stored in side table `paper_impact_metadata`:

- `impact_factor`
- `impact_factor_source`: for example `user_imported`, `manual`, or `unknown`
- `impact_factor_year`
- `updated_at`

The backend does not fetch impact factors online. Missing impact factors are reported as `impact_factor_status=needs_metadata`; papers are not deleted or automatically excluded merely because impact factor metadata is absent.

## Supported filters

- `year_min`
- `year_max`
- `journal_includes`
- `journal_excludes`
- `impact_factor_min`
- `impact_factor_max`
- `keyword`
- `has_pdf`
- `has_parsed_text`
- `has_extraction_output`
- `has_verified_evidence`
- `has_safe_verified_evidence`
- `exclude_from_citation`
- `citation_priority`
- `needs_metadata`
- `limit`
- `offset`

When `exclude_from_citation` is omitted, the filter returns the default candidate set and excludes papers marked `exclude_from_citation=true`.

## Safety notes

- Filtering is read-only.
- Citation eligibility writes only touch `paper_citation_eligibility`.
- No paper deletion is performed.
- No extraction result mutation is performed.
- No review or verified state mutation is performed.
- No DFT export or writing safe gate is unlocked.
- No Zotero API, frontend UI, AI writing, automatic citation insertion, physical delete, `mark_verified`, export, or writing unlock is included.

## Migration status

Migration file added: `backend/app/migrations/002_paper_citation_eligibility.sql`.

Active DB migration was not applied in this run. Tests create the new tables only in temporary SQLite databases through SQLAlchemy metadata.
