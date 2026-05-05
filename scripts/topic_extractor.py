#!/usr/bin/env python3
"""Extract markdown deliverables from N8N deliberation workflow JSON traces.

Reads one or more deliberation_<ISO>.json files (as written to Drive by the
multi-LLM deliberation workflow) and writes sibling markdown files containing
the final author output, optionally with audit metadata.

Usage:
    python extract_deliberation.py path/to/deliberation_2026-05-05T14-45-54-813Z.json
    python extract_deliberation.py path/to/folder/ --include-initial
    python extract_deliberation.py path/to/folder/ --full -o ./extracted/
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------- Domain model ----------

@dataclass
class DeliberationTrace:
    """Parsed deliberation workflow JSON trace."""

    timestamp: str
    draft_prompt: str
    extra_context: str
    refined_prompt: str
    rubric: list[dict[str, Any]]
    initial_author_output: str
    iterations: list[dict[str, Any]]
    final_output: str
    final_verdict: str
    retry_count: int
    models_used: dict[str, str]

    @classmethod
    def from_path(cls, path: Path) -> DeliberationTrace:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            timestamp=data["timestamp"],
            draft_prompt=data["input"]["draft_prompt"],
            extra_context=data["input"].get("extra_context", ""),
            refined_prompt=data["refined_prompt"],
            rubric=data["rubric"],
            initial_author_output=data.get("initial_author_output", ""),
            iterations=data["iterations"],
            final_output=data["final_output"],
            final_verdict=data["final_verdict"],
            retry_count=data["retry_count"],
            models_used=data["models_used"],
        )

    @property
    def last_iteration(self) -> dict[str, Any]:
        return self.iterations[-1]

    @property
    def overall_score(self) -> int:
        return int(self.last_iteration["validator_json"]["overall_score"])


# ---------- Rendering ----------

def _excerpt(text: str, n: int = 120) -> str:
    """Single-line excerpt for frontmatter, escaped for YAML."""
    flat = " ".join(text.split())
    if len(flat) > n:
        flat = flat[: n - 1].rstrip() + "…"
    return flat.replace('"', "'")


def render_frontmatter(trace: DeliberationTrace, version_label: str = "final") -> str:
    """YAML frontmatter capturing audit info for the rendered file."""
    lines = [
        "---",
        f"timestamp: {trace.timestamp}",
        f"version: {version_label}",
        f"verdict: {trace.final_verdict}",
        f"overall_score: {trace.overall_score}",
        f"retry_count: {trace.retry_count}",
        "models:",
        f"  author: {trace.models_used.get('author', 'unknown')}",
        f"  critic: {trace.models_used.get('critic', 'unknown')}",
        f"  moderator: {trace.models_used.get('moderator', 'unknown')}",
        f'draft_prompt_excerpt: "{_excerpt(trace.draft_prompt)}"',
        "---",
        "",
    ]
    return "\n".join(lines)


def render_final(trace: DeliberationTrace) -> str:
    """Just the final output, with frontmatter."""
    return render_frontmatter(trace, "final") + trace.final_output


def render_initial(trace: DeliberationTrace) -> str:
    """The pre-critique v1 output, with frontmatter labelled appropriately."""
    return render_frontmatter(trace, "initial-pre-critique") + trace.initial_author_output


def render_full(trace: DeliberationTrace) -> str:
    """Final output plus audit appendix (refined prompt, rubric, critic, validator)."""
    parts: list[str] = [render_frontmatter(trace, "final"), "## Final output\n", trace.final_output]
    parts.append("\n\n---\n\n## Deliberation audit\n")

    parts.append("### Refined prompt\n")
    parts.append("```")
    parts.append(trace.refined_prompt)
    parts.append("```\n")

    parts.append("### Rubric\n")
    for r in trace.rubric:
        parts.append(
            f"- **{r['criterion']}** (weight {r['weight']}/{r['max_score']}): {r['description']}"
        )
    parts.append("")

    parts.append("### Validator scores\n")
    for c in trace.last_iteration["validator_json"]["per_criterion"]:
        parts.append(f"- **{c['criterion']}**: {c['score']}/5 — {c['justification']}")
    rec = trace.last_iteration["validator_json"].get("recommendation", "")
    if rec:
        parts.append(f"\n*Recommendation:* {rec}")
    parts.append("")

    critic = trace.last_iteration["critic_json"]
    parts.append("### Critic notes\n")
    for label, key in (("Strengths", "strengths"), ("Weaknesses", "weaknesses"), ("Suggested revisions", "suggested_revisions")):
        parts.append(f"**{label}:**")
        for item in critic.get(key, []):
            parts.append(f"- {item}")
        parts.append("")

    return "\n".join(parts)


# ---------- I/O ----------

def discover_inputs(arg: Path) -> list[Path]:
    if arg.is_file():
        return [arg]
    if arg.is_dir():
        return sorted(arg.glob("deliberation_*.json"))
    raise FileNotFoundError(f"Path not found: {arg}")


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="JSON file, or directory containing deliberation_*.json files")
    parser.add_argument("-o", "--output-dir", type=Path, default=None, help="Output directory (default: same as input)")
    parser.add_argument("--include-initial", action="store_true", help="Also write pre-critique v1 as <basename>.v1.md")
    parser.add_argument("--full", action="store_true", help="Render full audit (final + rubric + critic + validator)")
    args = parser.parse_args(argv)

    try:
        inputs = discover_inputs(args.input)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not inputs:
        print(f"warning: no deliberation_*.json files found in {args.input}", file=sys.stderr)
        return 1

    written = 0
    for json_path in inputs:
        try:
            trace = DeliberationTrace.from_path(json_path)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"skip: {json_path.name} ({type(e).__name__}: {e})", file=sys.stderr)
            continue

        out_dir = args.output_dir or json_path.parent
        basename = json_path.stem  # deliberation_<ISO>

        content = render_full(trace) if args.full else render_final(trace)
        final_path = out_dir / f"{basename}.md"
        write_markdown(final_path, content)
        print(f"wrote: {final_path}")
        written += 1

        if args.include_initial:
            if trace.initial_author_output:
                v1_path = out_dir / f"{basename}.v1.md"
                write_markdown(v1_path, render_initial(trace))
                print(f"wrote: {v1_path}")
                written += 1
            else:
                print(f"  note: no initial_author_output in {json_path.name} (pre-patch trace)", file=sys.stderr)

    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())