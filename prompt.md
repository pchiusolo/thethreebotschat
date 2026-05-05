# RACE Prompt — Multi-LLM Deliberation Workflow

> Submit this with `CLAUDE.md` and the three sample workflows attached.

---

## ROLE

You are a senior N8N workflow engineer working in this project. Follow `CLAUDE.md` exactly. Where this prompt is silent, defer to CLAUDE.md. Where this prompt and CLAUDE.md genuinely conflict, this prompt wins for this workflow only — and you must surface the conflict in the trigger node's `notes` field.

## ACTION

Build a single, importable N8N workflow that orchestrates three LLMs in an Author–Critic–Moderator deliberation pattern with a bounded retry loop, returns the final author output and verdict to the form trigger, and logs the full trace to Google Drive.

## CONTEXT

### Architecture

| Role | Model env var | Mode |
|---|---|---|
| Moderator | `Moderator_Model` (Gemini) | No thinking. Refines draft → RACE + rubric. Later validates against rubric. |
| Author | `Author_Model` (Claude Opus) | Extended thinking enabled. Produces v1, then revised v2 after critique. |
| Critic | `Critic_Model` (ChatGPT) | Reasoning enabled. Returns structured critique JSON. |

### Input

`n8n-nodes-base.formTrigger` with two fields:
- `draft_prompt` (textarea, required)
- `extra_context` (textarea, optional)

### Required EnvironmentVariables keys

Validate all of these in `Code: Build Config`:

```
Anthropic_ApiKey, OpenAI_ApiKey, Gemini_ApiKey,
Author_Model, Critic_Model, Moderator_Model,
Author_Thinking_Budget, Critic_Reasoning_Effort,
prompt_moderator_refiner, prompt_author, prompt_critic, prompt_moderator_validator,
Pass_Threshold, Max_Critique_Passes,
LogFolderId
```

### Pipeline

After the standard scaffold (Trigger → Load Env Vars → Build Config), implement these stages using the two-node Code+HTTP pattern from CLAUDE.md §4. Use the JSON-extraction helper (CLAUDE.md §6) for every LLM response parse.

1. **Refine prompt** — Gemini, `prompt_moderator_refiner` as system. User message: `draft_prompt` + `extra_context`. Expected output JSON: `{ refined_prompt, rubric: [{ criterion, description, weight, max_score }] }`. Initialise pipeline state: `retry_count = 0`, `latest_author_output = null`.

2. **Author v1** — Claude Opus with thinking. System: `prompt_author`. User: `refined_prompt` + `extra_context`. Extract concatenated `text`-typed content blocks into `latest_author_output`. Preserve `thinking` blocks separately for the trace log only — they must not flow into downstream model calls.

3. **Critic** — ChatGPT with reasoning. System: `prompt_critic`. User: `refined_prompt` + `latest_author_output`. Expected output JSON: `{ strengths[], weaknesses[], suggested_revisions[] }`.

4. **Author revision** — Claude Opus with thinking. System: `prompt_author`. User: `refined_prompt` + previous `latest_author_output` + critic JSON + instruction `"Produce the revised version. Address each item in suggested_revisions explicitly. Preserve the strengths identified."`. Update `latest_author_output`.

5. **Validate** — Gemini, `prompt_moderator_validator` as system. User: `refined_prompt`, `rubric`, `latest_author_output`. Expected output JSON: `{ per_criterion: [{ criterion, score, justification }], overall_score, verdict, recommendation }`.

6. **Gate (cascading IFs per CLAUDE.md §7)**:
   - `IF: Pass Threshold Met` — `overall_score >= Pass_Threshold` (numeric gte). True → step 7. False → next IF.
   - `IF: Retry Eligible` — `retry_count < (Max_Critique_Passes - 1)` (numeric lt). True → `Code: Increment Retry` (sets `retry_count = retry_count + 1`) → loop back into step 3 via a `Merge: Critic Inputs` node placed at the entry of the Critic stage. False → step 7 with the current FAIL verdict.

7. **Build trace** — assemble:

```
{
  timestamp: <ISO>,
  input: { draft_prompt, extra_context },
  refined_prompt, rubric,
  iterations: [{ iteration: <0..n>, author_output, author_thinking, critic_json, validator_json }],
  final_output: <latest_author_output>,
  final_verdict: <validator_json.verdict>,
  retry_count,
  models_used: { author, critic, moderator }
}
```

8. **Log** — upload trace as `deliberation_<ISO>.json` to `LogFolderId` (CLAUDE.md §12).

9. **Final output** — Code node returning `{ status, final_output, validator_summary: { overall_score, verdict, recommendation }, retry_count }`. This is what the form trigger returns to the user.

### Loop semantics

`Max_Critique_Passes` is the maximum total number of Critic calls across the run. With `Max_Critique_Passes = 2`, the Critic runs at most twice: once after Author v1, optionally once more if the first revision doesn't pass. The `Code: Increment Retry` node sits inside the loop edge so `retry_count` is incremented exactly when a retry is launched, never on the success or terminal-FAIL paths.

### Model-specific call details

- **Author (Anthropic)**: `thinking: { type: "enabled", budget_tokens: <Author_Thinking_Budget> }`; `max_tokens` ≥ `Author_Thinking_Budget + 4096`.
- **Critic (OpenAI)**: `reasoning_effort: <Critic_Reasoning_Effort>`; `response_format: { type: "json_object" }`.
- **Moderator (Gemini)**: `temperature: 0.2`; `responseMimeType: "application/json"`.

### Iteration tracking for the trace

Each pass through Critic→Author-revision→Validate appends one entry to `iterations[]`. Index the entries by `iteration` starting at 0 (the first revision pass).

## EXPECTATIONS

### Output

A single workflow JSON document, valid for direct N8N import. No surrounding prose, no markdown fences.

### Quality bar

All conventions in CLAUDE.md apply. Run the CLAUDE.md §14 self-check before returning.

### Stay minimal

No caching, no observability beyond the trace log, no notifications, no DB persistence beyond the Drive upload, no concurrency. The four prompt strings stay in `EnvironmentVariables` — they are not embedded in the workflow JSON.

### Surface decisions, don't bury them

If you make any choice that isn't fully determined by this spec or CLAUDE.md (e.g. a node naming variant, a layout choice, a defensive default for a malformed LLM response), record it in a one-line note on the affected node.