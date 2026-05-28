# D4-7C to D5 Future Roadmap

## 0. Current Stable Baseline

* Current commit baseline: 01020ad0b055fbb10ceabadaab788a9953232521
* Current completed stage: D4-7B Writing Assistant UI / Citation Insertion Draft Proposal Integration
* Current system capability:

  * User can paste manuscript sentence/paragraph.
  * System can recommend candidate papers through POST /api/writing/citation-candidates.
  * UI shows evidence safety status:

    * confirmed candidate draft only when backend says safe_verified
    * needs human verification
    * metadata-only suggestion
  * User can request draft citation proposal through POST /api/writing/citation-insertion-draft.
  * System returns proposal_status, draft_text, warnings, checklist, and blocked_actions.
* Current safety boundary:

  * No automatic final citation insertion.
  * No bibliography generation.
  * No export/writing unlock.
  * No verified/safe_verified write.
  * No DB write from Writing Assistant flow.
  * Pending, repaired locator, unverified extraction, and metadata-only suggestions are never treated as verified evidence.

## 1. Immediate User Validation Plan

Before continuing feature development, manually validate the current system:

1. Start backend on canonical machine.
2. Open:
   http://localhost:8000/pages/writing_assistant/index.html
3. Paste:
   Single-atom catalysts can accelerate sulfur redox kinetics in lithium-sulfur batteries.
4. Click candidate search.
5. Confirm candidate cards render correctly.
6. Confirm unverified_extraction and metadata_only are not shown as confirmed evidence.
7. Click Generate Draft Citation Proposal.
8. Confirm proposal_status is safe, usually needs_human_verification.
9. Confirm warnings, checklist, and blocked_actions are visible.
10. Confirm there is no Insert Citation / Generate Bibliography / Copy Final Citation misleading action.

## 2. D4-7C Final Writing Assistant Acceptance Audit

Goal:
Perform one lightweight end-to-end acceptance pass after user manual validation.

Scope:

* Docs and maybe small UI copy polish only.
* No new backend behavior.
* No DB write.

Validation:

* Playwright full smoke.
* Focused Writing Assistant test.
* Real backend smoke against canonical DB.
* Active DB before/after count comparison.

Exit criteria:

* User confirms the current Writing Assistant is usable.
* No dangerous network calls.
* No DB mutation.
* UI wording is clear enough for non-engineering use.

## 3. D4-8 Zotero / BibTeX / CSL Compatibility Gate

Goal:
Add read-only citation metadata compatibility preview.

Important:
This is not final bibliography generation yet.

Possible API:
GET /api/library/papers/{paper_id}/citation-metadata-preview

Possible output:

* paper metadata preview
* BibTeX draft
* CSL JSON draft
* missing metadata warnings
* citation safety status
* evidence status

Safety:

* Label all outputs as draft metadata only.
* Do not mark citation as verified.
* Do not generate final bibliography.
* Do not unlock export/writing.
* Missing metadata must be surfaced as warning, not silently filled.

## 4. D4-9 Metadata Completion Workflow

Goal:
Help user identify papers that lack citation metadata.

Scope:

* title
* authors
* journal
* year
* DOI
* volume/issue/pages
* publisher
* impact factor metadata if user imports it

Safety:

* User-provided or imported metadata should be marked by source.
* No online scraping unless explicitly approved later.
* No automatic trust upgrade from metadata completeness.

## 5. D4-10 Human Verification Promotion Workflow

Goal:
Create a controlled workflow to promote reviewed evidence from pending/unverified to verified/safe_verified.

This must be handled with maximum caution.

Required gates:

* human review UI
* original source locator visible
* explicit user confirmation
* before/after DB backup
* audit log
* narrow write scope
* no bulk accidental promotion
* safe_verified should require stricter criteria than verified

Safety:

* repaired locator is not verified by itself.
* unverified extraction is not evidence by itself.
* metadata-only is never evidence.
* verified does not automatically mean safe for writing.

## 6. D5 Writing Workflow Expansion

Possible future modules:

### D5-1 Manuscript Comment Assistant

* user pastes paragraph
* system suggests comments
* links suggestions to candidate papers
* no automatic claims

### D5-2 Draft Revision Assistant

* improve clarity and academic style
* flag unsupported claims
* recommend candidate papers
* keep citation safety state visible

### D5-3 Evidence-backed Writing Cards

* only safe_verified evidence can become confirmed writing card
* pending/unverified cards remain suggestion-only

### D5-4 Export Preparation

* only after safe_verified evidence exists
* bibliography generation remains gated
* export/writing unlock requires separate explicit approval

## 7. Long-term Principles

* Never trade safety for convenience.
* Never silently upgrade evidence status.
* Never treat metadata completeness as source support.
* Never hide warnings behind UI polish.
* Every write to canonical DB must be intentional, documented, and auditable.
* Prefer draft/proposal states before final states.
* User remains final reviewer for evidence and citation decisions.
