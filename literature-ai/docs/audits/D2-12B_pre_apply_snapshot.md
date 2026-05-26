# D2-12B Controlled Migration Pre-Apply Snapshot Gate

## Scope

This gate is a pre-apply snapshot and drift check only.

No controlled migration apply was executed.

## Git Snapshot

- HEAD: `ad2dffdb16e75559274dab90c4afd4326265d71a`
- remote `refs/heads/master`: `ad2dffdb16e75559274dab90c4afd4326265d71a`
- local HEAD matches remote: `true`

## Roots And Registry

- source root: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backend\DDesktop代码开发AI检索数据库literature-aibackenddatalibrariesdefault`
- target root: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default`
- canonical registry path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\library_registry.json`
- canonical registry SHA256: `40d3073efffda894fa6d1016747b6086df6a8b87878d8e3d7ace0d19899afb01`
- canonical registry still points to the historical mirror root: `true`
- target `database.sqlite` exists: `false`
- target `library.json` exists: `false`

## Active SQLite

- active SQLite path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backend\DDesktop代码开发AI检索数据库literature-aibackenddatalibrariesdefault\database.sqlite`
- active SQLite SHA256: `a1c7fb7b7a3b1beba57a532b994a8dd14d306c4b67f3a8fe842ad74fd73852df`
- db kind: `sqlite`
- active database papers total: `15`
- recovered from candidate scan: `false`

## Dry-Run Core Conclusions

- `target_conflicts_count=0`
- `ready_for_apply=true`
- `apply_executed=false`
- `missing_referenced_files_count=0`
- `duplicate_artifact_paths_count=0`
- `copy_plan=26 files`
- `includes_unreferenced_files=false`
- `skipped_unreferenced_files_count=212`
- migration mode: `db_referenced_only_plus_required_library_metadata`

## Copy Plan Summary

- active database: `1`
- required library metadata: `1`
- DB-referenced artifacts: `24`
- DB-referenced artifacts by type: `pdf=6`, `markdown=6`, `tei=6`, `docling_json=6`
- unreferenced files included: `false`

## Copy Plan Source SHA256

| Category | Relative path | Source SHA256 |
| --- | --- | --- |
| active_database | `database.sqlite` | `a1c7fb7b7a3b1beba57a532b994a8dd14d306c4b67f3a8fe842ad74fd73852df` |
| required_library_metadata | `library.json` | `6472461e7fde0409db45bb4513b797feab9b0cdf0ca2e5ceb1b3cee56702c2a8` |
| db_referenced_artifact:docling_json | `storage/docling_json/4453cee7-5b35-4ae4-a2b0-51cdf17f002b_2024-Revealing_the_16-electron_sulfur_reduction_reaction_network_in_lithium_sulfur__Li-S__batteries.docling.json` | `94c222bdaa1bfb23f11afb670676157e4b0b1f5f256b5b55ecc15bd4247ce577` |
| db_referenced_artifact:docling_json | `storage/docling_json/7ad3ecd9-64e6-4e79-8854-5848dfa5f201_2002-聚偏氟乙烯β相全反式结构链的第一性原理计算.docling.json` | `37713fe11995223c1e0a698ff8299cea8391853624a15265fec00d179c26623b` |
| db_referenced_artifact:docling_json | `storage/docling_json/7b953bee-58df-4703-80f9-30856bfdcc1d_2005-GaN中与C和O有关的杂质能级第一性原理计算.docling.json` | `8e02ceead5f810c6bb481a055ea0728b372aa21e31cd89ef797b39539c7bc8ef` |
| db_referenced_artifact:docling_json | `storage/docling_json/cf03a9b8-bf0a-4c94-bb27-dd24ac0ed5fa_2002-分子和金表面相互作用的第一性原理研究.docling.json` | `0a1ee23cf10e248bcfa94fef46d6603a817276b19a291680facd0b2269a1f638` |
| db_referenced_artifact:docling_json | `storage/docling_json/cf79612a-b912-41f2-a759-2ba4a41661c4_2022-锂硫电池非均相电催化剂.docling.json` | `918f56281648e4ae0003cfc736cecfc7e3eaacf071a631d27cdcf6e59d3e69c7` |
| db_referenced_artifact:docling_json | `storage/docling_json/dbcae8cc-b1be-4391-90ff-3c8fab2947c6_2002-四方铁电体PbFe0_5Nb0_5O3精细结构的第一性原理研究.docling.json` | `b7b29a8d599543691a6aed8542ae33c84b983c47723a555f8c1d019b9417ad28` |
| db_referenced_artifact:markdown | `storage/markdown/4453cee7-5b35-4ae4-a2b0-51cdf17f002b_2024-Revealing_the_16-electron_sulfur_reduction_reaction_network_in_lithium_sulfur__Li-S__batteries.md` | `c80cd051437804129992070c6d06ac8fb14facd126473e4b3ab216f89f935b34` |
| db_referenced_artifact:markdown | `storage/markdown/7ad3ecd9-64e6-4e79-8854-5848dfa5f201_2002-聚偏氟乙烯β相全反式结构链的第一性原理计算.md` | `f68cdf111c24c88468793cb17b4dbbe9018834255a952a18d123e7f99345f760` |
| db_referenced_artifact:markdown | `storage/markdown/7b953bee-58df-4703-80f9-30856bfdcc1d_2005-GaN中与C和O有关的杂质能级第一性原理计算.md` | `58d6e2849642fa5b3b2189f807540f7d94511964897cab3a901bb3e8f792ae6e` |
| db_referenced_artifact:markdown | `storage/markdown/cf03a9b8-bf0a-4c94-bb27-dd24ac0ed5fa_2002-分子和金表面相互作用的第一性原理研究.md` | `cc242a27b842bcea9cbc314bc4eed06dbaef92237b987ee939bef6fec256b4c9` |
| db_referenced_artifact:markdown | `storage/markdown/cf79612a-b912-41f2-a759-2ba4a41661c4_2022-锂硫电池非均相电催化剂.md` | `1439d7cd765b4ee4fc4733364bba11c9410203fafbe54238d42d55cdb5820b6b` |
| db_referenced_artifact:markdown | `storage/markdown/dbcae8cc-b1be-4391-90ff-3c8fab2947c6_2002-四方铁电体PbFe0_5Nb0_5O3精细结构的第一性原理研究.md` | `c6c54dc34ad70bdcbc898e8a425ebce82977c3c4c1595b5603b87a559aa88ed9` |
| db_referenced_artifact:pdf | `storage/pdf/4453cee7-5b35-4ae4-a2b0-51cdf17f002b_2024-Revealing_the_16-electron_sulfur_reduction_reaction_network_in_lithium_sulfur__Li-S__batteries.pdf` | `3b1a48d7dcb056b514c0e0ffe70bf996dd7bf94a730909a05cc7a99ddf9991a8` |
| db_referenced_artifact:pdf | `storage/pdf/7ad3ecd9-64e6-4e79-8854-5848dfa5f201_2002-聚偏氟乙烯β相全反式结构链的第一性原理计算.pdf` | `6e68eda39d01f41cdfff7271d80537f0f7a01e11088152aaefbda7dfaeb25db6` |
| db_referenced_artifact:pdf | `storage/pdf/7b953bee-58df-4703-80f9-30856bfdcc1d_2005-GaN中与C和O有关的杂质能级第一性原理计算.pdf` | `8530585bf54723230d1f0658280b356912414aae2d9ac7e9bea9a8533100b82b` |
| db_referenced_artifact:pdf | `storage/pdf/cf03a9b8-bf0a-4c94-bb27-dd24ac0ed5fa_2002-分子和金表面相互作用的第一性原理研究.pdf` | `468682831e3b126ca2d4184b94a25ee18a57a90202c2f24e8b3cf613b6d8638b` |
| db_referenced_artifact:pdf | `storage/pdf/cf79612a-b912-41f2-a759-2ba4a41661c4_2022-锂硫电池非均相电催化剂.pdf` | `2df4344c93989f96ac3e76f602da0fc0b3c8ba3e6ecaf08252f917a20c4418b3` |
| db_referenced_artifact:pdf | `storage/pdf/dbcae8cc-b1be-4391-90ff-3c8fab2947c6_2002-四方铁电体PbFe0_5Nb0_5O3精细结构的第一性原理研究.pdf` | `2c7a5a228aa24c2337449b9af47bc4364af24e213e74af88f873247c9e647557` |
| db_referenced_artifact:tei | `storage/tei/4453cee7-5b35-4ae4-a2b0-51cdf17f002b_2024-Revealing_the_16-electron_sulfur_reduction_reaction_network_in_lithium_sulfur__Li-S__batteries.tei.xml` | `de27bc2677f5c650729a126e5ef12983bc4eedfd81c429658d64452b03cc5bb8` |
| db_referenced_artifact:tei | `storage/tei/7ad3ecd9-64e6-4e79-8854-5848dfa5f201_2002-聚偏氟乙烯β相全反式结构链的第一性原理计算.tei.xml` | `c57b3cf4b21f72564b596def2fdf9cba91b463975209b42c819f98359c2a5bb1` |
| db_referenced_artifact:tei | `storage/tei/7b953bee-58df-4703-80f9-30856bfdcc1d_2005-GaN中与C和O有关的杂质能级第一性原理计算.tei.xml` | `5c8147e2b8f7142619eeb4b4e2bd2cc2a67be84570a7d96399be30e5d72ab3b7` |
| db_referenced_artifact:tei | `storage/tei/cf03a9b8-bf0a-4c94-bb27-dd24ac0ed5fa_2002-分子和金表面相互作用的第一性原理研究.tei.xml` | `0a33262bd35d65ee3ea7c345c2795438db37bcfc3767af5256bc79e4981f431f` |
| db_referenced_artifact:tei | `storage/tei/cf79612a-b912-41f2-a759-2ba4a41661c4_2022-锂硫电池非均相电催化剂.tei.xml` | `a5bd3aca990156b81312f12ae9d2ecf8e6e3efb44bf4b51a059d85b5bfa4a10b` |
| db_referenced_artifact:tei | `storage/tei/dbcae8cc-b1be-4391-90ff-3c8fab2947c6_2002-四方铁电体PbFe0_5Nb0_5O3精细结构的第一性原理研究.tei.xml` | `451787c2b692f539688510e2b9f03a543ffab8981189e32128f82bb9e60f6430` |

## Additional Gate Outputs

`d2_real_extraction_coverage_gate.py`:

- `mode=dry_run`
- `db_kind=sqlite`
- `papers_total=15`
- `extractable_papers_count=1`
- `extractable_dft_results_count=1`
- `apply_executed=False`
- `export_writing_gate_unchanged=True`

`audit_ai_workflow_boundary.py`:

- `active_db_kind=sqlite`
- `is_active_library_sqlite=True`
- `external_analysis_runs=0`
- `verified_reviews=0`
- `dft_export_total_candidates=0`
- `writing_cards_total=6`

## Rollback Strategy Summary

- Back up the canonical registry before any future apply.
- If target writes are partial, remove only files listed in the copy plan under the target root.
- If registry update happens and post-update validation fails, restore the registry backup before runtime traffic resumes.
- Re-run this dry-run gate and verify `target_conflicts_count=0` before any future apply.

## Explicit Non-Actions

- no `--apply` executed
- no canonical registry change
- no active SQLite move
- no real artifact copy
- no target `database.sqlite` creation
- no target `library.json` creation
- no verified review write
