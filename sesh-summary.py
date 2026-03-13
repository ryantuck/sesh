#!/usr/bin/env python3
"""Generate a markdown summary of a Claude Code session from JSONL event logs."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_jsonl(filepath):
    """Parse a JSONL file into a list of event dicts."""
    events = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def merge_assistant_events(events):
    """Merge streamed assistant events that share the same message.id into single turns.

    Claude streams content blocks as separate JSONL lines sharing the same
    message.id.  The last line carries stop_reason and final usage stats.
    This function collapses them so each API call is one logical event.
    """
    merged = []
    seen_msg_ids = {}

    for evt in events:
        if evt.get("type") != "assistant":
            merged.append(evt)
            continue

        msg = evt.get("message", {})
        msg_id = msg.get("id")
        if not msg_id:
            merged.append(evt)
            continue

        if msg_id in seen_msg_ids:
            # Append content blocks to existing merged event
            idx = seen_msg_ids[msg_id]
            existing = merged[idx]
            existing_content = existing["message"].get("content", [])
            new_content = msg.get("content", [])
            existing_content.extend(new_content)
            existing["message"]["content"] = existing_content
            # Update usage/stop_reason from the latest event
            if msg.get("stop_reason"):
                existing["message"]["stop_reason"] = msg["stop_reason"]
            if msg.get("usage"):
                existing["message"]["usage"] = msg["usage"]
            # Keep latest timestamp
            existing["timestamp"] = evt.get("timestamp", existing.get("timestamp"))
        else:
            seen_msg_ids[msg_id] = len(merged)
            merged.append(evt)

    return merged


def format_timestamp(ts_str):
    """Format an ISO timestamp to a readable string."""
    if not ts_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, AttributeError):
        return ts_str


def compute_duration(start_ts, end_ts):
    """Compute human-readable duration between two ISO timestamps."""
    if not start_ts or not end_ts:
        return None
    try:
        start = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
        delta = end - start
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}s"
        minutes, seconds = divmod(total_seconds, 60)
        if minutes < 60:
            return f"{minutes}m {seconds}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m {seconds}s"
    except (ValueError, AttributeError):
        return None


def format_token_usage(usage):
    """Format token usage into a readable summary."""
    if not usage:
        return None
    parts = []
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    if input_tokens:
        parts.append(f"input: {input_tokens:,}")
    if output_tokens:
        parts.append(f"output: {output_tokens:,}")
    if cache_create:
        parts.append(f"cache_write: {cache_create:,}")
    if cache_read:
        parts.append(f"cache_read: {cache_read:,}")
    return ", ".join(parts) if parts else None


def truncate(text, max_len=500):
    """Truncate text with an ellipsis indicator."""
    if not text:
        return ""
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... ({len(text) - max_len} more characters)"


def format_tool_result_content(content):
    """Extract readable text from tool_result content (string or array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "tool_result":
                    inner = item.get("content", "")
                    if isinstance(inner, str):
                        parts.append(inner)
                    elif isinstance(inner, list):
                        for sub in inner:
                            if isinstance(sub, dict):
                                parts.append(sub.get("text", ""))
                            else:
                                parts.append(str(sub))
                elif item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_reference":
                    parts.append(f"[Tool loaded: {item.get('tool_name', 'unknown')}]")
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def render_tool_use(block, tool_results):
    """Render a tool_use block and its result as markdown."""
    tool_id = block.get("id", "")
    tool_name = block.get("name", "unknown")
    tool_input = block.get("input", {})
    lines = []
    lines.append(f"#### Tool: {tool_name}")

    # Render input based on tool type
    if tool_name == "Read":
        lines.append(f"**File:** `{tool_input.get('file_path', '?')}`")
        if tool_input.get("offset"):
            lines.append(f"**Offset:** {tool_input['offset']}, **Limit:** {tool_input.get('limit', 'default')}")
    elif tool_name == "Write":
        lines.append(f"**File:** `{tool_input.get('file_path', '?')}`")
    elif tool_name == "Edit":
        lines.append(f"**File:** `{tool_input.get('file_path', '?')}`")
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if old or new:
            lines.append("<details><summary>Diff</summary>\n")
            lines.append("```diff")
            for l in old.splitlines():
                lines.append(f"- {l}")
            for l in new.splitlines():
                lines.append(f"+ {l}")
            lines.append("```")
            lines.append("</details>")
    elif tool_name == "Glob":
        lines.append(f"**Pattern:** `{tool_input.get('pattern', '?')}`")
        if tool_input.get("path"):
            lines.append(f"**Path:** `{tool_input['path']}`")
    elif tool_name == "Grep":
        lines.append(f"**Pattern:** `{tool_input.get('pattern', '?')}`")
        if tool_input.get("glob"):
            lines.append(f"**File glob:** `{tool_input['glob']}`")
        if tool_input.get("output_mode"):
            lines.append(f"**Mode:** {tool_input['output_mode']}")
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        lines.append(f"```bash\n{cmd}\n```")
    elif tool_name == "Agent":
        lines.append(f"**Type:** {tool_input.get('subagent_type', 'general-purpose')}")
        lines.append(f"**Description:** {tool_input.get('description', '')}")
        prompt = tool_input.get("prompt", "")
        if prompt:
            lines.append(f"\n<details><summary>Agent Prompt</summary>\n")
            lines.append(f"{truncate(prompt, 1000)}")
            lines.append("</details>")
    elif tool_name == "WebFetch":
        lines.append(f"**URL:** `{tool_input.get('url', '?')}`")
        if tool_input.get("prompt"):
            lines.append(f"**Prompt:** {tool_input['prompt']}")
    elif tool_name == "WebSearch":
        lines.append(f"**Query:** {tool_input.get('query', '?')}")
    elif tool_name == "ToolSearch":
        lines.append(f"**Query:** `{tool_input.get('query', '?')}`")
    else:
        if tool_input:
            lines.append(f"**Input:** `{json.dumps(tool_input, default=str)[:300]}`")

    # Render result if available
    result = tool_results.get(tool_id)
    if result is not None:
        result_meta = result.get("meta")
        result_content = result.get("content", "")

        if result_meta:
            # Rich tool result metadata
            if isinstance(result_meta, dict):
                status = result_meta.get("status")
                if status:
                    lines.append(f"\n**Status:** {status}")

                # Agent completion result
                if result_meta.get("agentId"):
                    duration = result_meta.get("totalDurationMs")
                    tokens = result_meta.get("totalTokens")
                    tool_count = result_meta.get("totalToolUseCount")
                    parts = []
                    if duration:
                        parts.append(f"duration: {duration / 1000:.1f}s")
                    if tokens:
                        parts.append(f"tokens: {tokens:,}")
                    if tool_count:
                        parts.append(f"tool uses: {tool_count}")
                    if parts:
                        lines.append(f"**Agent Stats:** {', '.join(parts)}")

                # File read result
                if result_meta.get("type") == "text" and "file" in result_meta:
                    finfo = result_meta["file"]
                    lines.append(f"**Lines:** {finfo.get('totalLines', '?')}")

                # Glob result
                if "filenames" in result_meta and "durationMs" in result_meta:
                    fnames = result_meta["filenames"]
                    lines.append(f"**Files found:** {len(fnames)}")
                    if fnames:
                        shown = fnames[:10]
                        for fn in shown:
                            lines.append(f"  - `{fn}`")
                        if len(fnames) > 10:
                            lines.append(f"  - ... and {len(fnames) - 10} more")

                # Grep result
                if result_meta.get("mode") == "content":
                    lines.append(f"**Matches in {result_meta.get('numFiles', '?')} files, {result_meta.get('numLines', '?')} lines**")

                # WebFetch result
                if result_meta.get("url"):
                    code = result_meta.get("code")
                    duration = result_meta.get("durationMs")
                    parts = []
                    if code:
                        parts.append(f"HTTP {code}")
                    if duration:
                        parts.append(f"{duration}ms")
                    if parts:
                        lines.append(f"**Response:** {', '.join(parts)}")

            elif isinstance(result_meta, str):
                # Error string
                lines.append(f"\n**Error:** {result_meta}")

        if result_content:
            content_str = format_tool_result_content(result_content)
            if content_str:
                is_error = result.get("is_error", False)
                label = "Error Output" if is_error else "Result"
                lines.append(f"\n<details><summary>{label}</summary>\n")
                lines.append(f"```\n{truncate(content_str, 2000)}\n```")
                lines.append("</details>")

    return "\n".join(lines)


def render_subagent(agent_id, subagent_dir):
    """Render a subagent's conversation as nested markdown."""
    lines = []

    # Load meta
    meta_path = subagent_dir / f"{agent_id}.meta.json"
    meta = {}
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)

    # Load events
    jsonl_path = subagent_dir / f"{agent_id}.jsonl"
    if not jsonl_path.exists():
        lines.append(f"> Subagent log not found: {agent_id}")
        return "\n".join(lines)

    events = parse_jsonl(str(jsonl_path))
    events = merge_assistant_events(events)

    agent_type = meta.get("agentType", "unknown")
    lines.append(f"**Agent Type:** {agent_type}")

    if events:
        first_ts = events[0].get("timestamp")
        last_ts = events[-1].get("timestamp")
        duration = compute_duration(first_ts, last_ts)
        if duration:
            lines.append(f"**Duration:** {duration}")

    lines.append("")
    lines.extend(render_conversation(events, indent_level=1, subagent_dir=subagent_dir))
    return "\n".join(lines)


def render_conversation(events, indent_level=0, subagent_dir=None):
    """Render a sequence of merged events as markdown lines."""
    lines = []
    step = 0

    # Build a map from tool_use_id to tool result data
    tool_results = {}
    for evt in events:
        if evt.get("type") != "user":
            continue
        msg = evt.get("message", {})
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                tid = item.get("tool_use_id", "")
                result_meta = evt.get("toolUseResult")
                is_error = item.get("is_error", False)
                tool_results[tid] = {
                    "content": item.get("content", ""),
                    "meta": result_meta,
                    "is_error": is_error,
                }

    prefix = "#" * (indent_level + 3)  # h3 for top-level, h4 for subagent, etc.

    for evt in events:
        evt_type = evt.get("type")

        # --- Queue operations ---
        if evt_type == "queue-operation":
            op = evt.get("operation", "?")
            ts = format_timestamp(evt.get("timestamp"))
            if op == "enqueue":
                step += 1
                lines.append(f"{prefix} Step {step}: Task Enqueued")
                lines.append(f"**Time:** {ts}")
                task = evt.get("content", "")
                if task:
                    lines.append(f"\n> {task}")
                lines.append("")
            elif op == "dequeue":
                step += 1
                lines.append(f"{prefix} Step {step}: Task Dequeued")
                lines.append(f"**Time:** {ts}")
                lines.append("")

        # --- User messages (initial prompt only, not tool results) ---
        elif evt_type == "user":
            msg = evt.get("message", {})
            content = msg.get("content")

            # Skip tool result messages (they're rendered with their tool_use)
            if isinstance(content, list):
                continue

            # This is the initial user prompt
            if isinstance(content, str) and content:
                step += 1
                ts = format_timestamp(evt.get("timestamp"))
                lines.append(f"{prefix} Step {step}: User Prompt")
                lines.append(f"**Time:** {ts}")
                if evt.get("permissionMode"):
                    lines.append(f"**Permission Mode:** {evt['permissionMode']}")
                lines.append(f"\n> {content}")
                lines.append("")

        # --- Assistant messages ---
        elif evt_type == "assistant":
            msg = evt.get("message", {})
            content_blocks = msg.get("content", [])
            model = msg.get("model", "unknown")
            usage = msg.get("usage", {})
            stop_reason = msg.get("stop_reason")

            if not content_blocks:
                continue

            # Categorize blocks
            thinking_blocks = [b for b in content_blocks if b.get("type") == "thinking"]
            text_blocks = [b for b in content_blocks if b.get("type") == "text"]
            tool_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]

            # Render thinking
            for tb in thinking_blocks:
                thinking = tb.get("thinking", "")
                if thinking:
                    step += 1
                    lines.append(f"{prefix} Step {step}: Thinking")
                    lines.append(f"**Model:** `{model}`\n")
                    lines.append(f"<details><summary>Internal reasoning</summary>\n")
                    lines.append(truncate(thinking, 2000))
                    lines.append("\n</details>\n")

            # Render text output
            for tb in text_blocks:
                text = tb.get("text", "")
                if text:
                    step += 1
                    lines.append(f"{prefix} Step {step}: Assistant Response")
                    lines.append(f"\n{text}\n")

            # Render tool uses
            for tb in tool_blocks:
                step += 1
                tool_name = tb.get("name", "unknown")
                lines.append(f"{prefix} Step {step}: {tool_name}")
                lines.append("")
                lines.append(render_tool_use(tb, tool_results))
                lines.append("")

                # If this is an Agent tool use, render the subagent inline
                if tool_name == "Agent" and subagent_dir:
                    tool_id = tb.get("id", "")
                    result = tool_results.get(tool_id, {})
                    result_meta = result.get("meta")
                    if isinstance(result_meta, dict):
                        agent_id = result_meta.get("agentId", "")
                        if agent_id:
                            # Try both with and without "agent-" prefix
                            agent_file_id = agent_id
                            agent_file = subagent_dir / f"{agent_file_id}.jsonl"
                            if not agent_file.exists():
                                agent_file_id = f"agent-{agent_id}"
                                agent_file = subagent_dir / f"{agent_file_id}.jsonl"
                            if agent_file.exists():
                                lines.append(f"<details><summary>Subagent: {agent_id}</summary>\n")
                                lines.append(render_subagent(agent_file_id, subagent_dir))
                                lines.append("\n</details>\n")

            # Token usage for the final block of a turn
            if stop_reason and usage:
                usage_str = format_token_usage(usage)
                if usage_str:
                    lines.append(f"*Tokens — {usage_str}*\n")

        # --- Progress events ---
        elif evt_type == "progress":
            data = evt.get("data", {})
            if data.get("type") == "hook_progress":
                # These are hook notifications; usually not worth surfacing
                pass

    return lines


def compute_session_stats(events, subagent_dir=None):
    """Compute aggregate statistics for the session."""
    stats = {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_write": 0,
        "total_cache_read": 0,
        "tool_use_counts": {},
        "models_used": set(),
        "subagent_count": 0,
        "web_fetches": 0,
        "errors": 0,
    }

    for evt in events:
        if evt.get("type") == "assistant":
            msg = evt.get("message", {})
            model = msg.get("model")
            if model:
                stats["models_used"].add(model)
            usage = msg.get("usage", {})
            if usage and msg.get("stop_reason"):
                stats["total_input_tokens"] += usage.get("input_tokens", 0)
                stats["total_output_tokens"] += usage.get("output_tokens", 0)
                stats["total_cache_write"] += usage.get("cache_creation_input_tokens", 0)
                stats["total_cache_read"] += usage.get("cache_read_input_tokens", 0)

            for block in msg.get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "unknown")
                    stats["tool_use_counts"][name] = stats["tool_use_counts"].get(name, 0) + 1
                    if name == "Agent":
                        stats["subagent_count"] += 1
                    if name == "WebFetch":
                        stats["web_fetches"] += 1

        if evt.get("type") == "user":
            msg = evt.get("message", {})
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("is_error"):
                        stats["errors"] += 1

    # Also count subagent stats
    if subagent_dir and subagent_dir.exists():
        for meta_file in subagent_dir.glob("*.meta.json"):
            agent_id = meta_file.stem.replace(".meta", "")
            jsonl_file = subagent_dir / f"{agent_id}.jsonl"
            if jsonl_file.exists():
                sub_events = parse_jsonl(str(jsonl_file))
                sub_stats = compute_session_stats(sub_events)
                stats["total_input_tokens"] += sub_stats["total_input_tokens"]
                stats["total_output_tokens"] += sub_stats["total_output_tokens"]
                stats["total_cache_write"] += sub_stats["total_cache_write"]
                stats["total_cache_read"] += sub_stats["total_cache_read"]
                for k, v in sub_stats["tool_use_counts"].items():
                    stats["tool_use_counts"][k] = stats["tool_use_counts"].get(k, 0) + v
                stats["models_used"].update(sub_stats["models_used"])
                stats["errors"] += sub_stats["errors"]

    return stats


def generate_summary(session_path):
    """Generate the full markdown summary for a session."""
    session_path = Path(session_path)

    # Determine the JSONL file and session directory
    if session_path.suffix == ".jsonl":
        jsonl_path = session_path
        session_id = session_path.stem
        session_dir = session_path.parent / session_id
    elif session_path.is_dir():
        session_id = session_path.name
        jsonl_path = session_path.parent / f"{session_id}.jsonl"
        session_dir = session_path
    else:
        print(f"Error: {session_path} is not a .jsonl file or directory", file=sys.stderr)
        sys.exit(1)

    if not jsonl_path.exists():
        print(f"Error: {jsonl_path} not found", file=sys.stderr)
        sys.exit(1)

    # Parse events
    raw_events = parse_jsonl(str(jsonl_path))
    events = merge_assistant_events(raw_events)

    subagent_dir = session_dir / "subagents" if session_dir.exists() else None

    # Extract metadata from events (slug may not be on the first event)
    meta = {}
    for evt in events:
        if evt.get("type") in ("user", "assistant"):
            if not meta.get("cwd"):
                meta["cwd"] = evt.get("cwd", "")
                meta["git_branch"] = evt.get("gitBranch", "")
                meta["version"] = evt.get("version", "")
                meta["session_id"] = evt.get("sessionId", session_id)
            if evt.get("slug") and not meta.get("slug"):
                meta["slug"] = evt["slug"]
            if meta.get("slug"):
                break

    # Model from first assistant event
    for evt in events:
        if evt.get("type") == "assistant":
            meta["model"] = evt.get("message", {}).get("model", "unknown")
            break

    # Timestamps
    if events:
        meta["start_time"] = events[0].get("timestamp")
        meta["end_time"] = events[-1].get("timestamp")

    # Initial prompt
    initial_prompt = ""
    for evt in events:
        if evt.get("type") == "user":
            content = evt.get("message", {}).get("content")
            if isinstance(content, str):
                initial_prompt = content
                break

    # Compute stats
    stats = compute_session_stats(events, subagent_dir)

    # --- Build Markdown ---
    lines = []
    slug = meta.get("slug", session_id)
    lines.append(f"# Session: {slug}")
    lines.append("")

    # Metadata table
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"| Field | Value |")
    lines.append(f"|-------|-------|")
    lines.append(f"| **Session ID** | `{meta.get('session_id', session_id)}` |")
    if slug:
        lines.append(f"| **Slug** | {slug} |")
    lines.append(f"| **Start** | {format_timestamp(meta.get('start_time'))} |")
    lines.append(f"| **End** | {format_timestamp(meta.get('end_time'))} |")
    duration = compute_duration(meta.get("start_time"), meta.get("end_time"))
    if duration:
        lines.append(f"| **Duration** | {duration} |")
    lines.append(f"| **Working Directory** | `{meta.get('cwd', '?')}` |")
    lines.append(f"| **Git Branch** | `{meta.get('git_branch', '?')}` |")
    lines.append(f"| **CLI Version** | {meta.get('version', '?')} |")
    lines.append(f"| **Model** | `{meta.get('model', '?')}` |")
    lines.append("")

    # Stats summary
    lines.append("## Statistics")
    lines.append("")
    total_tokens = (
        stats["total_input_tokens"]
        + stats["total_output_tokens"]
        + stats["total_cache_write"]
        + stats["total_cache_read"]
    )
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| **Total Tokens** | {total_tokens:,} |")
    lines.append(f"| **Input Tokens** | {stats['total_input_tokens']:,} |")
    lines.append(f"| **Output Tokens** | {stats['total_output_tokens']:,} |")
    lines.append(f"| **Cache Write** | {stats['total_cache_write']:,} |")
    lines.append(f"| **Cache Read** | {stats['total_cache_read']:,} |")
    lines.append(f"| **Models Used** | {', '.join(f'`{m}`' for m in sorted(stats['models_used']))} |")
    lines.append(f"| **Subagents Spawned** | {stats['subagent_count']} |")
    lines.append(f"| **Errors** | {stats['errors']} |")
    lines.append("")

    # Tool use breakdown
    if stats["tool_use_counts"]:
        lines.append("### Tool Usage")
        lines.append("")
        lines.append("| Tool | Count |")
        lines.append("|------|-------|")
        for tool, count in sorted(stats["tool_use_counts"].items(), key=lambda x: -x[1]):
            lines.append(f"| {tool} | {count} |")
        lines.append("")

    # Task
    if initial_prompt:
        lines.append("## Task")
        lines.append("")
        lines.append(f"> {initial_prompt}")
        lines.append("")

    # Conversation flow
    lines.append("## Session Flow")
    lines.append("")
    lines.extend(render_conversation(events, indent_level=0, subagent_dir=subagent_dir))

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: sesh-summary.py <session.jsonl | session-dir>", file=sys.stderr)
        print("", file=sys.stderr)
        print("Generate a markdown summary of a Claude Code session.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Arguments:", file=sys.stderr)
        print("  session.jsonl    Path to the main session JSONL file", file=sys.stderr)
        print("  session-dir      Path to the session directory", file=sys.stderr)
        print("", file=sys.stderr)
        print("Output is written to stdout. Redirect to a file as needed:", file=sys.stderr)
        print("  sesh-summary.py session.jsonl > summary.md", file=sys.stderr)
        sys.exit(1)

    session_path = sys.argv[1]
    summary = generate_summary(session_path)
    print(summary)


if __name__ == "__main__":
    main()
