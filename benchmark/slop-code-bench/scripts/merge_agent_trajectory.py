"""Convert agent stdout.jsonl checkpoints into a readable trajectory text.

Usage:
    python scripts/merge_agent_trajectory.py outputs/deepseek/claude_code-2.0.51_cog-guided_cog_20260701T1732

The script expects <experiment_dir>/**/checkpoint_*/agent/stdout.jsonl
and writes <experiment_dir>/trajectory.txt.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


MAX_TOOL_OUTPUT_LINES = 120


def _truncate(text: str, max_lines: int = MAX_TOOL_OUTPUT_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} lines truncated) ..."


def _format_tool_input(name: str, input_data: dict[str, Any]) -> str:
    parts = [f"[{name}]"]
    for key, value in input_data.items():
        if isinstance(value, str) and ("\n" in value or len(value) > 100):
            parts.append(f"  {key}:")
            for sub_line in value.splitlines():
                parts.append(f"    {sub_line}")
        else:
            parts.append(f"  {key}: {value}")
    return "\n".join(parts)


def _format_tool_result(block: dict[str, Any], tool_names: dict[str, str]) -> str:
    tool_use_id = block.get("tool_use_id", "")
    name = block.get("name") or tool_names.get(tool_use_id, "unknown")
    output = block.get("content") or block.get("output", "")
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False, indent=2)
    output = _truncate(output)
    is_error = block.get("is_error", False)
    header = f"[{name} result]"
    if is_error:
        header += " (error)"
    return f"{header}\n{output}"


def _get_content_blocks(event: dict[str, Any]) -> list[Any]:
    """Return message content blocks from either event.message.content or event.content."""
    message = event.get("message") or {}
    content = message.get("content")
    if content is None:
        content = event.get("content", [])
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return content
    return [content]


def _format_assistant(event: dict[str, Any]) -> str:
    chunks: list[str] = []
    for block in _get_content_blocks(event):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "")
            if text.strip():
                chunks.append(text)
        elif block_type == "thinking":
            thinking = block.get("thinking", "")
            if thinking.strip():
                chunks.append(f"<thinking>\n{thinking}\n</thinking>")
        elif block_type == "tool_use":
            name = block.get("name", "unknown")
            input_data = block.get("input", {})
            chunks.append(_format_tool_input(name, input_data))
    return "\n\n".join(chunks)


def _format_user(event: dict[str, Any], tool_names: dict[str, str]) -> str:
    chunks: list[str] = []
    seen_content: list[str] = []
    for block in _get_content_blocks(event):
        if isinstance(block, str):
            if block.strip():
                chunks.append(block)
                seen_content.append(block.strip())
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_result":
            formatted = _format_tool_result(block, tool_names)
            chunks.append(formatted)
            # Remember the rendered content so we can skip duplicate stdout later.
            content = block.get("content") or block.get("output", "")
            if isinstance(content, str):
                seen_content.append(content.strip())
        elif block_type == "text":
            text = block.get("text", "")
            if text.strip():
                chunks.append(text)
                seen_content.append(text.strip())

    # If the event carries a structured tool_use_result, include stdout/stderr too.
        tool_result = event.get("tool_use_result")
    if isinstance(tool_result, dict):
        stdout = tool_result.get("stdout", "")
        stderr = tool_result.get("stderr", "")
        stdout_stripped = stdout.strip()
        if stdout_stripped and not any(
            stdout_stripped in seen or seen in stdout_stripped
            for seen in seen_content
        ):
            chunks.append(f"[stdout]\n{_truncate(stdout)}")
        if stderr:
            chunks.append(f"[stderr]\n{_truncate(stderr)}")

    return "\n\n".join(chunks)


def _format_event(event: dict[str, Any], tool_names: dict[str, str]) -> str | None:
    event_type = event.get("type")
    if event_type == "assistant":
        body = _format_assistant(event)
    elif event_type == "user":
        body = _format_user(event, tool_names)
    elif event_type == "system":
        body = event.get("content", "")
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False, indent=2)
    else:
        body = json.dumps(event, ensure_ascii=False, indent=2)

    body = (body or "").strip()
    if not body:
        return None
    return f"[{event_type.upper()}]\n{body}"


def _process_jsonl(jsonl_path: Path) -> list[str]:
    events: list[dict[str, Any]] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"type": "raw", "line": line})

    # Build tool_use_id -> tool name mapping across the whole checkpoint.
    tool_names: dict[str, str] = {}
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in _get_content_blocks(event):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_id = block.get("id") or block.get("tool_use_id")
                if tool_id:
                    tool_names[tool_id] = block.get("name", "unknown")

    sections: list[str] = []
    for event in events:
        if event.get("type") == "raw":
            sections.append(f"[RAW LINE]\n{event.get('line', '')}")
            continue
        formatted = _format_event(event, tool_names)
        if formatted:
            sections.append(formatted)
    return sections


def _natural_sort_key(name: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part
        for part in re.split(r"(\d+)", name)
    ]


def merge_trajectory(experiment_dir: Path) -> Path:
    # Recursively find all checkpoint_*/agent/stdout.jsonl files.
    jsonl_paths = sorted(
        experiment_dir.rglob("checkpoint_*/agent/stdout.jsonl"),
        key=lambda p: _natural_sort_key(str(p.relative_to(experiment_dir))),
    )
    if not jsonl_paths:
        raise FileNotFoundError(
            f"No checkpoint_*/agent/stdout.jsonl files found under {experiment_dir}"
        )

    output_path = experiment_dir / "trajectory.txt"

    with open(output_path, "w", encoding="utf-8") as out:
        out.write(f"Experiment: {experiment_dir.name}\n")
        out.write(f"Checkpoints: {len(jsonl_paths)}\n")
        out.write("Generated from: */checkpoint_*/agent/stdout.jsonl\n")
        out.write("\n")

        for jsonl_path in jsonl_paths:
            checkpoint_dir = jsonl_path.parent.parent
            sections = _process_jsonl(jsonl_path)
            out.write(f"\n{'=' * 80}\n")
            out.write(f"CHECKPOINT START: {checkpoint_dir.relative_to(experiment_dir)}\n")
            out.write(f"Source: {jsonl_path}\n")
            out.write(f"{'=' * 80}\n\n")
            out.write("\n\n---\n\n".join(sections))
            out.write(f"\n\n{'=' * 80}\n")
            out.write(f"CHECKPOINT END: {checkpoint_dir.relative_to(experiment_dir)}\n")
            out.write(f"Lines processed: {len(sections)}\n")
            out.write(f"{'=' * 80}\n")

    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge agent stdout.jsonl checkpoints into a readable trajectory text."
    )
    parser.add_argument(
        "experiment_dir",
        type=Path,
        help="Experiment directory containing */checkpoint_*/agent/stdout.jsonl",
    )
    args = parser.parse_args()

    output_path = merge_trajectory(args.experiment_dir.resolve())
    print(f"Wrote trajectory to {output_path}")


if __name__ == "__main__":
    main()
