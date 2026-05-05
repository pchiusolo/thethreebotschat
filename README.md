# thethreebotschat

An N8N orchestration workflow that runs three commercial LLMs in an **Author–Critic–Moderator** deliberation loop to deeply brainstorm, critique, and validate ideas and prompts.

---

## Features

- **Form-triggered** — submit a draft prompt and optional extra context via an N8N form; the final output is returned directly as the form response.
- **Prompt refinement** — Gemini (Moderator) rewrites the raw draft into a RACE-structured prompt and generates a weighted scoring rubric before any generation begins.
- **Extended-thinking Author** — Claude Opus produces an initial draft with extended thinking enabled. Thinking blocks are preserved in the trace log but never forwarded to downstream models.
- **Reasoned Critic** — ChatGPT reviews the draft with reasoning enabled and returns structured JSON (`strengths`, `weaknesses`, `suggested_revisions`).
- **Guided revision** — Claude Opus revises the draft, explicitly addressing each `suggested_revision` while preserving identified strengths.
- **Rubric-based validation** — Gemini scores the revised draft per-criterion against the rubric, returning `overall_score`, `verdict`, and `recommendation`.
- **Bounded retry loop** — if the score is below `Pass_Threshold` and retries remain (`retry_count < Max_Critique_Passes - 1`), the Critic→Revision→Validation cycle repeats automatically.
- **Full trace logging** — every run uploads a `deliberation_<ISO>.json` file to Google Drive containing the complete pipeline trace (input, refined prompt, rubric, all iterations with thinking blocks, final output, verdict).

---

## Architecture

```
Trigger: Form
    │
    ▼
n8n Table: Load Env Vars → Code: Build Config
    │
    ▼
Code: Build Refiner Payload → HTTP: Moderate Refine (Gemini)
    │
    ▼
Code: Parse Refined Prompt          ← initialises retry_count=0, iterations=[]
    │
    ▼
Code: Build Author v1 Payload → HTTP: Author v1 (Claude Opus + thinking)
    │
    ▼
Code: Parse Author v1
    │
    ▼  ◄──────────────────────────────────────────────────────────────┐
Merge: Critic Inputs  (input 0 = initial pass │ input 1 = retry)     │
    │                                                                  │
    ▼                                                                  │
Code: Build Critic Payload → HTTP: Critic (ChatGPT + reasoning)       │
    │                                                                  │
    ▼                                                                  │
Code: Parse Critic Response                                            │
    │                                                                  │
    ▼                                                                  │
Code: Build Author Revision Payload → HTTP: Author Revision (Claude)  │
    │                                                                  │
    ▼                                                                  │
Code: Parse Author Revision                                            │
    │                                                                  │
    ▼                                                                  │
Code: Build Validator Payload → HTTP: Validate (Gemini)               │
    │                                                                  │
    ▼                                                                  │
Code: Parse Validator Response  ← appends iteration entry             │
    │                                                                  │
    ▼                                                                  │
IF: Pass Threshold Met?                                                │
  ├── TRUE  ──────────────────────────────┐                           │
  └── FALSE → IF: Retry Eligible?         │                           │
                ├── TRUE → Code: Increment Retry ────────────────────┘
                └── FALSE ───────────────┐
                                         ▼
                                  Code: Build Trace
                                         │
                                         ▼
                                  Drive: Upload Trace
                                         │
                                         ▼
                                  Code: Final Output  →  form response
```

### Roles

| Role | Model env var | Provider | Notes |
|---|---|---|---|
| Moderator | `Moderator_Model` | Gemini | Refines prompt + generates rubric; validates output against rubric. No thinking. |
| Author | `Author_Model` | Anthropic (Claude Opus) | Produces v1 draft and revised draft. Extended thinking enabled. |
| Critic | `Critic_Model` | OpenAI (ChatGPT) | Returns structured critique JSON. Reasoning enabled. |

---

## Environment Variables

All variables are stored in a single N8N **EnvironmentVariables** data table (one row per key, columns `Name` and `Value`). The `Code: Build Config` node validates that every required key is present before the pipeline runs.

### API credentials

| Key | Description |
|---|---|
| `Anthropic_ApiKey` | Anthropic API key for Claude Opus (Author). |
| `OpenAI_ApiKey` | OpenAI API key for ChatGPT (Critic). |
| `Gemini_ApiKey` | Google Gemini API key (Moderator). |

### Model identifiers

| Key | Example | Description |
|---|---|---|
| `Author_Model` | `claude-opus-4-7` | Anthropic model ID for the Author role. |
| `Critic_Model` | `o3` | OpenAI model ID for the Critic role. |
| `Moderator_Model` | `gemini-2.5-pro` | Gemini model ID for the Moderator role. |

### Model behaviour

| Key | Example | Description |
|---|---|---|
| `Author_Thinking_Budget` | `10000` | Token budget for Claude extended thinking. `max_tokens` is set to this value + 4096. |
| `Critic_Reasoning_Effort` | `medium` | OpenAI reasoning effort: `low`, `medium`, or `high`. |

### Prompts

All prompt strings live in the table so they can be edited without touching the workflow JSON.

| Key | Used by |
|---|---|
| `prompt_moderator_refiner` | Gemini system prompt for the initial prompt-refinement + rubric-generation step. |
| `prompt_author` | Claude system prompt for both the v1 draft and all revisions. |
| `prompt_critic` | ChatGPT system prompt for structured critique output. |
| `prompt_moderator_validator` | Gemini system prompt for per-criterion rubric scoring. |

### Loop control

| Key | Example | Description |
|---|---|---|
| `Pass_Threshold` | `7.5` | Minimum `overall_score` from the Validator to exit the loop as PASS. |
| `Max_Critique_Passes` | `2` | Maximum total Critic calls per run. With `2`, the Critic runs at most twice (one initial + one retry). |

### Storage

| Key | Description |
|---|---|
| `LogFolderId` | Google Drive folder ID where `deliberation_<ISO>.json` trace files are uploaded. |

---

## Trace log format

Every run writes a JSON file to Google Drive:

```json
{
  "timestamp": "<ISO 8601>",
  "input": { "draft_prompt": "...", "extra_context": "..." },
  "refined_prompt": "...",
  "rubric": [{ "criterion": "...", "description": "...", "weight": 0.2, "max_score": 10 }],
  "iterations": [
    {
      "iteration": 0,
      "author_output": "...",
      "author_thinking": ["<thinking blocks, for inspection only>"],
      "critic_json": { "strengths": [], "weaknesses": [], "suggested_revisions": [] },
      "validator_json": { "per_criterion": [], "overall_score": 8.1, "verdict": "PASS", "recommendation": "..." }
    }
  ],
  "final_output": "...",
  "final_verdict": "PASS",
  "retry_count": 0,
  "models_used": { "author": "claude-opus-4-7", "critic": "o3", "moderator": "gemini-2.5-pro" }
}
```

---

## Import & setup

1. Import `multi-llm-deliberation.json` into your N8N instance.
2. Open `n8n Table: Load Env Vars` and replace `REPLACE_WITH_ENV_TABLE_ID` with your EnvironmentVariables table UUID.
3. Bind the Google Drive credential in `Drive: Upload Trace` (replace `REPLACE_WITH_CREDENTIAL_ID`).
4. Add all required keys to the EnvironmentVariables table.
5. Activate and open the form URL to submit your first deliberation.

---

## Project conventions

Workflow code follows the conventions in [`claude.md`](claude.md):
- LLM calls use the two-node `Code: Build Payload` → `HTTP:` pattern (no native LLM nodes).
- All credentials, model names, prompts, and thresholds live in EnvironmentVariables — nothing is hardcoded.
- LLM responses are parsed via the `parseJsonObject` / `extractAnthropicText` / `extractGeminiText` helpers defined in §6 of `claude.md`.
