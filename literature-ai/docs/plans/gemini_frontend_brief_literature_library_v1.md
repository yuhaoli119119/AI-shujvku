# Gemini Frontend Brief: Literature Library Polish v1

## Goal

Rebuild the literature library page into a clean, high-density research workspace. The target feel is closer to a professional literature database, not a stack of oversized cards.

Codex owns backend changes. Gemini should only work on the frontend layer for the literature library page and related page-level interactions.

## User complaints that must be fixed

1. The left literature directory is visually broken after compression and important information is unreadable.
2. The left directory is too short; it should visually run the full usable height like the right workspace.
3. The year heading is too large and steals space.
4. Papers should not only be grouped by year visually; within the year they must also respect serial number order from small to large.
5. The user wants optional pane visibility:
   - reading a single paper: allow hiding the left literature directory
   - browsing the library: allow hiding the right detail workspace
6. The overall page should feel professional, clean, ordered, and suitable for literature review work.

## Backend contract already prepared by Codex

Use `GET /api/papers` with these rules:

- default sort is already stable: `year asc -> serial_number asc -> title`
- optional params now supported:
  - `sort_by=year_serial|created_at|title`
  - `sort_order=asc|desc`

Important: frontend should **not** re-sort records in a conflicting way after receiving them.

Useful fields already present in each paper row:

- `serial_number`
- `year`
- `title`
- `title_zh`
- `journal`
- `doi`
- `pdf_quality_status`
- `pdf_quality_report.metrics.page_count`
- `pdf_quality_report.metrics.file_size_bytes`
- `counts.dft_results`
- `counts.figures`
- `counts.writing_cards`
- `workflow_status`

## Required UI changes

### 1. Left panel: literature directory

Use a compact row/list design, not tall cards.

Each row should show, in priority order:

- year group label
- serial number
- title
- journal
- DOI optional and de-emphasized
- PDF quality chip
- page count
- file size
- compact counters for DFT / figures / writing

Rules:

- year label must be visually smaller than current version
- serial number must be easy to scan
- titles must not collapse into unreadable narrow columns
- no horizontal scrollbar inside each paper row
- the whole left panel should use the full available height
- the list area should scroll cleanly inside the panel

### 2. Year grouping

The page should visually group papers by year.

Within each year:

- preserve backend order
- display serial number from small to large

Year group header should be modest, similar to a table section label, not a hero heading.

### 3. Pane visibility modes

Add a simple view mode control near the library page top area:

- `双栏`
- `只看目录`
- `只看文献`

Behavior:

- `双栏`: show left directory + right detail
- `只看目录`: hide right workspace, expand left
- `只看文献`: hide left directory, expand right

Persist the last chosen mode in `localStorage`.

### 4. Top area compression

The filter/action/search area should be denser and calmer:

- reduce vertical waste
- keep one clear search row
- keep one compact filter/action row
- avoid oversized boxes
- keep the page looking like a working tool, not a landing page

### 5. Detail workspace compatibility

Do not redesign the detail content structure deeply in this pass.
Only ensure:

- it expands correctly when left panel is hidden
- no overlap
- no clipped content

## UX tone

The page should feel:

- professional
- compact
- ordered
- easy to scan for review writing
- visually quieter than the current card-heavy version

Avoid:

- oversized pills
- giant year numbers
- multi-line visual clutter in the left directory
- decorative empty space

## Implementation notes

Frontend can use `localStorage` for pane mode persistence; no backend persistence is required for this part.

If a field is missing:

- year missing: show `年份待补`
- journal missing: show `期刊待补`
- DOI missing: show `无 DOI`
- page count missing: show `页数待补`
- file size missing: show `大小待补`

## Acceptance checklist

1. Left directory rows remain readable at normal desktop width.
2. No row in the left directory shows a horizontal scrollbar.
3. Year groups are visible but visually restrained.
4. Serial numbers inside the same year are visually scan-friendly and ordered ascending.
5. User can switch between directory-only / split / paper-only modes.
6. Left panel can occupy the main width when right panel is hidden.
7. Right panel can occupy the main width when left panel is hidden.
8. The page looks closer to a literature database than to a card dashboard.
