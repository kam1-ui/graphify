#!/usr/bin/env python3
"""Turn Claude Code session logs into clean Markdown ready for graphify.

Claude Code stores every session as JSONL under
~/.claude/projects/<encoded-workspace>/<session>.jsonl. Those files are mostly
noise (queue ops, file snapshots, tool calls, internal "thinking") with the
real conversation — your prompts and the assistant's prose answers — buried in
it. This extracts just that conversation into one Markdown file per session,
with YAML frontmatter (date, project, session id), so graphify can ingest your
past sessions as first-class documents and you can query "what did we decide
about X" against the graph instead of re-reading transcripts.

This is the conversation-ingestion preprocessor (graphify issue #425, Level 1):
graphify itself is untouched. A separate `graphify extract` turns the produced
Markdown into graph nodes (paid Pass 3 — Gemini, NOT Claude, which would burn a
session).

SECURITY: session logs often contain secrets you pasted (API keys, cookies).
Those are redacted before anything is written or sent to a model.

Works for ANY workspace, not just this repo: pass the workspace name (the
directory under ~/.claude/projects, with or without the path-encoding). The
session files are global, so this runs the same from anywhere — which is why
it's meant to be installed as a global `parse-sessions` command.

ponytail: regex secret redaction covers the patterns we've actually seen
(Google AQ./AIza, GitHub ghp_/gho_, Netscape cookies, bearer-ish blobs). It is
a denylist, not a guarantee — add patterns as new secret shapes appear.

Usage:
    parse-sessions graphify                 # -> ./sessions-out/*.md
    parse-sessions graphify -o ~/vault/conv # custom output dir
    parse-sessions --list                   # list known workspaces
    python tools/parse_sessions.py          # self-check
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"

# Redaction patterns for secrets that show up in pasted chat content.
_SECRET_PATTERNS = [
    re.compile(r"\bAQ\.[A-Za-z0-9_\-]{20,}"),          # Google AI Studio key (new)
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"),          # Google API key (legacy)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),       # GitHub tokens
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),              # OpenAI-style keys
    re.compile(r"\b(?:__Secure-|__Host-)[A-Za-z0-9_\-]+\t.*"),  # cookie lines
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),    # Slack tokens
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _blocks_text(content) -> list[str]:
    """Pull human-readable text blocks out of a message's content."""
    if isinstance(content, str):
        return [content] if content.strip() else []
    out = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text", "").strip()
            if t:
                out.append(t)
    return out


def parse_session(path: Path) -> tuple[str, dict] | None:
    """Extract the conversation from one .jsonl into (markdown, metadata)."""
    turns: list[tuple[str, str]] = []  # (role, text)
    first_ts = None
    session_id = path.stem
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = o.get("message", {})
        # A "user" entry that is really a tool_result is noise, not a prompt.
        content = msg.get("content")
        if t == "user" and isinstance(content, list) and not any(
            isinstance(b, dict) and b.get("type") == "text" for b in content
        ):
            continue
        texts = _blocks_text(content)
        if not texts:
            continue  # assistant thinking/tool_use only -> skip
        if first_ts is None:
            first_ts = o.get("timestamp")
        turns.append((t, "\n\n".join(texts)))

    if not turns:
        return None

    date = (first_ts or "")[:10] or "unknown"
    lines = [
        "---",
        f"date: {date}",
        f"session_id: {session_id}",
        f"source: claude-code-session",
        "tags:",
        "  - conversation",
        "---",
        "",
        f"# Session {session_id[:8]} ({date})",
        "",
    ]
    for role, text in turns:
        who = "User" if role == "user" else "Assistant"
        lines.append(f"## {who}")
        lines.append("")
        lines.append(redact(text))
        lines.append("")
    return "\n".join(lines), {"date": date, "session_id": session_id, "turns": len(turns)}


def _find_workspace_dir(name: str) -> Path | None:
    """Resolve a workspace name to its ~/.claude/projects/<dir>."""
    if not PROJECTS.exists():
        return None
    cand = PROJECTS / name
    if cand.is_dir():
        return cand
    # Claude encodes paths as -home-dev-work-<name>; match by suffix.
    matches = [d for d in PROJECTS.iterdir() if d.is_dir() and d.name.endswith(name)]
    return matches[0] if len(matches) == 1 else None


def parse_workspace(name: str, out_dir: Path) -> int:
    wd = _find_workspace_dir(name)
    if wd is None:
        print(f"  ! workspace not found: {name} (try --list)", file=sys.stderr)
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for jsonl in sorted(wd.glob("*.jsonl")):
        result = parse_session(jsonl)
        if result is None:
            continue
        md, meta = result
        (out_dir / f"{jsonl.stem}.md").write_text(md, encoding="utf-8")
        print(f"  {jsonl.stem[:8]}  {meta['date']}  {meta['turns']:4} turns")
        n += 1
    print(f"wrote {n} session(s) -> {out_dir}", file=sys.stderr)
    return n


def _self_check() -> int:
    import tempfile

    sample = [
        {"type": "queue-operation", "operation": "enqueue"},  # noise
        {"type": "user", "timestamp": "2026-06-30T10:00:00Z",
         "message": {"content": [{"type": "text", "text": "ma cle est AQ.Ab8RN6SECRETkey1234567890 garde-la"}]}},
        {"type": "assistant",
         "message": {"content": [{"type": "thinking", "thinking": "hidden"},
                                  {"type": "text", "text": "Compris, je la masque."}]}},
        {"type": "user",  # tool_result masquerading as user -> dropped
         "message": {"content": [{"tool_use_id": "x", "type": "tool_result", "content": "out"}]}},
    ]
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "sess.jsonl"
        f.write_text("\n".join(json.dumps(o) for o in sample), encoding="utf-8")
        md, meta = parse_session(f)
        assert meta["turns"] == 2, meta  # 1 user + 1 assistant text; noise dropped
        assert "## User" in md and "## Assistant" in md, md
        assert "Compris" in md and "hidden" not in md, "thinking must be dropped"
        assert "AQ.Ab8RN6SECRET" not in md and "[REDACTED]" in md, "secret must be redacted"
        assert "tool_result" not in md and "out" not in md.split("Assistant")[0], "tool noise leaked"
        assert meta["date"] == "2026-06-30", meta
    # redact() unit
    assert redact("token ghp_ABCDEFGHIJKLMNOPQRST1234") == "token [REDACTED]"
    assert redact("plain text stays") == "plain text stays"
    print("self-check OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude session logs -> clean Markdown for graphify.")
    ap.add_argument("workspace", nargs="?", help="workspace name (omit to self-check)")
    ap.add_argument("-o", "--output", help="output dir (default: ./sessions-out)")
    ap.add_argument("--list", action="store_true", help="list known workspaces and exit")
    args = ap.parse_args()

    if args.list:
        if not PROJECTS.exists():
            print("no ~/.claude/projects found")
            return 0
        for d in sorted(PROJECTS.iterdir()):
            if d.is_dir():
                n = len(list(d.glob("*.jsonl")))
                print(f"  {d.name}  ({n} session{'s' if n != 1 else ''})")
        return 0

    if not args.workspace:
        return _self_check()

    out = Path(args.output) if args.output else Path("sessions-out")
    n = parse_workspace(args.workspace, out)
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
