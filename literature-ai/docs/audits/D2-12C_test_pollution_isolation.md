# D2-12C Active Library Test Pollution Isolation

Date: 2026-05-26

## Scope

This gate isolated post-migration test pollution in the canonical active library:

- Active library root: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default`
- Active SQLite: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- No controlled migration rollback was performed.
- No active SQLite move was performed.
- No extraction apply was executed.
- No verified review was written.

## Baseline Pollution

After D2-12B.1 post-apply pytest, the target active library contained 10 UUID-only unreferenced PDFs under `storage/pdf`. These files were not part of the D2-12B.1 controlled migration copy plan.

Baseline audit summary:

- Active DB kind: `sqlite`
- Active database papers total: `15`
- Recovered from candidate scan: `false`
- DB-referenced PDF count: `6`
- Unreferenced PDF count: `10`
- Tiny UUID-only unreferenced PDF count: `10`

Baseline residue files:

| File | Size | SHA256 |
| --- | ---: | --- |
| `1e54832e-e5a4-4876-b33c-043789be680a.pdf` | 17 | `66341475e80a266565f74ef93b931f3ad5f69f337a27de5460ae285c328f5a4c` |
| `25133a79-7b6a-459f-a316-60896ab8ff83.pdf` | 20 | `e59d71466caa7bf1769387632522f4c016492af236f796863281c45eac068f53` |
| `2e0a8b92-fa51-4e45-adf6-1861b2a0f25a.pdf` | 23 | `f43bed28371582ea34456d25413c2d71d1c491c5bd03051b0ce041f50d2a6294` |
| `7bf05ae4-6da6-4729-8ec3-d0e1cea626e7.pdf` | 15 | `8e3c147d17df1db56ad03cffdb236c32c08ce4a5d2efa1c308f9be2f72dd652a` |
| `86deec84-f332-498f-a0b0-22921cc717a3.pdf` | 16 | `aa4c5751b7514b3b321041958ec7e3b14fb07bd1e1a4e51e82949cd32a73ec1b` |
| `96f86efd-0273-4e78-9150-1aebc98ea4fc.pdf` | 16 | `b0c4024b6fb86f6e2c3119b749e65093de779ad8f0180c863599b576944999b4` |
| `9c7a4f85-7570-4f8f-a7f4-2e011afbad44.pdf` | 15 | `8e3c147d17df1db56ad03cffdb236c32c08ce4a5d2efa1c308f9be2f72dd652a` |
| `9e1594cf-b0a4-44ee-83ff-b01de3d0f75c.pdf` | 15 | `6932f75671ebcbcb71cbe29848480d3dc01dd812a015ddfac330ebb66ed119b9` |
| `b5a12194-3830-4db2-8228-caec05bcc7cd.pdf` | 17 | `cf5f14df8f4ca4622fe6beac23a828a43d3d234916b95ef8d5f364e6f1023c09` |
| `f7ffb850-a5c7-46e7-929c-00c83d32bd13.pdf` | 17 | `66341475e80a266565f74ef93b931f3ad5f69f337a27de5460ae285c328f5a4c` |

## Source Identification

Pollution source: `literature-ai/backend/tests/test_papers_api.py`.

The `setup_test_db` fixture correctly redirected `LITAI_DATABASE_URL` to a temporary SQLite database, but did not redirect `LITAI_STORAGE_ROOT`. API upload and attach tests then wrote tiny PDF payloads through the ingestion path into the real active library storage root while recording metadata in the temporary test database. After the tests finished, those target-root PDFs were unreferenced by the real active SQLite.

The fixture now redirects both:

- `LITAI_DATABASE_URL` to a temporary test database
- `LITAI_STORAGE_ROOT` to a temporary storage directory

## Modified Files

- `literature-ai/backend/tests/test_papers_api.py`
- `literature-ai/backend/scripts/d2_active_library_test_pollution_audit.py`
- `scripts/d2_active_library_test_pollution_audit.py`
- `literature-ai/docs/audits/D2-12C_test_pollution_isolation.md`

## Audit And Cleanup Tool

Added `d2_active_library_test_pollution_audit.py` to scan the active target library for strict tiny UUID-only unreferenced PDFs.

Strict cleanup eligibility required all conditions:

- Located under active canonical `storage/pdf`
- File extension `.pdf`
- UUID-only filename
- Size less than 1 KB
- Not referenced by active SQLite paper artifact fields
- SHA256 recorded before deletion

Cleanup was run dry-run first, then apply. Deleted files count: `10`.

No DB-referenced PDFs, markdown, TEI, Docling JSON, figures, `database.sqlite`, `library.json`, or historical mirror files were deleted.

## Verification

Targeted source verification:

- `python -m pytest -q tests\test_papers_api.py`
- Result: `25 passed, 44 warnings`
- Post-targeted-test tiny UUID-only unreferenced PDF count: `0`

Full verification:

- `python -m compileall app findpapers tests`
- Result: passed
- `python -m pytest -q`
- Result: `294 passed, 457 warnings`

Post-full-pytest pollution audit:

- DB-referenced PDF count: `6`
- PDF files count: `6`
- Unreferenced PDF count: `0`
- Tiny UUID-only unreferenced PDF count: `0`

Active DB proof:

- DB kind: `sqlite`
- Active SQLite: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- Effective DB path equals active library DB path: `true`
- Active database papers total: `15`
- Recovered from candidate scan: `false`

Coverage proof:

- `Li2S2 / reaction_barrier / 2.73 eV` remains discoverable by `d2_real_extraction_coverage_gate.py`
- Extraction apply executed: `false`

Export and writing safety proof:

- `dft_export_safe_eligible=0`
- `writing_cards_safe_usable=0`
- `verified_reviews=0`

## Explicit Non-Actions

- Did not move active SQLite.
- Did not rollback D2-12B.1 migration.
- Did not delete DB-referenced artifacts.
- Did not delete historical mirror root content.
- Did not execute extraction apply.
- Did not write verified review.

## Remaining Risk

Future tests that combine temporary databases with ingestion or storage writes must also isolate `LITAI_STORAGE_ROOT`. The new audit script can be used as a guard after full test runs to detect regressions before they pollute the active canonical library.
