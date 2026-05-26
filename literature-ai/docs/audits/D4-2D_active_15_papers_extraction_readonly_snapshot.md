# D4-2D Active 15 Papers Extraction Read-only Coverage Snapshot

Date: 2026-05-27

Scope: read-only coverage snapshot for the active canonical 15-paper database. This pass only read runtime DB resolution metadata, SQLite rows, and local artifact existence. It did not modify database content, registry files, papers, reviews, extraction outputs, materialized facts, or artifacts.

## 1. Baseline / Sync

Commands requested and executed before any edit:

- `git status --short`: clean
- `git log -1 --oneline`: `6776717 docs d4 active db runtime resolution confirmation`
- `git rev-parse HEAD`: `677671799d58fb75872b7ca270ce086058983042`
- `git branch -vv`: `master 6776717 [origin/master] docs d4 active db runtime resolution confirmation`
- `git fetch origin`: succeeded
- `git rev-parse origin/master`: `677671799d58fb75872b7ca270ce086058983042`
- `git ls-remote origin refs/heads/master`: `677671799d58fb75872b7ca270ce086058983042 refs/heads/master`

Baseline conclusion:

- Starting HEAD: `677671799d58fb75872b7ca270ce086058983042`
- Local `origin/master`: `677671799d58fb75872b7ca270ce086058983042`
- Remote `refs/heads/master`: `677671799d58fb75872b7ca270ce086058983042`
- Worktree before edits: clean.
- HEAD, local `origin/master`, and remote `refs/heads/master` were identical before this docs-only manifest was added.

## 2. Active DB Confirmation

Runtime resolver output:

- registry path used: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\library_registry.json`
- resolved DB path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- effective DB path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- active library: `默认文献库`
- active library root: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default`
- `papers_total`: 15
- stable sample exists: yes
  - DB id: `3978dc79f94f4457863fd68449ae293d`
  - expected hyphenated id: `3978dc79-f94f-4457-863f-d68449ae293d`
  - title: `锂硫电池非均相电催化剂`

Gate result: active runtime DB is the canonical 15-paper DB. Snapshot proceeded.

## 3. Coverage Summary Matrix

| Metric | Count |
| --- | ---: |
| total papers | 15 |
| papers with PDF | 6 |
| papers without PDF | 9 |
| papers with parsed text or markdown artifact | 6 |
| papers missing parsed text and markdown artifact | 9 |
| papers with any extraction output | 6 |
| papers with zero extraction output | 9 |
| papers with locator page coverage | 0 |
| papers with only text-only / missing-page evidence | 4 |
| papers with verified review | 0 |
| papers with safe verified review | 0 |
| papers eligible for DFT export | 0 |
| papers fully blocked from DFT export | 0 |
| papers eligible for writing evidence pack | 0 |
| papers blocked from writing evidence pack | 6 |
| non-canonical artifact/path count | 0 |
| suspicious backend/data or backup path count | 0 |
| total materialized fact rows | 13 |
| total evidence items | 18 |
| total review records | 0 |
| total safe verified reviews | 0 |
| total DFT rows | 0 |
| total writing cards | 6 |
| total writing eligible | 0 |
| total writing blocked | 6 |
| GREEN papers | 0 |
| YELLOW papers | 14 |
| RED papers | 1 |

Notes:

- URL/DOI-like `source_path` values were treated as source metadata, not local artifacts, and were not counted as non-canonical artifact paths.
- Local artifact path checks covered `pdf_path`, `tei_path`, `docling_json_path`, and `markdown_path`.
- No checked local artifact path resolved under `backend/data`, `backend/data_backup`, `uploaded_dbs`, Temp, pytest, or any other non-canonical area.

## 4. 15-paper Table

| # | Paper ID | Title | Year | PDF | Text/MD | Extraction | Evidence | Locator pages | Reviews | DFT export | Writing | Risk |
| ---: | --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| 1 | `cac6f70ade9041dba3c7518861c4cc15` | Advances in lithium-sulfur batteries based on multifunctional cathodes and electrolytes | 2016 | no | no | 0 | 0 | 0 | 0 | n/a | n/a | YELLOW |
| 2 | `7eecdb29ba60413fbeca82dc8532dd1e` | 聚偏氟乙烯β相全反式结构链的第一性原理计算 | 2002 | yes | yes | 2 | 2 | 0 | 0 | n/a | blocked | YELLOW |
| 3 | `267338173c80493e81bfa02435bf22a2` | Liquid electrolyte lithium/sulfur battery: Fundamental chemistry, problems, and solutions | 2013 | no | no | 0 | 0 | 0 | 0 | n/a | n/a | YELLOW |
| 4 | `584bda44ec114811974d61bdb83a1fc7` | 分子和金表面相互作用的第一性原理研究 | 2002 | yes | yes | 2 | 0 | 0 | 0 | n/a | blocked | YELLOW |
| 5 | `729f4dc3cb3c45ca94c306e29eeebe80` | 四方铁电体PbFe0.5Nb0.5O3精细结构的第一性原理研究 | 2002 | yes | yes | 2 | 2 | 0 | 0 | n/a | blocked | YELLOW |
| 6 | `a2306ee3c87548f19b032181fc9bfa90` | GaN中与C和O有关的杂质能级第一性原理计算 | 2005 | yes | yes | 2 | 0 | 0 | 0 | n/a | blocked | YELLOW |
| 7 | `38c0e8d04d6545dcadb5ec32380caf21` | 不同掺杂浓度Lu掺杂GaN电子结构和光学性质的第一性原理研究 | 2024 | no | no | 0 | 0 | 0 | 0 | n/a | n/a | YELLOW |
| 8 | `89cc5174b5344bfaa99b6a61ca82dc44` | 第一原理計算からのRh-ドープTiO 2 における半金属性 | 2012 | no | no | 0 | 0 | 0 | 0 | n/a | n/a | YELLOW |
| 9 | `e44528c13b05495580fa565aec0d4244` | Ti掺杂SnO 2 半导体固溶体的第一性原理研究 | 2012 | no | no | 0 | 0 | 0 | 0 | n/a | n/a | YELLOW |
| 10 | `952928fc603d4dd4b7d53de33e595a1a` | 基于第一性原理方法研究ReO x 在ReO x -Rh/ZrO 2 和ReO x -Ir/ZrO 2 催化的甘油氢解反应中的作用机制 | 2013 | no | no | 0 | 0 | 0 | 0 | n/a | n/a | YELLOW |
| 11 | `c13f0602aad94bc5866b0434c15c699c` | 二维M2XO2-2x(OH)2x(M=Ti, V;X=C, N)析氢催化活性的第一性原理研究 | 2017 | no | no | 0 | 0 | 0 | 0 | n/a | n/a | YELLOW |
| 12 | `33343806f92b469d82032da72dd3ad15` | 第一原理からのFe 3 Cセメンタイト表面の構造と安定性 | 2003 | no | no | 0 | 0 | 0 | 0 | n/a | n/a | YELLOW |
| 13 | `96b7c810a6964a9b867b9624a4fd3618` | 锂/硫电池的研究现状、问题及挑战 | 2013 | no | no | 0 | 0 | 0 | 0 | n/a | n/a | YELLOW |
| 14 | `3978dc79f94f4457863fd68449ae293d` | 锂硫电池非均相电催化剂 | 2022 | yes | yes | 3 | 11 | 0 | 0 | n/a | blocked | YELLOW |
| 15 | `b234de0a6fff43f1aedb5f691f76004f` | Revealing the 16-electron sulfur reduction reaction network in lithium sulfur (Li-S) batteries | 2024 | yes | yes | 2 | 3 | 0 | 0 | n/a | blocked | RED |

Column notes:

- `Extraction` counts materialized fact rows from `catalyst_samples`, `dft_settings`, `dft_results`, `mechanism_claims`, `electrochemical_performance`, and `figure_data_points`.
- `Evidence` counts `evidence_locators`, `evidence_spans`, and `evidence_claims`.
- `DFT export` is `n/a` because no paper currently has `dft_results` rows.
- `Writing` is blocked where writing cards exist because no safe verified review payload is present.

## 5. Risk Classification by Paper

### GREEN

None. No paper has PDF/text/extraction/evidence/review state sufficient for a fully safe downstream export/writing posture.

### YELLOW

The following papers have explainable incompleteness and current safety gates keep export/writing blocked or unavailable:

- `cac6f70ade9041dba3c7518861c4cc15`: missing PDF, parsed text/markdown, extraction output, evidence, and safe verified review.
- `7eecdb29ba60413fbeca82dc8532dd1e`: PDF/text exist and extraction rows exist, but evidence has no page coverage and writing is blocked by missing review.
- `267338173c80493e81bfa02435bf22a2`: missing PDF, parsed text/markdown, extraction output, evidence, and safe verified review.
- `584bda44ec114811974d61bdb83a1fc7`: PDF/text and extraction rows exist, but evidence is zero and writing is blocked by missing review.
- `729f4dc3cb3c45ca94c306e29eeebe80`: PDF/text and extraction rows exist, but evidence has no page coverage and writing is blocked by missing review.
- `a2306ee3c87548f19b032181fc9bfa90`: PDF/text and extraction rows exist, but evidence is zero and writing is blocked by missing review.
- `38c0e8d04d6545dcadb5ec32380caf21`: missing PDF, parsed text/markdown, extraction output, evidence, and safe verified review.
- `89cc5174b5344bfaa99b6a61ca82dc44`: missing PDF, parsed text/markdown, extraction output, evidence, and safe verified review.
- `e44528c13b05495580fa565aec0d4244`: missing PDF, parsed text/markdown, extraction output, evidence, and safe verified review.
- `952928fc603d4dd4b7d53de33e595a1a`: missing PDF, parsed text/markdown, extraction output, evidence, and safe verified review.
- `c13f0602aad94bc5866b0434c15c699c`: missing PDF, parsed text/markdown, extraction output, evidence, and safe verified review.
- `33343806f92b469d82032da72dd3ad15`: missing PDF, parsed text/markdown, extraction output, evidence, and safe verified review.
- `96b7c810a6964a9b867b9624a4fd3618`: missing PDF, parsed text/markdown, extraction output, evidence, and safe verified review.
- `3978dc79f94f4457863fd68449ae293d`: PDF/text and extraction rows exist, but evidence has no page coverage and writing is blocked by missing review.

### RED

- `b234de0a6fff43f1aedb5f691f76004f`: one `evidence_locators` row has `locator_status=text_only`, no page, and a bbox-like payload. PDF jump remains disabled and no export/writing row is marked safe, but the locator shape is internally inconsistent enough to require a RED risk label for the next review gate.

## 6. Evidence / Review / Export Safety

Evidence locator state:

- total evidence items: 18
- papers with locator/page coverage: 0
- papers with only text-only or missing-page evidence: 4
- pseudo/unsafe locator risk: 1 paper, due to bbox without page on a text-only locator
- PDF jump should remain disabled for all evidence in this snapshot.

Review state:

- review records: 0
- verified reviews: 0
- safe verified reviews: 0
- unsafe/stale/unresolved/ambiguous review rows: 0
- verified-like serialized payloads: 0
- save_reviews bypass signal: not observed

Export / writing state:

- DFT rows: 0
- DFT export safe rows: 0
- DFT export blocked rows: 0
- writing cards: 6
- writing eligible: 0
- writing blocked: 6
- writing blocked reason: missing review
- Text-only evidence was not counted as safe export or safe writing evidence.

## 7. Safety Checks

- DB write: no
- DB copy/move/delete: no
- Registry write: no
- Migration apply: no
- Verified review write: no
- Extraction/reprocessing apply: no
- Materialize: no
- Artifact cleanup: no
- Backend/data or backend/data_backup used as runtime DB: no
- Snapshot taken from 0/4/5-paper DB: no
- Real data modified to improve coverage: no

## 8. Recommendation for Next Gate

Do not proceed to apply/reprocessing/materialization from this snapshot. The next gate should be a targeted review/readiness triage that decides whether to:

1. inspect the RED locator consistency case without writing DB state,
2. plan a reviewed extraction/reprocessing pass for the 9 metadata-only papers,
3. keep all export/writing paths blocked until safe verified reviews exist,
4. explicitly preserve canonical runtime DB resolution from D4-2C before any future apply step.
