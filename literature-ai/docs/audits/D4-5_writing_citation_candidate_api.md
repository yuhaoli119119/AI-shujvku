# D4-5 Writing Citation Candidate API

## Scope

Implemented a read-only citation candidate recommendation API for drafting support. It recommends papers from the current literature database for a user-provided draft sentence, claim, paragraph, or keyword text.

This does not write article text, insert citations, call Zotero, mark verification, modify review state, write impact metadata, unlock export/writing, materialize extraction, or change registry/artifacts.

## API

`POST /api/writing/citation-candidates`

Request fields:

- `text`: draft sentence, claim, paragraph, or keyword text.
- `max_candidates`: 1-50, default 10.
- `filters`: optional year, journal, impact factor, metadata, asset, evidence, and citation priority filters.
- `include_unverified_suggestions`: default true.
- `include_pending_review`: default true.

Response fields include:

- `query_text`
- `candidate_count`
- `candidates`
- `excluded_count`
- `excluded_reasons`
- `warnings`
- `safety`

Each candidate includes paper metadata, impact factor metadata, citation eligibility, deterministic score/tier, evidence status, whether it can be used as a confirmed citation, whether human verification is required, matched fields, supporting snippets, reason, and warnings.

## Recommendation Strategy

The first implementation is deterministic and offline:

- normalize query text into searchable tokens
- score token overlap against title, abstract, paper sections, evidence claims, review evidence text, locators, and extraction rows
- boost high citation priority, safe verified evidence, verified evidence, PDF availability, impact factor availability/value, and recency when year filters are used
- penalize missing impact metadata, low priority, metadata-only matches, and weak evidence status
- sort by score, high priority, year, and title

No online model, web lookup, DOI lookup, Zotero integration, or scraping is used.

## Hard Exclusions

The service hard excludes:

- `exclude_from_citation=true`
- `citation_priority=exclude`
- journal excludes
- year outside filters
- impact factor below/above explicit bounds
- missing impact factor when `impact_factor_min` or `impact_factor_max` is set

Missing impact factor is never treated as 0. When an impact-factor bound is set and metadata is missing, the paper is excluded with `needs_metadata_excluded_by_impact_factor_min` or `needs_metadata_excluded_by_impact_factor_max`, and a response warning summarizes the count.

## Evidence Status

Evidence statuses:

- `safe_verified`: matched review evidence passes the existing safe verified review gate.
- `verified`: matched evidence is verified/supported but not elevated beyond its actual gate.
- `pending_with_locator`: pending review evidence has an associated locator.
- `pending_without_locator`: pending review evidence has no associated locator.
- `unverified_extraction`: matched extraction row has no safe/verified review.
- `metadata_only`: match came from title/abstract/metadata-like text only.

Only `safe_verified` candidates return `can_be_used_as_confirmed_citation=true`. All pending, unverified, or metadata-only candidates return `requires_human_verification=true` and warning text.

Repaired/exact locators do not imply verification; pending review plus locator remains `pending_with_locator`.

## Active DB Smoke

Read-only POST smoke was run against:

`D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`

Request text:

`Single-atom catalysts can promote sulfur redox kinetics in lithium-sulfur batteries.`

Result:

- HTTP 200
- `candidate_count`: 4
- `excluded_count`: 0
- candidate titles:
  - `Revealing the 16-electron sulfur reduction reaction network in lithium sulfur (Li-S) batteries`
  - `锂硫电池非均相电催化剂`
  - `Advances in lithium–sulfur batteries based on multifunctional cathodes and electrolytes`
  - `Liquid electrolyte lithium/sulfur battery: Fundamental chemistry, problems, and solutions`

Before and after counts were identical:

- `papers_total`: 15
- `paper_impact_metadata_rows`: 0
- `paper_citation_eligibility_rows`: 0
- `review_rows`: 5
- `evidence_locator_rows`: 4
- `verified_review_rows`: 0
- `safe_verified_review_rows`: 0
- `included_for_writing_rows`: 0
- `writing_cards_eligible`: 0

No active DB writes were performed.

## Verification

Focused tests cover claim recommendation, hard exclusions, sorting boosts, year/journal/impact filters, missing IF handling, all evidence-status classes, repaired locator behavior, empty text errors, max candidate limits, and DB immutability.

Frontend was not changed; Playwright was not run.
