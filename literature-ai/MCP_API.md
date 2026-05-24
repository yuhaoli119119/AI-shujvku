# Literature AI MCP API Guide

## Overview

`literature-ai` now exposes an MCP server at:

```text
http://localhost:8000/mcp
```

Transport mode:

- Streamable HTTP

Authentication:

- `Authorization: Bearer <MCP_API_KEY>`

---

## Environment setup

Set at least:

```env
LITAI_MCP_ENABLED=true
LITAI_MCP_SERVER_NAME=Literature AI MCP
LITAI_MCP_API_KEYS=claude|Claude Desktop|litmcp_claude|read_papers,append_notes,propose_corrections,request_parse;admin|Admin|litmcp_admin|read_papers,append_notes,propose_corrections,request_parse,review_corrections
```

Entry format:

```text
source_prefix|display_name|raw_api_key|capability1,capability2
```

Supported capabilities:

- `read_papers`
- `append_notes`
- `propose_corrections`
- `request_parse`
- `review_corrections`

---

## Available tools

### Reader / contributor tools

- `query_papers`
- `get_paper`
- `list_notes`
- `append_note`
- `propose_correction`
- `parse_paper`
- `get_parse_status`
- `scan_local_pdfs`
- `ingest_pdf_batch`

### Reviewer tools

- `get_correction_queue`
- `get_correction_detail`
- `approve_correction`
- `reject_correction`

---

## Correction target formats

### Top-level paper fields

Use the field name directly:

```text
abstract
title
journal
```

### Structured extracted records

Use:

```text
<collection>:<row_id>:<field>
```

Examples:

```text
dft_results:550e8400-e29b-41d4-a716-446655440000:value
mechanism_claims:550e8400-e29b-41d4-a716-446655440001:claim_text
electrochemical_performance:550e8400-e29b-41d4-a716-446655440002:capacity_value
catalyst_samples:550e8400-e29b-41d4-a716-446655440003:coordination
dft_settings:550e8400-e29b-41d4-a716-446655440004:functional
writing_cards:550e8400-e29b-41d4-a716-446655440005:research_gap
```

---

## Recommended review workflow

1. `query_papers`
2. `get_paper`
3. `append_note` for soft concerns
4. `propose_correction` for concrete changes
5. Reviewer uses `get_correction_queue`
6. Reviewer uses `get_correction_detail`
7. Reviewer calls `approve_correction` or `reject_correction`

## Local IDE batch PDF workflow

### Step 1: scan a local folder

Use:

- `scan_local_pdfs(folder_path, recursive=true, limit=100)`

This returns:

- local PDF path
- filename
- whether it is already ingested
- linked `paper_id` if already parsed

### Step 2: batch ingest only unparsed files

Use:

- `ingest_pdf_batch(folder_path, recursive=true, limit=20, only_unparsed=true)`

This returns per file:

- `already_ingested`
- `completed`
- `failed`

and includes `paper_id` / `job_id` when available.

### Step 3: hand off to reviewer AI

The reviewer AI can then use:

- `query_papers`
- `get_paper`
- `get_correction_queue`
- `get_correction_detail`
- `approve_correction`

### Notes

- local folder scanning depends on the MCP server process having filesystem access to that folder
- parsed local files are recognized through stored `source_path`
- `only_unparsed=true` is recommended for routine batch runs

---

## Example: propose a DFT correction

```json
{
  "paper_id": "PAPER_UUID",
  "field_name": "dft_results",
  "target_path": "dft_results:RESULT_UUID:value",
  "operation": "replace",
  "proposed_value": -1.45,
  "reason": "Cross-check with table value in the source PDF.",
  "evidence_payload": {
    "page": 6,
    "section_title": "DFT Results",
    "quoted_text": "Li2S4 adsorption energy on Fe-N4 is -1.45 eV."
  }
}
```

---

## Example: review a pending correction

Reviewer flow:

1. `get_correction_queue`
2. `get_correction_detail`
3. inspect:
   - `current_value`
   - `proposed_value`
   - `reason`
   - `evidence_payload`
4. `approve_correction` or `reject_correction`

---

## HTTP admin endpoints

In addition to MCP, the backend also exposes review endpoints:

- `GET /api/corrections`
- `GET /api/corrections/{correction_id}`
- `POST /api/corrections/{correction_id}/approve`
- `POST /api/corrections/{correction_id}/reject`

These use the same Bearer key capability model.

---

## Client examples

### Claude Desktop

Example local MCP config:

```json
{
  "mcpServers": {
    "literature-ai": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer litmcp_claude"
      }
    }
  }
}
```

### Cursor / custom MCP client

Point the client to:

```text
http://localhost:8000/mcp
```

and send:

```text
Authorization: Bearer <your_key>
```

---

## Operational notes

- Keep contributor keys and reviewer keys separate
- Reserve `review_corrections` for trusted reviewer agents only
- Prefer `append_note` for ambiguous issues and `propose_correction` for concrete changes
- Use `get_correction_detail` before approval to avoid blind merges

## External AI import workflow

If an external AI cannot directly call MCP, you can still use the local backend as the system of record:

1. upload or paste the external AI result into `POST /api/external-analysis/import`
2. the backend stores:
   - raw external text / payload
   - normalized candidate items
   - candidate types: `note`, `correction`, `relationship`, `unmapped`
3. inspect the imported run through:
   - `GET /api/external-analysis/runs`
   - `GET /api/external-analysis/runs/{run_id}`
4. materialize the accepted candidates into local records through:
   - `POST /api/external-analysis/runs/{run_id}/materialize`

This lets web GPT or other outside tools participate in review without writing directly to the main structured tables.
