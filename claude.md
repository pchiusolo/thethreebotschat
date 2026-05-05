# CLAUDE.md — N8N Workflow Conventions

This document captures the conventions used across N8N workflows in this project. Any workflow JSON you generate must follow these conventions exactly, regardless of the specific task. Task-specific architecture and data shapes belong in the per-task RACE prompt; what's here applies to **every** workflow.

If a convention here conflicts with a task-specific instruction in the RACE prompt, the RACE prompt wins for that workflow only — but flag the conflict in the workflow's first node `notes` field.

---

## 1. Workflow scaffold

Every workflow starts with this exact three-node sequence, in this order:

```
Trigger → n8n Table: Load Env Vars → Code: Build Config → <rest of pipeline>
```

### `n8n Table: Load Env Vars`
- Type: `n8n-nodes-base.dataTable`, typeVersion `1`.
- `operation: "get"`, `returnAll: true`.
- `dataTableId.value`: placeholder `REPLACE_WITH_ENV_TABLE_ID` (the user binds the actual ID post-import; this is a shared singleton table across all workflows in the project).
- Settings: `onError: "stopWorkflow"`.

### `Code: Build Config`
- Type: `n8n-nodes-base.code`, typeVersion `2`.
- Settings: `onError: "stopWorkflow"`.
- Notes: `"Builds flat Name→Value map from EnvironmentVariables rows. Access values as: $('Code: Build Config').first().json.KeyName"`.
- Body (canonical form):

```javascript
const items = $input.all();
const cfg = {};
for (const item of items) {
  const name = (item.json.Name ?? '').trim();
  const value = item.json.Value;
  if (name && value != null && value !== '') {
    cfg[name] = value;
  }
}

const requiredKeys = [
  // List every key this workflow needs, in any order
];

for (const key of requiredKeys) {
  if (!cfg[key]) {
    throw new Error('[Build Config] Missing required key: ' + key + '. Add it to EnvironmentVariables table.');
  }
}

return [{ json: cfg }];
```

Always validate. Never let a missing key surface as a downstream HTTP 401 or a confusing parse error.

### Trigger choice
- `n8n-nodes-base.manualTrigger` for batch/orchestrator-driven workflows that pick jobs from tables.
- `n8n-nodes-base.webhook` for synchronous request–response workflows. Pattern: `httpMethod: "POST"`, `path: "<slug>"`, `responseMode: "lastNode"` so the last node's `$json` becomes the HTTP response body.
- `n8n-nodes-base.formTrigger` for interactive personal-use workflows where the user submits via a form UI.

---

## 2. Variable access

After Build Config, every downstream node reads config via:

```
$('Code: Build Config').first().json.<KeyName>
```

In code nodes:
```javascript
const cfg = $('Code: Build Config').first().json;
// then cfg.OpenAI_ApiKey, cfg.prompt_xyz, etc.
```

In expressions (HTTP headers, IF conditions, DataTable filters):
```
={{ $('Code: Build Config').first().json.<KeyName> }}
```

Never hardcode API keys, model names, prompts, folder IDs, or thresholds in node parameters or code. Everything that could change between runs lives in the `EnvironmentVariables` table.

---

## 3. Code node conventions

### Style

- Plain JavaScript, no TypeScript.
- Prefer `function () {}` form over arrow functions for top-level helpers inside Code nodes. Arrow functions are fine for inline callbacks (`.map`, `.filter`).
- Prefer string concatenation (`'a: ' + value`) over template literals. Reserve template literals for genuinely multi-line strings.
- `const` everywhere; `let` only when reassignment is necessary.
- snake_case for all JSON field names produced by code (`prompt_version`, `weighted_score`, `qa_state`).

### Defensive parsing

Wrap every `JSON.parse` in a try/catch. Throw with a `[Node Name]` prefix:

```javascript
let parsed;
try {
  parsed = JSON.parse(rawString);
} catch (error) {
  throw new Error('[Build Scenes Prompt] Failed to parse scene_plan_json: ' + error.message);
}
```

When parsing LLM output, use the JSON-extraction helper (Section 6) — LLMs occasionally wrap JSON in prose or markdown fences.

### Validation

After parsing, validate shape and throw early:

```javascript
if (!Array.isArray(parsed) || parsed.length === 0) {
  throw new Error('[Build Scenes Prompt] scene_plan_json is empty or invalid.');
}
```

### Accessing prior node outputs

- `$json` — current node's input.
- `$input.all()` — all input items.
- `$('Node Name').first().json` — output of a specific upstream node, first item.
- `$('Node Name').all()` — output of a specific upstream node, all items.

When a workflow has many intermediate transformations, prefer the explicit `$('Node Name')` form over `$json` chains. It survives refactoring.

---

## 4. HTTP nodes for LLM calls

**Never use the native `n8n-nodes-base.openAi`, `n8n-nodes-base.anthropic`, or any other vendor-specific LLM node.** Always use `n8n-nodes-base.httpRequest` (typeVersion `4.2` or current). Reasons: full control over payloads, no surprises when the vendor changes their SDK, easier to debug from raw response inspection.

### Two-node pattern

LLM calls are always two nodes:

1. `Code: Build <Purpose> Payload` — assembles the full request body as `{ json: { <vendor>_payload: {...} } }`.
2. `HTTP: <Purpose>` — POSTs `={{ JSON.stringify($json.<vendor>_payload) }}`.

This keeps prompt logic (in Code) separate from transport (in HTTP) and makes it trivial to dry-run prompts without consuming credits.

### OpenAI

```json
{
  "method": "POST",
  "url": "https://api.openai.com/v1/chat/completions",
  "sendHeaders": true,
  "headerParameters": {
    "parameters": [
      {
        "name": "Authorization",
        "value": "={{ 'Bearer ' + $('Code: Build Config').first().json.OpenAI_ApiKey }}"
      }
    ]
  },
  "sendBody": true,
  "contentType": "raw",
  "rawContentType": "application/json",
  "body": "={{ JSON.stringify($json.openai_payload) }}",
  "options": {}
}
```

Payload shape: `{ model, temperature, max_completion_tokens, messages: [{role, content}], response_format?: { type: "json_object" } }`. For reasoning models, add `reasoning_effort: "low" | "medium" | "high"`.

### Anthropic

```json
{
  "method": "POST",
  "url": "https://api.anthropic.com/v1/messages",
  "sendHeaders": true,
  "headerParameters": {
    "parameters": [
      { "name": "x-api-key", "value": "={{ $('Code: Build Config').first().json.Anthropic_ApiKey }}" },
      { "name": "anthropic-version", "value": "2023-06-01" },
      { "name": "content-type", "value": "application/json" }
    ]
  },
  "sendBody": true,
  "contentType": "raw",
  "rawContentType": "application/json",
  "body": "={{ JSON.stringify($json.anthropic_payload) }}",
  "options": {}
}
```

**Critical**: Anthropic uses `x-api-key`, **not** `Authorization: Bearer`. The `anthropic-version` header is required.

Payload shape: `{ model, max_tokens, system, messages: [{role: "user", content}], thinking?: { type: "enabled", budget_tokens: <n> } }`.

When extended thinking is enabled, the response `content[]` array contains both `type: "thinking"` and `type: "text"` blocks. Extract only the `text` blocks for downstream use; pass `thinking` blocks through to logs only.

### Gemini

```json
{
  "method": "POST",
  "url": "=https://generativelanguage.googleapis.com/v1beta/models/{{ $('Code: Build Config').first().json.Moderator_Model }}:generateContent?key={{ $('Code: Build Config').first().json.Gemini_ApiKey }}",
  "sendHeaders": true,
  "headerParameters": {
    "parameters": [
      { "name": "content-type", "value": "application/json" }
    ]
  },
  "sendBody": true,
  "contentType": "raw",
  "rawContentType": "application/json",
  "body": "={{ JSON.stringify($json.gemini_payload) }}",
  "options": {}
}
```

Payload shape: `{ system_instruction: { parts: [{ text }] }, contents: [{ role: "user", parts: [{ text }] }], generationConfig: { responseMimeType: "application/json", temperature } }`.

Extract response text from `candidates[0].content.parts[0].text`.

### Batching for fan-out

When an HTTP node receives multiple input items (one per job in a batch), enable batching to avoid hitting rate limits:

```json
"options": {
  "batching": {
    "batch": {
      "batchSize": 5,
      "batchInterval": 2500
    }
  }
}
```

Five concurrent requests, 2.5-second interval between bursts. Tune up only if the vendor's rate limit allows it.

### Settings on HTTP nodes

- Single-shot synchronous calls (one item in → one item out): `onError: "continueRegularOutput"` so a single failure produces a parseable error item rather than killing the workflow.
- The downstream parse Code node is responsible for detecting the error item and either failing loudly or carrying a default failure structure forward.

---

## 5. Data tables

### Read

`n8n-nodes-base.dataTable` typeVersion `1`, `operation: "get"`, `returnAll: true`. `dataTableId.value` is the table UUID.

### Update

```json
{
  "operation": "update",
  "dataTableId": { "mode": "id", "value": "<TABLE_UUID>" },
  "matchType": "allConditions",
  "filters": {
    "conditions": [
      { "keyName": "<unique_key>", "keyValue": "={{ $json.<unique_key> }}" }
    ]
  },
  "columns": {
    "mappingMode": "defineBelow",
    "value": {
      "<col_name>": "={{ $json.<source_field> }}"
    }
  }
}
```

Settings on update nodes: `onError: "continueRegularOutput"` — a single row failing to update should not kill a batch.

### Locking pattern

For "pick one job from a table and process it" workflows, set `lock_until` and `_lock_owner` columns at pick time and skip rows where `lock_until > now`. Standard lock window is 15 minutes.

---

## 6. JSON-extraction helper for LLM responses

LLMs occasionally wrap JSON output in markdown fences or add prose. Use this helper inside any Code node that parses LLM responses:

```javascript
function parseJsonObject(rawText) {
  if (!rawText) {
    throw new Error('Empty response text');
  }
  const cleaned = String(rawText).trim();
  const match = cleaned.match(/\{[\s\S]*\}/);
  if (!match) {
    throw new Error('No JSON object found in response');
  }
  return JSON.parse(match[0]);
}
```

For arrays, swap the regex to `/\[[\s\S]*\]/` and the error message accordingly.

System prompts should always end with: *"Output ONLY a single JSON object. No commentary outside the JSON. No markdown fences."* — but assume models will violate this occasionally and parse defensively.

---

## 7. Control flow

### IF nodes (typeVersion 2)

```json
{
  "conditions": {
    "combinator": "and",
    "conditions": [
      {
        "id": "cond-<descriptive-slug>",
        "leftValue": "={{ $json.<field> }}",
        "rightValue": <value>,
        "operator": { "type": "number" | "string" | "boolean", "operation": "equals" | "gte" | "lt" | ... }
      }
    ]
  }
}
```

Always give each condition a meaningful `id` (e.g. `cond-weighted-score`, not `cond-1`). Settings: empty `{}`.

### Merge nodes (typeVersion 3)

For combining two parallel branches before a downstream step:

```json
{
  "mode": "combine",
  "combineBy": "combineByPosition",
  "options": {}
}
```

Settings: `onError: "stopWorkflow"`.

### Retry patterns

Two acceptable patterns; pick based on whether the workflow is async or sync.

- **State-bearing output (preferred for async / orchestrator-driven workflows)**: workflow ends with a status (`APPROVED`, `REJECTED`, `RETRY`) and a `pipeline_stage` indicating the next handler. An external scheduler re-enqueues `RETRY` items. No in-place loop.
- **Bounded in-place loop (only for sync personal-use workflows)**: an IF gate routes a "retry" branch back to an earlier step via a Merge node. Carry `retry_count` in `$json` and increment in a dedicated Code node. Hard cap at 2–3 iterations via the IF condition. Document the loop edge in the Merge node's `notes`.

---

## 8. Output conventions

### Output node pattern

When a workflow can finish in multiple states, end each branch in a dedicated `Code: Output <STATUS>` node that shapes the final JSON:

```javascript
const input = items[0].json;
return [{
  json: {
    status: 'APPROVED',
    pipeline_stage: '<next_step>',
    // ...domain fields
    // ...full diagnostic payload (scores, rationale, etc.) for downstream visibility
  }
}];
```

Standard statuses: `APPROVED`, `REJECTED`, `RETRY`. Add domain-specific ones (e.g. `QA_PASSED`, `QA_FAILED`, `QA_PARSE_ERROR`) when needed; document them in the workflow's first-node `notes`.

### Webhook response shape

When the trigger is a webhook (`responseMode: "lastNode"`), the last node's `$json` becomes the HTTP response body. Always include:

- `status` — one of the standard statuses above.
- `pipeline_stage` — what should happen next (string slug).
- A nested object containing the full diagnostic payload (LLM raw output, scores, errors) so the caller has everything for logging or retries.

### Versioning of upstream context

When relevant (multi-stage content pipelines, A/B testing of prompts), carry `prompt_version` and `content_strategy_version` through the workflow and into the output. Trace beats forensics every time.

---

## 9. Settings and error handling

| Node type | Default `onError` |
|---|---|
| Code | `stopWorkflow` |
| DataTable get | `stopWorkflow` |
| DataTable update | `continueRegularOutput` |
| HTTP (single-shot) | `continueRegularOutput` |
| HTTP (fan-out with batching) | `continueRegularOutput` |
| Merge | `stopWorkflow` |
| IF | empty `{}` |
| Trigger | empty `{}` (or `stopWorkflow` for manual) |

Rule of thumb: **stop on errors that would corrupt downstream state; continue on errors that affect a single item in a batch**.

Every node has a non-empty `notes` field. One sentence is enough; explain *why*, not *what*. Examples:
- `"Builds flat Name→Value map from EnvironmentVariables rows."`
- `"Uses normalized dialogue plus the shared beat-based scene plan so image concepts stay aligned."`
- `"qa_score is mapped from editorial_total_score for compatibility."`

---

## 10. Naming conventions

### Node names

Action prefix, colon, descriptive label:
- `Trigger: Manual` / `Trigger: Webhook /qa-scoring` / `Trigger: Form`
- `n8n Table: Load Env Vars` / `n8n Table: Get Shorts Jobs` / `n8n Table: Update Podcast QA`
- `Code: Build Config` / `Code: Pick & Lock Job` / `Code: Parse QA Response`
- `HTTP: QA OpenAI (Gates 1+5)` / `HTTP: Generate Scenes`
- `Drive: Upload scenes.json`
- `IF: Hard Fail` / `IF: Approved`
- `Merge: Reviewer Results`

Disambiguate when multiple nodes share a verb: `Code: Build QA Prompt` vs `Code: Build Images Prompt`.

### EnvironmentVariables keys

- API keys: `<Vendor>_ApiKey` (e.g. `OpenAI_ApiKey`, `Anthropic_ApiKey`, `Gemini_ApiKey`).
- Model identifiers: `<Role>_Model` (e.g. `Author_Model`).
- Prompts: `prompt_<role>` (e.g. `prompt_author`, `prompt_critic`).
- Folder IDs: `<Purpose>FolderId` or just `<Purpose>` if context is unambiguous (e.g. `LogFolderId`, `Scenes`, `Images`).
- Numeric thresholds: PascalCase or snake_case consistent within a workflow. Pick one and stick with it.

### File names produced by workflows

`<slug>_<artifact>.json` (e.g. `topic_slug_scenes.json`, `topic_slug_images.json`). Use ISO timestamps when uniqueness across runs matters: `<purpose>_<ISO>.json`.

---

## 11. Layout

- Position values spaced ≥ 400px on the x-axis between sequential nodes.
- Y-axis used for parallel branches: top branch ~−96, bottom branch ~+96, merge at y=0.
- Reading order is left-to-right.
- Loop-back edges should use a vertical detour (drop y by 200–300) so the graph stays readable.

These are hints, not rules. The graph rendering matters less than the connections being correct.

---

## 12. Google Drive uploads

```json
{
  "name": "={{ <expression that produces filename> }}",
  "driveId": {
    "__rl": true,
    "value": "MyDrive",
    "mode": "list",
    "cachedResultName": "My Drive"
  },
  "folderId": {
    "__rl": true,
    "value": "={{ $('Code: Build Config').first().json.<FolderIdKey> }}",
    "mode": "id"
  },
  "options": {}
}
```

Type: `n8n-nodes-base.googleDrive`, typeVersion `3`. Credential: `googleDriveOAuth2Api` (placeholder credential ID; user rebinds post-import).

Settings: `onError: "stopWorkflow"`. Logging is critical; if Drive fails, the run should fail loudly.

---

## 13. Anti-patterns

Don't:

- Use native LLM nodes (`@n8n/n8n-nodes-langchain.*`, `n8n-nodes-base.openAi`, etc.). Always HTTP Request.
- Hardcode credentials, model names, folder IDs, prompts, or thresholds anywhere except `EnvironmentVariables`.
- Skip the `Code: Build Config` validation step. Missing keys must surface there, not deep in the pipeline.
- Use `JSON.parse` without try/catch.
- Trust LLM JSON output without the regex-extraction helper.
- Leave `notes` empty on any node.
- Use `responseMode: "responseNode"` with a separate "Respond to Webhook" node for sync workflows. Use `responseMode: "lastNode"` and shape the last Code node's output to be the response.
- Add caching, queues, observability, or notification side-effects unless the RACE prompt explicitly asks for them. Stay minimal.
- Invent connections between nodes that aren't explicitly described in the RACE prompt.

---

## 14. Self-check before returning a workflow

Before returning the JSON, verify:

1. The JSON parses as valid JSON.
2. Every node has a unique `id` (UUID v4).
3. Every node referenced in `connections` exists in `nodes`.
4. The Build Config `requiredKeys` array contains every key actually used downstream.
5. Every Code node uses the `[Node Name]` error-prefix convention.
6. No hardcoded secrets, model names, folder IDs, or prompt strings outside `EnvironmentVariables` references.
7. `onError` settings match the table in Section 9.
8. Every node has a non-empty `notes` field.
9. If there's a loop, the iteration cap is enforced by an IF condition that reads `retry_count` from `$json`, and the cap value comes from `EnvironmentVariables`.

Surface any deviation in a final note at the top of the JSON output (as a workflow-level `notes` field on the trigger), not in surrounding prose.

---

## 15. Reference workflows

These attached samples illustrate the conventions in production form. Refer to them when the conventions document is ambiguous; refer to this document when the samples conflict (samples may carry domain-specific patches that aren't general convention):

- `script-to-scenes--generational-slang.json` — manual trigger, single LLM (OpenAI), Drive upload, parallel scenes/images branches.
- `qa-scoring--three-gens-talks.json` — webhook trigger, single LLM (OpenAI), multi-branch IF gating, state-bearing outputs (APPROVED/REJECTED/RETRY).
- `script-qa-gate--all-channels.json` — manual trigger, dual LLM (OpenAI + Anthropic), batching, fan-out across multiple jobs, DataTable updates.

The samples are oracles for *how* to write nodes; this document is the oracle for *which* conventions to follow.
