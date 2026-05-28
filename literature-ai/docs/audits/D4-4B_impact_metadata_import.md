# D4-4B Impact Metadata Import

## Scope

Implemented a controlled Journal Impact Factor metadata import path. The importer writes only to `paper_impact_metadata` and leaves `papers`, reviews, evidence locators, extraction outputs, materialization, registry, and artifact files untouched.

## API

`POST /api/library/impact-metadata/import`

Query parameters:

- `dry_run`: default `false`; parses, validates, matches, and reports without writing when `true`.
- `expected_papers_total`: optional guard. For the workspace default active DB, the API defaults this to `15`.

Supported request bodies:

- `Content-Type: text/csv`
- `Content-Type: application/json`

CSV fields:

- `journal`
- `impact_factor`
- `impact_factor_year`
- `impact_factor_source`
- optional `issn`
- optional `eissn`
- optional `note`

JSON shape:

```json
{
  "source": "user_imported",
  "year": 2024,
  "items": [
    {
      "journal": "Advanced Energy Materials",
      "impact_factor": 24.4,
      "impact_factor_year": 2024,
      "impact_factor_source": "user_imported"
    }
  ]
}
```

## Matching

The first implementation uses deterministic journal-name matching only. It normalizes both imported journal names and `papers.journal` by:

- trimming
- casefolding
- collapsing whitespace
- replacing common punctuation differences with spaces

No fuzzy matching, web lookup, scraping, or online IF fetching exists.

## Write Behavior

Rows are upserted by `paper_id`, matching the existing `paper_impact_metadata` schema. Duplicate import rows for the same normalized journal, year, and source are deduplicated deterministically by keeping the last row in input order. Unmatched metadata is returned in `unmatched_items`.

The API response includes:

- `imported_count`
- `updated_count`
- `matched_paper_count`
- `unmatched_items`
- `invalid_items`
- `needs_metadata_remaining`
- `source`
- `impact_factor_year`
- `active_db_write_performed`
- before/after snapshots

## Active DB Smoke

Only a dry run was performed against the canonical active DB. No impact metadata was written because no user-provided formal IF table was supplied.

Smoke fixture:

- source: `test_import`
- year: `2099`
- journal: `Nature Energy`
- impact factor: `1.0`
- result: matched 1 paper, would import 1 row, wrote 0 rows

See `docs/audits/D4-4B_impact_metadata_import_smoke.json`.

## Safety Snapshot

Dry-run active DB before and after:

- `papers_total`: 15 -> 15
- `paper_impact_metadata_rows`: 0 -> 0
- `paper_citation_eligibility_rows`: 0 -> 0
- `review_rows`: 5 -> 5
- `evidence_locator_rows`: 4 -> 4
- `verified_review_rows`: 0 -> 0
- `safe_verified_rows`: 0 -> 0
- `included_for_writing_rows`: 0 -> 0

No paper deletion, verification, export unlock, writing unlock, extraction apply, materialization, registry write, DB copy/move/delete, or artifact cleanup was performed.
