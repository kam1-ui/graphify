#!/usr/bin/env python3
"""Adversarial code review with Gemini, grounded in the graphify graph.

Pasting isolated code snippets into an LLM review hides the connections that
matter: who calls this, what community it lives in, what a change ripples into.
This tool feeds Gemini the relevant SUBGRAPH (via `graphify query`, token-
bounded) plus the source of the files that subgraph points at, so the review
reasons about real structure, not a fragment.

graphify is immutable — this only reads its public CLI output (graph.json via
`graphify query`) and the repo's files.

ponytail: subgraph is whatever `graphify query --budget` returns for the topic;
file set is parsed from its `[src=path ...]` lines. A topic that doesn't match
the graph yields a thin subgraph — pick concept/file names that appear in it
(use `graphify query <topic>` first to check).

Usage:
    python tools/gemini_review.py "Is the backend selection correct?" --topic "backend LLM"
    python tools/gemini_review.py "Review my change" --topic "chunking" --diff
    python tools/gemini_review.py        # self-check (no graph/network)
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

MODEL = "gemini-2.5-pro"  # reasoning model for review, not extraction
# Capture both the file and (when present) the line: "[src=graphify/llm.py loc=L931 ...]"
_SRC = re.compile(r"\[src=([^\s\]]+)(?:\s+loc=L(\d+))?")
_WINDOW = 60  # lines of context to pull around each cited node line


def _gemini_key() -> str:
    k = os.environ.get("GEMINI_API_KEY", "")
    if not k:
        env = Path.home() / ".gemini" / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    k = line.split("=", 1)[1].strip().strip('"')
    return k


def subgraph(topic: str, budget: int) -> str:
    """Token-bounded subgraph for a topic via `graphify query`."""
    out = subprocess.run(
        ["graphify", "query", topic, "--budget", str(budget)],
        capture_output=True, text=True,
    )
    # graphify prints warnings to stderr; the graph is on stdout.
    return out.stdout


def files_in(subgraph_text: str, repo: Path, max_files: int) -> dict[Path, set[int]]:
    """Map each referenced source file -> the set of node line numbers cited.

    Returns an ordered dict (insertion = first-seen order). A file with no line
    (loc absent) maps to an empty set, meaning "no specific line — whole-file
    fallback". Lines let us window around the actual code the subgraph points
    at, instead of truncating big files from the top (which missed e.g.
    llm.py:931 where the real logic lives).
    """
    files: dict[Path, set[int]] = {}
    for m in _SRC.finditer(subgraph_text):
        p = m.group(1)
        if not p:
            continue
        fp = repo / p
        if not fp.is_file():
            continue
        if fp not in files:
            if len(files) >= max_files:
                continue
            files[fp] = set()
        if m.group(2):
            files[fp].add(int(m.group(2)))
    return files


def _snippet(fp: Path, lines: set[int]) -> str:
    """Code for a file: windows around cited lines, or the head if none."""
    text = fp.read_text(encoding="utf-8", errors="replace")
    src = text.splitlines()
    if not lines:
        return "\n".join(src[:200])  # no line info -> head fallback
    # Merge overlapping windows so a function isn't shown twice.
    wanted: set[int] = set()
    for ln in lines:
        wanted.update(range(max(0, ln - 1 - _WINDOW), min(len(src), ln - 1 + _WINDOW)))
    out, prev = [], -2
    for i in sorted(wanted):
        if i != prev + 1:
            out.append(f"... (lines around {i + 1}) ...")
        out.append(f"{i + 1}\t{src[i]}")
        prev = i
    return "\n".join(out)


def build_prompt(question: str, sub: str, files: dict[Path, set[int]], diff: str) -> str:
    parts = [
        "You are an adversarial senior reviewer. Reason about the STRUCTURE, "
        "not just lines: callers/callees, communities, ripple effects. "
        "Be concrete and concise (max 350 words).\n",
        f"QUESTION:\n{question}\n",
        "RELEVANT SUBGRAPH (from graphify — who connects to what):\n"
        f"{sub.strip()[:6000]}\n",
    ]
    for f, lines in files.items():
        parts.append(f"FILE {f} (windowed around the cited lines):\n```\n{_snippet(f, lines)}\n```\n")
    if diff:
        parts.append(f"UNCOMMITTED DIFF under review:\n```diff\n{diff[:8000]}\n```\n")
    parts.append("Give: (a) correctness risks, (b) structural impacts the graph "
                 "reveals, (c) the simplest fix. Flag if the subgraph is too thin "
                 "to judge.")
    return "\n".join(parts)


def call_gemini(prompt: str, key: str) -> str:
    import json
    import urllib.request

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={key}"
    data = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    if "candidates" not in d:
        return f"[gemini error] {json.dumps(d)[:300]}"
    return d["candidates"][0]["content"]["parts"][0]["text"]


def _self_check() -> int:
    # No graph / no network: verify file parsing + prompt assembly.
    sample = (
        "NODE llm.py [src=graphify/llm.py loc=L1 community=21]\n"
        "NODE detect.py [src=graphify/detect.py loc=L1 community=20]\n"
        "NODE Path [src= loc= community=37]\n"        # empty src -> skipped
        "NODE call [src=graphify/llm.py loc=L931 community=21]\n"  # same file, 2nd line
    )
    repo = Path(__file__).resolve().parent.parent
    files = files_in(sample, repo, max_files=10)
    names = [f.name for f in files]
    assert "llm.py" in names and "detect.py" in names, names
    assert names.count("llm.py") == 1, "must dedup files"
    # both cited lines for llm.py are collected
    llm = next(f for f in files if f.name == "llm.py")
    assert files[llm] == {1, 931}, files[llm]
    assert all(f.is_file() for f in files), "only existing files"
    # windowed snippet around L931 actually contains line 931's code, NOT L1's head
    snip = _snippet(llm, files[llm])
    assert "931\t" in snip, "should window around the cited line"
    prompt = build_prompt("Is X correct?", sample, files, diff="")
    assert "QUESTION:" in prompt and "RELEVANT SUBGRAPH" in prompt
    assert "FILE" in prompt and "Is X correct?" in prompt
    assert not any(str(f) == str(repo) for f in files)  # empty-src never a file
    print("self-check OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Graph-grounded Gemini code review.")
    ap.add_argument("question", nargs="?", help="what to review (omit to self-check)")
    ap.add_argument("--topic", help="concept/file to center the subgraph on (default: question)")
    ap.add_argument("--budget", type=int, default=1500, help="subgraph token budget")
    ap.add_argument("--max-files", type=int, default=4, help="max files to include")
    ap.add_argument("--diff", action="store_true", help="also include the uncommitted git diff")
    ap.add_argument("--repo", default=".", help="repo root")
    args = ap.parse_args()

    if not args.question:
        return _self_check()

    key = _gemini_key()
    if not key:
        ap.error("no GEMINI_API_KEY (env or ~/.gemini/.env)")

    repo = Path(args.repo).resolve()
    sub = subgraph(args.topic or args.question, args.budget)
    files = files_in(sub, repo, args.max_files)
    diff = ""
    if args.diff:
        diff = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True).stdout

    print(f"  subgraph: {sub.count('NODE ')} nodes | files: {[f.name for f in files]}", file=sys.stderr)
    prompt = build_prompt(args.question, sub, files, diff)
    print(call_gemini(prompt, key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
