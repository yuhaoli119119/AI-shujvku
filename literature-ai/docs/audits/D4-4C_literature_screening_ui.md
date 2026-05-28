# D4-4C Literature Screening UI Audit

## Objectives
- Introduce a dedicated UI (`frontend/pages/literature_screening/index.html`) for fast literature screening.
- Support importing impact metadata via CSV/JSON (Dry Run by default).
- Support filtering papers by year, journal, impact factor, needs_metadata, pdf availability, parsing status, extraction output, and verified evidence counts.
- Support bulk operations: Mark as Do Not Cite, Set citation priority.
- Ensure security and correct bounding: No deletion of papers, no faking of verified status, no writing to DB outside of the citation eligibility endpoint.

## Implementation Details
1. **Screening Page**
   - Created `frontend/pages/literature_screening/index.html`.
   - Included filter panel covering all required fields.
   - Built a robust table displaying impact_factor, needs_metadata status, verified counts, citation priority, etc.
   - Bulk action toolbar for setting "Do Not Cite" (`exclude_from_citation=true`) and citation priorities, with double confirmation.
   - Integrated import panel for impact metadata with explicit default `dry_run=true` and manual confirmation for real runs.

2. **API Integrations**
   - **GET** `/api/library/papers/filter` to fetch filtered results.
   - **POST** `/api/library/papers/citation-eligibility/bulk` for updating citation fields safely without modifying core fields or review items.
   - **POST** `/api/library/impact-metadata/import` for CSV/JSON imports with robust parsing and feedback counts.

3. **Constraints Validated**
   - **No Delete Paper**: UI does not have a delete button, no API calls for deletion are made.
   - **No Verification Faking**: The frontend strictly binds verified fields as read-only from the GET response, and `exclude_from_citation` does not overlap with `mark-verified`.
   - **No Unsafe Operations**: DB migrations, extraction, materialize, and registry write operations are completely bypassed in this context.
   - **Safety First**: Dry Run for metadata import is checked by default. Non-dry run triggers standard browser confirm dialog.

## Testing Strategy
- Expanded Playwright smoke tests to include:
  1. `Literature Screening` page load and filter API call.
  2. Filter parameter bindings (`year_min`, `impact_factor_min`, `needs_metadata`, etc.).
  3. Bulk `Mark Do Not Cite` flow and payload structure.
  4. Bulk `Set selected priority` flow and payload structure.
  5. `Import Impact Metadata` panel toggle, dry run default checking, API payload parsing, and dialog interception for non-dry run.
- Tests assert no `verified` faking and no `delete` calls are initiated by this page.

## Result
Tests pass, operations safely guarded. The UI correctly functions as an independent screening layer without touching or degrading the main literature library or extraction pipelines.
