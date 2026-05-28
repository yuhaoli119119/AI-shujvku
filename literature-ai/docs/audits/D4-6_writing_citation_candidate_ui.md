# D4-6 Writing Citation Candidate UI / Web Writing Assistant Panel Audit

This audit document evaluates the design, safety features, API integration, and test coverage of the Web Writing Assistant Panel.

---

## 1. Modifications Summary

### Modified Files:
1. `frontend/shared/topnav.js` — Registered the "写作辅助" (Writing Assistant) page to allow easy visual navigation.
2. `frontend/tests/smoke.spec.js` — Added full end-to-end mock interceptors for `POST /api/writing/citation-candidates` and a comprehensive smoke test suite validating input logic, safety badges, warnings, and copy commands.

### New Files:
1. `frontend/pages/writing_assistant/index.html` — The main user panel structure.
2. `frontend/pages/writing_assistant/page.css` — Modern aesthetic styling using CSS design tokens for light/dark/eyecare modes and 6 visual themes.
3. `frontend/pages/writing_assistant/page.js` — Core page controller handling form bindings, tokenize validations, API calling, badge mapping, HTML templating, and copy logic.
4. `docs/audits/D4-6_writing_citation_candidate_ui.md` — This audit record.

---

## 2. Page & API Specifications

- **Front-end Access Path**: `http://localhost:8000/pages/writing_assistant/index.html`
- **Backend API Invoked**: `POST /api/writing/citation-candidates`
- **Request Payload Schema**:
  ```json
  {
    "text": "Single-atom catalysts can accelerate sulfur redox kinetics in lithium-sulfur batteries.",
    "max_candidates": 10,
    "filters": {
      "year_min": 2022,
      "impact_factor_min": 10.0,
      "citation_priority": "high",
      "has_pdf": true
    },
    "include_unverified_suggestions": true,
    "include_pending_review": true
  }
  ```

---

## 3. Safety Guardrails & Safety Badge Classifications

To satisfy the strict safety criteria, the writing assistant panel has zero-tolerance rules for active DB writes or fake evidence representation:

### Safety Tag Mapping
| Scenario | Condition | Badge Color & Wording | Page Class |
| :--- | :--- | :--- | :--- |
| **Confirmed Citation** | `can_be_used_as_confirmed_citation === true` | Green Badge: **Confirmed citation candidate** | `.border-confirmed` |
| **Needs Verification** | `requires_human_verification === true` and `evidence_status !== 'metadata_only'` | Amber/Orange Badge: **Needs human verification** | `.border-needs-verification` |
| **Metadata-only** | `evidence_status === 'metadata_only'` | Grey Badge: **Metadata-only suggestion — cannot be used as evidence yet** | `.border-metadata-only` |

### Key Safety Guardrails Implemented:
1. **No Backend DB Writes / Read-Only Panel**: The page does not call, reference, or support write requests like `mark_verified`, `reviewer_status=verified`, `verified=true`, or `save_reviews`. All actions are pure local queries.
2. **Prominent Safety Disclaimers**: Displays a highlighted sticky banner explicitly warning users that unverified suggestions or items requiring human verification must not be treated as reliable evidence.
3. **Prominent Warning Visibility**: If `warnings` are returned by the API (e.g. `suggestion_only_needs_human_verification` or `impact_factor_needs_metadata`), they are displayed in bold red warning containers directly inside the paper card.
4. **Copy Security Guardrail**: There is **no** automatic citation insertion button. Instead, a "Copy Candidate Info" button is provided. The copied string explicitly forces inclusion of the `evidence_status` and all associated verification warnings (e.g. `Verification Warning: suggestion_only_needs_human_verification`) to prevent misleading reference insertions.
5. **Validation Guardrail**: Short inputs (fewer than 2 searchable terms after stopwords removal) are blocked locally, avoiding unnecessary API overhead and prompting the user with a clean helper message.

---

## 4. Audit Checklist

| Audit Question | Status | Details |
| :--- | :--- | :--- |
| **Did we touch active DB / write to DB?** | ❌ **No** | Full read-only lookup query. No writes. |
| **Did we touch backend python files?** | ❌ **No** | Changes restricted entirely to `frontend/` and `docs/audits/`. |
| **Did we delete papers?** | ❌ **No** | No delete buttons or backend delete references included. |
| **Did we touch migrations or schema?** | ❌ **No** | Absolutely no Alembic/SQLAlchemy changes. |
| **Did we touch registry or materialization?** | ❌ **No** | Read-only search API consumption. |
| **Did we unlock export/writing gates?** | ❌ **No** | Checked in Playwright smoke tests that no unlocking occurs. |
| **Did we misrepresent metadata suggestions?** | ❌ **No** | Labeled strictly as "Metadata-only suggestion — cannot be used as evidence yet". |

---

## 5. Playwright Testing & Verification

- **Smoke Test Command**: `npm test -- --project=chromium` (executed in `/frontend/` folder)
- **Test Strategy**:
  - Validates correct rendering of layout and safety disclaimers.
  - Verifies local input validations for empty or keyword-less submissions.
  - Intercepts and mocks `POST /api/writing/citation-candidates` to serve three candidate papers (Confirmed green badge, Needs verification amber badge, Metadata-only grey badge) and one excluded paper reason.
  - Verifies all three badges are rendered correctly with their respective theme border classes.
  - Asserts warnings are prominently displayed.
  - Asserts excluded candidates are isolated into a separate collapsible container.
  - Asserts no DB writes or auto-inserters exist on the page.

---

## 6. Remaining Risks & Conclusions

- **Remaining Risks**: None. Since the UI contains absolutely no buttons or scripts calling any state-modifying backend APIs, there is zero risk of database corruption, data leakages, or security-gate bypasses.
- **Conclusion**: The Web Writing Assistant Panel meets all D4-6 UI/UX specifications, aligns with the theme styles, enforces robust safety tags, and has extensive mock smoke test coverage.
