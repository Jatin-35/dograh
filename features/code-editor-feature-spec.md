# Feature Spec: Code Editor (Custom Functions + Tool Calling)

## Context
We're adding a "Code Editor" module to our platform, inspired by how Heltar does it. This lets a user define chatbots (JSON config), define callable functions/tools (JSON schema), and write the actual Python logic for those functions in one central handler file. Read the attached reference doc for the exact UX and file conventions before writing code — replicate the *concepts*, not literally copy their product.

## Goal
A cloud-based mini-IDE inside our platform where a user can:
1. Define one or more chatbot configs (model, system prompt, temperature, linked functions).
2. Define one or more function/tool schemas (OpenAI-compatible function-calling JSON format).
3. Write the actual Python implementation of those functions in a single routed entry-point handler.
4. Test-run the code against a sample payload before deploying.
5. Version and deploy — deploying syncs chatbots/functions into our existing DB/Agent tables.

## Hard Requirements

### 1. Virtual file structure
NEVER let the user manage real files on disk directly — ALWAYS represent this as a virtual folder tree stored in our DB (per-org), rendered in a Monaco-based editor (same as VS Code).

```
/
├── all_events_entry_point.py     <- single Python handler, one per org
├── chatbot/                      <- one JSON file per chatbot
│   └── <bot_name>.json
└── function_definitions/         <- one JSON file per function/tool
    └── <function_name>.json
```

RIGHT: one Python handler routes on `function_name`.
WRONG: one Lambda/file per function. That fragments logic and kills the "single router" testability model — do not do this.

### 2. Chatbot JSON schema
Required fields: `id` (UUID, immutable, used to sync/match on deploy), `name` (unique).
Optional: `model`, `systemPrompt`, `temperature`, `maxTokens`, `contextLength`, `hasTimeout`, `timeoutDuration`, `functions` (array of function names).

NEVER auto-generate a new `id` on every save — ALWAYS keep the same `id` once created, or deploy will create a duplicate chatbot instead of updating the existing one.

### 3. Function definition JSON schema
MUST follow OpenAI's function-calling schema format exactly: `name`, `description`, `strict` (optional), `parameters` (JSON Schema object with `type`, `properties`, `required`, `additionalProperties`).

RIGHT:
```json
{
  "name": "get_order_status",
  "description": "Gets the current status of a customer order",
  "strict": true,
  "parameters": {
    "type": "object",
    "properties": {
      "order_id": { "type": "string", "description": "The order ID like ORD-12345" }
    },
    "additionalProperties": false,
    "required": ["order_id"]
  }
}
```

WRONG: putting `type` or `required` at the wrong nesting level (e.g. `required` inside `properties`). Validate every schema against a JSON-meta-schema before allowing save — reject with a clear error if invalid.

RULE: filename must equal `name` exactly (snake_case). NEVER allow filename/name mismatch — that breaks lookups on deploy.

NEVER require the user to include `function_name` or a `business` object inside the schema's `parameters`. ALWAYS auto-inject these two keys into the payload at call-time, after the LLM emits its tool call and before it reaches the handler:
```json
{
  "function_name": "get_order_status",
  "order_id": "ORD-12345",
  "business": {
    "id": "biz_123",
    "phoneNumberId": "987654321",
    "countryCode": "IN",
    "bizWhatsappNumber": "+919876543210",
    "businessAccountId": "meta_biz_456",
    "fbAppId": "fb_app_789"
  }
}
```
This keeps the schema shown to the LLM lean (only real params), while still giving the handler routing info + which business/phone-number-id triggered it — needed since one org can have multiple businesses/phone numbers with different API keys.

### 4. Python handler contract
Single function signature per org:
```python
def all_events_handler(event, context):
    function_name = event.get('function_name')
    business = event.get('business', {})

    if function_name == 'get_order_status':
        order_id = event.get('order_id')
        # business['phoneNumberId'] / business['id'] available for multi-business routing
        return {"status": "shipped", "tracking": "ABC123"}

    return {"error": f"Unknown function: {function_name}"}
```
ALWAYS route on `function_name`. For non-trivial logic, ALWAYS let the user split into helper modules/files to avoid one giant if/elif block — but the router entry point stays the single dispatch point.

### 5. Environment variables
Store secrets encrypted, per-org, injected as `os.environ` at execution time. NEVER let API keys be hardcoded in the Python or JSON. Settings UI: key/value pairs, editable, not shown in plaintext after save.

### 6. Test Run
Give the user a "Test Run" action that:
- Always executes the current DRAFT code (not the last deployed version).
- Accepts a raw JSON test payload (`function_name` + params — no need to include `business`, auto-inject a mock one for testing).
- Returns `result`, `logs` (captured stdout/print), and `statusCode` in the console output.

### 7. Versioning + Deploy
- "Create Version": snapshot current draft (all chatbot JSONs + function JSONs + handler .py) with a description, immutable once created.
- "Deploy": pick a version, confirm, then sync:
  - Each chatbot JSON is upserted into our existing Agent/bot table, matched by `id` (not `name`).
  - Each function JSON is upserted and linked to the chatbots that reference it in `functions[]`.
  - Store the deployed version number against each chatbot record, and surface a "synced with code version vX" badge + edit-locked warning on our existing no-code Agent-builder UI so users don't edit a code-managed bot there and get it silently overwritten on next deploy.

NEVER let the no-code Agent page silently overwrite a code-managed bot without warning. ALWAYS show the sync badge + "edit via Code Editor" notice on any bot whose `id` originated from Code Editor.

### 8. Copilot (optional, phase 2)
An AI side-panel that can generate/edit the chatbot JSON, function JSON, and handler Python from a plain-English request, then show a diff the user must Accept before it's applied. NEVER auto-apply Copilot's changes without explicit Accept.

### 9. External API access
Expose the deployed handler at `POST /v1/org/code/run`, Bearer-auth'd via our existing API-key system (Settings → Developer). Give a "Docs" tab in the Test Run panel showing: request format, auth, which fields are auto-injected (so users know NOT to pass them), and a ready-to-copy cURL example with the auto-injected fields stripped out.

## Deliverable
Ship this as a new top-level "Code Editor" section in the sidebar, gated behind [whatever plan/permission tier we decide], reusing our existing Monaco setup if we have one, and our existing bot/function DB tables where possible rather than new ones — flag it to me if the existing schema can't represent `functions[]` linkage or the `id`-based sync cleanly, don't silently work around it.

## Out of scope for v1
- Multi-language support (Python only for v1).
- Per-function separate compute/timeout config (org-level timeout toggle is enough for v1).
