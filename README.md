# sesh-summary

Generate a markdown summary of a Claude Code session from its JSONL event logs.

## Usage

```bash
python3 sesh-summary.py <session.jsonl>
python3 sesh-summary.py <session-directory>

# Write to file
python3 sesh-summary.py session.jsonl > summary.md
```

No external dependencies — uses only the Python 3 standard library.

## What it does

Claude Code records every session interaction as a stream of JSON events in `.jsonl` files. This tool parses those events and produces a structured markdown document that captures the full session activity: every prompt, every tool call, every model response, and every subagent conversation.

### Input structure

A session on disk looks like this:

```
<session-id>.jsonl                     # Main session event log
<session-id>/
  subagents/
    agent-<id>.jsonl                   # Subagent event log
    agent-<id>.meta.json               # Subagent metadata (agent type)
    ...
```

The script accepts either the `.jsonl` file or the session directory as its argument and discovers the rest automatically.

### Processing pipeline

1. **Parse** — Each line of the JSONL file is a self-contained JSON event. The script reads all events into memory.

2. **Merge streamed assistant events** — The Claude API streams content blocks as separate events that share the same `message.id`. The script merges these into single logical turns so that one API call = one rendered step. The final event in a streamed group carries the `stop_reason` and cumulative token usage.

3. **Build tool result index** — User events that contain `tool_result` content are indexed by their `tool_use_id`. This allows each tool call to be rendered alongside its result.

4. **Extract metadata** — Session ID, slug, working directory, git branch, CLI version, model, and timestamps are pulled from the first few events.

5. **Compute statistics** — Token usage (input, output, cache write, cache read) is aggregated across all assistant turns with a `stop_reason` (to avoid double-counting streamed blocks). Tool usage counts, subagent counts, and error counts are tallied. Subagent stats are included recursively.

6. **Render markdown** — Events are walked in order and rendered as numbered steps:

   | Event type | Rendering |
   |---|---|
   | `queue-operation` | Task enqueue/dequeue with timestamp |
   | `user` (string content) | Initial user prompt in a blockquote |
   | `user` (tool_result array) | Skipped at top level — rendered inline with the tool call |
   | `assistant` thinking block | Collapsible "Internal reasoning" section |
   | `assistant` text block | Assistant response text |
   | `assistant` tool_use block | Tool name, formatted input, and result (if available) |
   | `progress` | Hook notifications — not surfaced |

7. **Inline subagent expansion** — When an `Agent` tool call is encountered and its result metadata contains an `agentId`, the script locates the corresponding subagent JSONL file and recursively renders its full conversation inside a collapsible `<details>` block.

### Event types

The JSONL files contain six event types:

| Type | Description |
|---|---|
| `queue-operation` | Task enqueue (`content` present) or dequeue. Main session only. |
| `user` | User prompts, tool results, and system-injected messages. Content is either a plain string (initial prompt) or an array of `tool_result` objects. |
| `assistant` | Model responses. Content blocks can be `thinking` (extended thinking with signature), `text`, or `tool_use`. Multiple events may share one `message.id` (streaming). |
| `progress` | Hook execution notifications (e.g., `PostToolUse:Read`). Subagents only. |

### Key fields

All non-queue events share: `type`, `uuid`, `parentUuid`, `timestamp`, `sessionId`, `cwd`, `version`, `gitBranch`, `isSidechain`, `userType`.

- `parentUuid` forms a linked list threading events together
- `slug` is a human-readable session name (may be absent on early events)
- `agentId` is present only in subagent files
- `toolUseResult` on user events carries rich metadata about the tool execution (file info for Read, file lists for Glob, match counts for Grep, HTTP status for WebFetch, completion stats for Agent)
- `sourceToolAssistantUUID` on user events points back to the assistant event that issued the tool call

### Tool-specific rendering

Each tool type has tailored rendering:

| Tool | Displayed fields |
|---|---|
| Read | File path, line count |
| Write | File path |
| Edit | File path, diff (old/new strings) |
| Glob | Pattern, matched file list |
| Grep | Pattern, file glob, match count |
| Bash | Command in code block |
| Agent | Subagent type, description, prompt; full subagent log inline |
| WebFetch | URL, prompt, HTTP status, duration |
| ToolSearch | Query |

### Output structure

```markdown
# Session: <slug>

## Metadata          — table with session ID, times, duration, cwd, branch, model
## Statistics        — token totals, tool counts, subagent/error counts
### Tool Usage       — breakdown by tool name
## Task              — initial user prompt
## Session Flow      — numbered steps for every event
  ### Step N: ...    — thinking, responses, tool calls with results
    <details>        — subagent conversations nested inside collapsible blocks
```
