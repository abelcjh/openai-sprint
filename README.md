# Invoice Register Agents SDK Demo

A sprint-ready demo for Malaysian SMEs: drop in messy invoice sources
(PDFs, receipt photos, or WhatsApp-style text) and turn them into one
central invoice register with confidence, duplicate warnings, and a human
review queue.

The app is intentionally small:

- FastAPI backend
- SQLite persistence
- Server-rendered dashboard
- OpenAI Agents SDK workflow when `OPENAI_API_KEY` is present
- Deterministic local fallback so the demo and tests still work offline

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

To use the live Agents SDK path, set:

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-5.5
```

If the SDK or API key is missing, the app uses a deterministic extractor
that is good enough for the sprint demo and test suite.

## Automatic Connectors

The app now supports manual ingestion, email polling, and WhatsApp webhook
ingestion. Every connector uses the same invoice pipeline:

```text
source channel -> raw artifact store -> Agents SDK workflow -> review guardrails -> invoice register
```

### Email Inbox Polling

Configure an IMAP inbox in `.env`:

```bash
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_IMAP_PORT=993
EMAIL_IMAP_USER=finance@example.com
EMAIL_IMAP_PASSWORD=app-password-here
EMAIL_IMAP_MAILBOX=INBOX
EMAIL_IMAP_SEARCH=UNSEEN
EMAIL_IMAP_LIMIT=5
```

Then use the dashboard's **Poll Inbox** button. The connector scans unread
messages and ingests:

- PDF attachments as `pdf`
- receipt images as `image`
- plain invoice emails as `text`

The connector marks fetched messages as seen after processing, so the inbox is
not repeatedly reprocessed during demos.

### WhatsApp Webhook

The app exposes this inbound endpoint:

```text
POST /webhooks/whatsapp
```

Use this full local URL while tunneling with a tool such as ngrok:

```text
http://127.0.0.1:8000/webhooks/whatsapp
```

For Meta-style webhook verification, set:

```bash
WHATSAPP_VERIFY_TOKEN=choose-a-demo-token
```

The webhook accepts:

- WhatsApp text bodies
- PDF media URLs
- image media URLs
- Twilio-style form webhooks with `Body`, `NumMedia`, `MediaUrl0`, and
  `MediaContentType0`
- Meta-style JSON webhooks with `entry[].changes[].value.messages[]`

For a no-setup sprint demo, use the dashboard's **Simulate WA** button. It
posts a realistic WhatsApp invoice message into the same webhook-grade ingestion
path without needing a live WhatsApp Business account.

### Frontend Motion Design

The dashboard is intentionally more than a CRUD table now:

- animated source-to-register hero pipeline
- floating source nodes for email, WhatsApp, and receipt photos
- animated Agents SDK core showing orchestration
- connector cards for email and WhatsApp
- confidence rings per invoice
- expandable agent traces per register row

The motion is pure CSS in `static/app.css`, with `prefers-reduced-motion`
support for accessibility.

## Demo Script

Use these exact examples during the sprint demo. Do not invent examples live.

### 1. Register A Clean WhatsApp Invoice

Paste this into the "WhatsApp or text invoice" box and click **Ingest
Message**:

```text
Supplier: Kopi Kita Sdn Bhd
Reg No: 202001234567
Invoice No: INV-2026-0512
Date: 12/05/2026
Kopi beans 5kg RM 180.00
Paper cups RM 42.00
Subtotal: RM 222.00
SST: RM 13.32
Total: RM 235.32
```

Expected result:

- status: `registered`
- supplier: `Kopi Kita Sdn Bhd`
- invoice number: `INV-2026-0512`
- date: `2026-05-12`
- total: `MYR 235.32`

What to say:

> This started as a WhatsApp-style supplier message. The agents converted it
> into a structured register row with supplier, invoice number, date, tax, total,
> confidence, and trace.

### 2. Trigger Duplicate Review

Paste the exact same invoice again and click **Ingest Message**:

```text
Supplier: Kopi Kita Sdn Bhd
Reg No: 202001234567
Invoice No: INV-2026-0512
Date: 12/05/2026
Kopi beans 5kg RM 180.00
Paper cups RM 42.00
Subtotal: RM 222.00
SST: RM 13.32
Total: RM 235.32
```

Expected result:

- status: `needs_review`
- review reason includes `Possible duplicate invoice found`
- trace includes `DuplicateRiskAgent checked`

What to say:

> The system does not blindly write duplicates. The duplicate agent checks the
> existing register and pushes risky rows into human review instead of
> overwriting anything.

### 3. Trigger Missing-Field Review

Paste this incomplete supplier message and click **Ingest Message**:

```text
From: Maju Trading Enterprise
Date: 13/05/2026
Need to pay for cleaning supplies
Detergent RM 48.00
Mop heads RM 32.00
Total: RM 80.00
```

Expected result:

- status: `needs_review`
- missing field includes `invoice_no`
- review reason includes `Missing required fields`

What to say:

> This is exactly how SME records get messy. The system extracts what it can,
> but refuses to auto-register because the invoice number is missing.

### 4. Trigger Rejection For Non-Invoice Text

Paste this and click **Ingest Message**:

```text
Birthday lunch agenda for the team.
Remember to check the weather and book a table near the office.
No invoice attached.
```

Expected result:

- status: `rejected`
- review reason says the input does not look like an invoice, receipt, bill, or
  order request

What to say:

> The register is not a generic note dump. Non-invoice content is rejected so the
> single source of truth stays clean.

### 5. Demonstrate PDF Or Receipt Photo Upload

Use any PDF or receipt image from your machine. The sprint prompt images are
acceptable as a physical-document-style upload if you do not have a real invoice
sample:

- `/mnt/c/Users/Abel Chin/Downloads/WhatsApp Image 2026-05-12 at 10.17.25 AM.jpeg`
- `/mnt/c/Users/Abel Chin/Downloads/WhatsApp Image 2026-05-12 at 10.17.25 AM (1).jpeg`

Expected result without `OPENAI_API_KEY`:

- status: usually `needs_review`
- trace says local fallback was used
- file is still stored as a raw artifact

Expected result with `OPENAI_API_KEY`:

- the live Agents SDK path sends PDFs as `input_file`
- receipt photos/images are sent as `input_image`
- the PDF/image specialist agent extracts the register draft
- uncertain OCR or missing fields go to `needs_review`

What to say:

> This proves the register is source-agnostic. Messages, PDFs, and physical
> invoice photos all land in the same invoice table with the same review rules.

### 6. Approve And Export

For any `needs_review` row that looks correct:

1. Click **Approve**.
2. Click **Export CSV**.
3. Open the downloaded CSV and show the normalized register columns.

Expected CSV columns:

```text
id,status,supplier_name,invoice_no,issue_date,currency,total_amount,confidence,review_reasons
```

The positioning line:

> This becomes the default invoice truth layer before accounting, tax
> filing, and e-invoicing workflows.

## How This Uses OpenAI's Agents SDK Non-Trivially

This is not implemented as a single "extract this invoice as JSON" prompt.
The live path in `app/agent_workflow.py` builds an agentic workflow around the
invoice register domain:

1. Detect whether the OpenAI Agents SDK path is available.
2. Build a central orchestrator agent.
3. Expose multiple specialist agents as tools.
4. Give the workflow real application tools for duplicate lookup and schema
   validation.
5. Force the final result into a typed `InvoiceDraft`.
6. Apply deterministic review guardrails before anything becomes a trusted
   register row.
7. Persist a local trace that mirrors the judge-visible agent steps, while the
   SDK also emits its own OpenAI dashboard trace when live.

The SDK matters because the task has multiple subtasks with different failure
modes: PDF extraction, image extraction, WhatsApp/text interpretation,
normalization, duplicate detection, and review classification. A single prompt
would blur those responsibilities together. The Agents SDK lets the app model
them as separate agents and tools while keeping one canonical output.

### SDK Primitives Used

The implementation imports the SDK inside `_run_agents()`:

```python
from agents import Agent, Runner, function_tool
```

Those primitives are used in three layers:

- `Agent`: defines the orchestrator and all specialist workers.
- `Agent.as_tool()`: exposes each specialist as a callable tool to the
  orchestrator.
- `function_tool`: exposes application code, such as duplicate lookup, to the
  agent workflow.
- `Runner.run()`: executes the full multi-agent loop.
- `output_type=InvoiceDraft`: makes the final agent output structured and
  schema-bound through the Pydantic model in `app/models.py`.

OpenAI's Agents SDK is designed around agents with tools, agents-as-tools or
handoffs, guardrails, and tracing. This app uses those ideas directly: the
manager agent owns the final invoice row, specialist agents perform bounded
subtasks, function tools connect to the register database, and the workflow
generates inspectable trace steps.

### Agent Topology

The central agent is `InvoiceRegisterOrchestrator`. It is deliberately the only
agent that owns the final answer because the product promise is a single source
of truth, not a set of separate AI opinions.

The orchestrator receives:

- the source type: `pdf`, `image`, or `text`
- the filename
- the raw file payload or message text
- instructions to produce exactly one canonical `InvoiceDraft`

It can call these specialist agents:

- `PdfInvoiceAgent`: extracts fields from PDF invoices.
- `ImageInvoiceAgent`: reads physical invoices and receipt photos.
- `MessageInvoiceAgent`: interprets WhatsApp, SMS, and plain text invoice
  messages.
- `NormalizerAgent`: normalizes dates, `RM` to `MYR`, supplier naming, and
  Malaysian SME register fields.
- `DuplicateRiskAgent`: checks whether a similar invoice already exists.
- `ReviewDecisionAgent`: decides whether the row is `registered`,
  `needs_review`, or `rejected`.

Each specialist is defined as an `Agent` with focused instructions and
`output_type=InvoiceDraft`. The orchestrator receives those specialists through
`as_tool()`:

```python
pdf_agent.as_tool(
    tool_name="extract_pdf_invoice",
    tool_description="Extract a canonical invoice draft from a PDF invoice.",
)
```

That is the important architectural move. The orchestrator does not just
"think harder"; it has named capabilities that match real invoice operations.
This makes the judging story stronger because every visible trace step maps to
a real workflow responsibility.

### Manager Pattern Instead Of Handoffs

The app uses a manager/orchestrator pattern rather than handoffs. In a handoff
workflow, a specialist can take over the conversation. That is useful for
customer support, but here it would be the wrong shape: a PDF specialist should
not own the final database row, and a duplicate-checking specialist should not
decide the whole user experience.

Instead, `InvoiceRegisterOrchestrator` keeps control and calls specialists as
tools. This matches the product requirement:

- one final invoice record
- one confidence value
- one status
- one review decision
- one register write

This also makes the UI easier to explain during judging: "The central register
agent calls the right specialists, then writes one governed invoice row."

### Structured Output With `InvoiceDraft`

The core schema is `InvoiceDraft` in `app/models.py`. It includes:

- required register fields: `supplier_name`, `invoice_no`, `issue_date`,
  `currency`, `total_amount`, `source_type`, `confidence`, `status`
- Malaysia-relevant optional fields: `supplier_registration_no`,
  `sst_or_tax_amount`, `subtotal`, `due_date`
- workflow fields: `line_items`, `duplicate_candidates`, `missing_fields`,
  `review_reasons`

Every live specialist and the orchestrator use:

```python
output_type=InvoiceDraft
```

This matters because the downstream app is not consuming prose. It needs a
database-safe register row. The structured output contract means the agent
workflow must produce something the Python app can validate, store, render, and
export.

The app still validates the final result defensively:

```python
draft = result.final_output
if not isinstance(draft, InvoiceDraft):
    draft = InvoiceDraft.model_validate(draft)
```

So even if the SDK returns a dictionary-like shape, the app brings it back into
the canonical Pydantic model before applying review rules or writing to SQLite.

### Function Tools Connected To Product State

The workflow exposes real application capabilities to the agents via
`@function_tool`.

The most important tool is `find_similar_invoices()`:

```python
@function_tool
def find_similar_invoices(
    supplier_name: str,
    invoice_no: str,
    total_amount: float | None,
    issue_date: str,
) -> str:
    """Return possible duplicate invoices from the register."""
    matches = db.find_similar_invoices(...)
    return json.dumps([match.model_dump() for match in matches])
```

This is non-trivial because the model is not only extracting text. It can query
the actual invoice register to decide whether a new row should be trusted or
sent to review. That makes the demo materially better than OCR-plus-JSON:

- first upload: row can become `registered`
- second upload of the same invoice: row becomes `needs_review`
- dashboard explains why: "Possible duplicate invoice found"

There is also `create_invoice_draft()`, which acts as a schema-validation tool
inside the workflow. It accepts JSON, attaches confidence and review reasons,
then constructs an `InvoiceDraft`. This gives the agent a product-native way to
validate a draft rather than just narrating one.

### File And Image Inputs

`_agent_input()` converts each source into the appropriate model input shape.

For text/WhatsApp messages, the input is a direct text prompt:

```python
return f"{prompt}\n\n{text}"
```

For PDFs, the file is base64 encoded and passed as an `input_file` item:

```python
{
    "type": "input_file",
    "filename": filename,
    "file_data": f"data:{mime_type};base64,{encoded}",
}
```

For physical invoice photos and receipt images, the file is base64 encoded and
passed as an `input_image` item:

```python
{
    "type": "input_image",
    "image_url": f"data:{mime_type};base64,{encoded}",
}
```

That gives the live agent path the intended multimodal behavior: PDFs, images,
and messages all enter the same register workflow, but they are represented
with the right input type for the source.

### Guardrails And Human Review

The strongest product guardrail is in `InvoiceDraft.enforce_review_rules()`.
This runs after the live SDK result and after local fallback extraction. The
rules are intentionally deterministic:

- never auto-register when `supplier_name` is missing
- never auto-register when `invoice_no` is missing
- never auto-register when `issue_date` is missing
- never auto-register when `total_amount` is missing
- send possible duplicates to `needs_review`
- send low-confidence results below `0.72` to `needs_review`
- preserve `rejected` when the input does not look like an invoice

This is the product's safety layer. The agents can extract and reason, but the
application owns the final trust boundary before a row is accepted into the
register. That is exactly what a real SME workflow needs: useful automation
without silent bookkeeping damage.

### Tracing And Judge-Visible Observability

When `OPENAI_API_KEY` is set and the SDK path runs, the Agents SDK emits its
own built-in traces for the run. In parallel, this app stores a product-level
trace in SQLite as `TraceStep` records.

The local trace is what the dashboard renders under "Agent trace and review
reasons." It includes steps like:

- `InvoiceRegisterOrchestrator started`
- `MessageInvoiceAgent extracted`
- `NormalizerAgent normalized`
- `DuplicateRiskAgent checked`
- `ReviewDecisionAgent decided`
- `RegisterStore saved`

This is important for the sprint because the judges can see the system working
as a workflow, not as a black box. The dashboard trace also remains useful when
the local fallback path runs without an API key.

### Resilient Offline Fallback

`ingest_invoice()` chooses the live Agents SDK path only when both conditions
are true:

```python
if os.getenv("OPENAI_API_KEY"):
    import agents
```

If the SDK package, network, model call, or API key is unavailable, the app
falls back to `extract_invoice_locally()` in `app/heuristics.py`.

That fallback is not the product vision; it is a sprint reliability feature. It
keeps the demo and tests running even in a venue with weak internet or missing
credentials. The trace explicitly says when fallback was used, so the app does
not pretend that deterministic parsing is the live AI workflow.

### Why This Is More Than A Wrapper Around A Prompt

The solution uses the Agents SDK to coordinate a real workflow:

- different agents own different invoice-processing responsibilities
- the orchestrator chooses and combines specialist outputs
- function tools connect agents to live register state
- structured output creates a typed contract between AI and the database
- deterministic review rules protect the business workflow
- trace data makes the workflow inspectable

The result is a working invoice register that can ingest messy SME inputs and
produce governed accounting-ready records. That is the "purposeful use of AI"
story for the sprint: AI is not decorative; it is doing the hard translation
from scattered Malaysian SME invoice reality into a trusted operational system.

## Tests

```bash
pytest
```
