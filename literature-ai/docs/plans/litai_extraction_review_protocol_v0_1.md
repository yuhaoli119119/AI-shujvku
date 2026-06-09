# LitAI Extraction And Review Protocol v0.1

## Purpose

This protocol defines the current extraction, evidence, review, DFT export, citation, and workbench behavior for `literature-ai`.

It is not a manual data-entry protocol. It is a multi-agent workbench protocol:

- The software imports papers, parses PDFs, prepares artifacts, extracts candidates, and exposes evidence.
- The user assigns AI workers per task. Codex, Gemini, GLM, Claude, or another IDE AI may act as parser, image auditor, DFT auditor, knowledge summarizer, or second-pass reviewer.
- AI outputs are candidates and audit evidence, not final truth.
- Human/final confirmation resolves disputes and controls trusted promotion.

## Core Principles

1. Automatic parser, extraction, and AI review outputs are candidates by default.
2. Every trusted field must be traceable to source evidence or explicit human/final confirmation.
3. A value visible only in an image trend is not directly exportable as a numeric database value.
4. Structured data, long text, evidence bundles, and audit records must be stored in separate layers.
5. The frontend is a workbench, not the source of truth.
6. Model names do not assign permanent jobs or trust. The assigned role for a run must be recorded in `source`, `source_label`, `agent_role`, or `model_name`.

## Dynamic Role Assignment

Examples of valid task assignments:

- "Codex parses this paper and proposes DFT candidates."
- "GLM audits figure crops and chart interpretation."
- "Gemini checks DFT rows against the evidence packet."
- "Claude writes a second-pass summary and flags missing evidence."
- "A human reviewer confirms which candidates can be trusted."

The same model may perform different roles in different runs. The system should track the role performed, not assume it from the model name.

## Status Model

Legacy workflow status names may still exist for compatibility:

```text
Imported -> Quality_Checked -> Parsed_Material_Ready -> Codex_Candidate
-> Gemini_Verified / Gemini_Revised / Gemini_Flagged / Evidence_Insufficient
-> Human_Confirmed -> ML_Ready / Citation_Ready
```

Interpretation:

- `Codex_Candidate` means "AI/system candidate", not necessarily produced by Codex.
- `Gemini_*` means "external/second AI review state", not necessarily reviewed by Gemini.
- `Human_Confirmed`, `ML_Ready`, and `Citation_Ready` are promotion states guarded by review and evidence rules.

New docs and UI copy should prefer generic labels such as "AI candidate", "external AI reviewed", "needs human confirmation", and "safe verified".

## Artifact Gate

Before any AI performs a paper-level audit, it must confirm that the workbench exposes readable artifacts:

- PDF exists and is readable.
- Markdown or Docling JSON has content.
- `by_id/<paper_id>/extraction/ai_reading_package.json` exists.
- Parsed sections, figures, tables, evidence locators, and prior audit opinions are visible when available.

If `artifact_ready_for_external_audit` is false, the correct output is `artifact_precondition_failed`, `needs_fix`, or a blocking note. The AI must not pretend it fully reviewed the paper.

## Record Granularity

Split a paper into independent records when needed:

- DFT results by material, structure, property, condition, adsorbate, and reaction step.
- Figures by figure/subfigure and evidence role.
- Tables by table and row group.
- Knowledge candidates by research gap, mechanism, method, conclusion, or writing logic.

Do not collapse multiple materials, conditions, or values into one ambiguous row.

## Evidence Requirements

Every non-empty trusted field should have:

- source text or quote
- PDF page
- section, figure, or table locator when available
- source type
- confidence
- review status

Text-only evidence can support a candidate, but exact page/table/figure locators are required before export or final trust where the relevant gate demands it.

## Figure And Image Policy

Figure records should be classified as:

- `data_figure`: may contain extractable numeric or comparative evidence.
- `knowledge_figure`: supports mechanism, structure, workflow, or interpretation.
- `invalid_crop`: header/footer/logo/noise/partial crop or unrelated image.
- `needs_review`: crop, bbox, label, axes, legend, or panel coverage must be checked.

Do not estimate precise numeric values from plots unless the value is explicitly readable in the source.

## DFT Export Policy

A DFT row can enter trusted export only when it satisfies the export gate:

- direct supporting evidence exists
- value and unit are clear
- material/adsorbate/property/reaction step are correctly normalized
- exact locator requirements are met
- review state is safe verified
- no blocking duplicate, schema, unit, or suspicious-value flags remain

Rejected, `needs_fix`, suspected duplicate, suspected missing, text-only, or unreviewed rows remain candidates.

## External AI Audit Imports

External AI audit imports through MCP/API should be stored as `external_audit_opinion` or review payload entries.

Required behavior:

- Keep `verification_status=unverified` until a later confirmation gate promotes it.
- Record `source`, `source_label`, `agent_role`, `model_name`, protocol version/hash when available, decision, reasons, and evidence location.
- Preserve prior audit opinions so later AI workers can see conflicts or agreement.
- Never let an external AI import directly unlock final export or final citation readiness.

## Review Checklist

For DFT/data records:

1. Is there direct source evidence?
2. Is the numeric value explicit?
3. Is the unit explicit and normalized correctly?
4. Can the evidence be located to page, section, table, figure, or bbox?
5. Is this only inferred from an image?
6. Is it a duplicate of another record?
7. Does the paper contain related evidence that suggests missing rows?
8. Is the row eligible for ML export after safety gates?

For knowledge/writing candidates:

1. Is this an original-paper fact, an interpretation, or an AI summary?
2. Does source text support the claim?
3. Is the category correct: gap, mechanism, method, conclusion, or writing logic?
4. Is it safe for citation support, or only useful as a drafting hint?
5. Does it need another AI or human review?

## Current Conclusion

The project uses a "structured candidates + evidence traceability + dynamic multi-agent review + human/final confirmation" protocol. It should not revert to a fixed human-entry workflow or a fixed Codex/Gemini division of labor.
