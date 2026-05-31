# D5-3A Evidence-Backed Writing Cards: Preflight & Read-only Skeleton Plan

## Goals
- Design the evidence-backed writing cards feature.
- Build a read-only backend skeleton for evaluating citation candidates/evidence and generating writing cards.
- Enforce strict safety rules to distinguish between `safe_verified` evidence and other statuses (`metadata_only`, `pending`, `unverified`).

## Non-Goals
- Final writing card export/writing.
- Database writes (no session commits, mutations).
- Modifying the D4-8 verification gate.
- Generating a bibliography.
- Modifying frontend UI or exposing new API endpoints (locked behind backend skeleton).
- Auto-inserting text into the manuscript.

## Inputs & Outputs
- **Input**: A list of `evidence_items` or `candidates`. Each item is expected to contain:
  - `title`: Title of the source paper.
  - `evidence_status`: The safety status of the evidence (`safe_verified`, `verified`, `pending`, `metadata_only`, `unverified`, etc.).
  - `warnings`: Existing warnings from previous processing stages.
  - `evidence_text` / `draft_text`: The claim or draft text the card is based on.
  - `source_locator`: Where the evidence comes from.
- **Output**: A list of `writing_cards` and a global `safety_guardrails` object.
  - `card_type`: The resulting status of the writing card (`confirmed_writing_card` vs `suggestion_only`).
  - `status`: Kept alongside card_type for clarity.
  - `claim_text` or `draft_text`: Text for the card.
  - `source_title`: The paper title.
  - `evidence_status`: Passed through from input.
  - `warnings`: Accumulated warnings.
  - `safety_guardrails`: Card-level hardcoded booleans confirming no side effects (`writes_db=false`, `auto_insert=false`, `generates_bibliography=false`, `export_unlocked=false`, `verified_status_changed=false`).

## Safety Rules & Status Promotion
1. **`safe_verified` -> `confirmed`**: 
   - Only when an input candidate has `evidence_status == "safe_verified"`, the card can be assigned `status = "confirmed_writing_card"` and `can_be_used_as_confirmed_fact = true`.
2. **Conservative fallback for `verified`**:
   - `verified` (without `safe_`) is explicitly treated as unsafe for confirmed cards. It falls back to `suggestion_only` / `needs_human_verification` and `can_be_used_as_confirmed_fact = false`.
3. **All other statuses -> `suggestion_only`**:
   - `metadata_only`, `pending`, `unverified`, `unknown` remain `status = "suggestion_only"` or `needs_human_verification`, and `can_be_used_as_confirmed_fact = false`.
4. **Warning Passthrough**:
   - All candidate warnings are passed through. If the status is not `safe_verified`, a `human_verification_required` (or similar) warning must be appended.

## API Draft (For Future)
```http
POST /api/writing/evidence-backed-cards/generate
Content-Type: application/json

{
  "candidates": [
    {
      "title": "Paper Title",
      "evidence_status": "safe_verified",
      "draft_text": "Claim text based on the paper.",
      "warnings": []
    }
  ]
}
```
*Response*:
```json
{
  "writing_cards": [ ... ],
  "safety_guardrails": { ... }
}
```

## Frontend Draft (For Future)
- A new section in the Writing Assistant UI to display "Writing Cards".
- A card visually distinguishes between a "Confirmed Fact" (green/check) and a "Suggestion" (yellow/warning).
- Action buttons for suggestions will prompt for "Verify Evidence" instead of "Use".

## Test Plan
- **Test 1**: Empty input returns safe empty list without error.
- **Test 2**: `safe_verified` candidate produces a `confirmed_writing_card` with `can_be_used_as_confirmed_fact = True`.
- **Test 3**: Non-`safe_verified` candidates (`metadata_only`, `pending`, `unverified`, `verified`) produce `suggestion_only` cards with `can_be_used_as_confirmed_fact = False`.
- **Test 4**: Warnings are passed through, and `needs_human_verification` warning is appended for unsafe candidates.
- **Test 5**: Safety guardrails are consistently returned as `False` (read-only, no DB write, no export).

## Risk List
- **Risk**: Other components might mistakenly assume `verified` is enough for a confirmed card.
  - **Mitigation**: Enforce the `safe_verified` check explicitly in this service.
- **Risk**: Writing cards might be accidentally saved to the database.
  - **Mitigation**: The skeleton service does not accept a DB session, ensuring it remains pure.
