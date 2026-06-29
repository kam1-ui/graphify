#!/usr/bin/env python3
"""Clean a raw transcript before semantic extraction, locally and for free.

A raw Whisper / YouTube auto-caption transcript is padded with noise that costs
Gemini tokens for zero signal: bracketed sound cues ([Music], [Applause]),
filler words (uh, um, you know), false starts, and duplicate lines that
auto-captions emit as the caption scrolls. Stripping that noise *before* the
paid LLM call is the single biggest lever on extraction cost — every removed
token is a token we don't pay Gemini to read.

This is deterministic regex cleanup, not an LLM pass: free, offline, batchable
over a 100h+ backlog. It sits at "stage 0" of the pipeline, between transcribe
and chunk:

    yt-dlp -> faster-whisper -> clean.py -> chunk -> graphify+gemini -> vault

Input format matches graphify's transcribe.py output (one Whisper segment per
line, plain text, newline-joined) but also tolerates a single blob of prose.

ponytail: regex cleanup, not semantic. It will not fix grammar or merge
sentences split mid-clause across segments. The upgrade path, if the regex
leaves too much noise on a given source, is a one-shot Gemini "tidy this
transcript" pass — but that costs tokens, so measure with --stats first.

Usage:
    python tools/clean.py raw.txt                  # -> stdout
    python tools/clean.py raw.txt -o clean.txt     # -> file
    python tools/clean.py raw.txt --stats          # report token reduction
    python tools/clean.py                          # run self-check
"""
import argparse
import re
import sys
from pathlib import Path

# Bracketed/parenthesized sound cues Whisper & auto-captions emit:
# [Music], [Applause], (laughs), [BLANK_AUDIO], ♪ ... ♪
_SOUND_CUE = re.compile(r"[\[(][^\])]*[\])]|♪[^♪]*♪")

# Standalone filler words. Bounded so we don't gut real words ("um" in "album").
# "you know" / "I mean" / "sort of" / "kind of" as verbal tics are left alone:
# they sometimes carry meaning and over-stripping risks mangling sentences.
_FILLER = re.compile(r"\b(?:uh+|um+|erm+|hmm+|uh-huh)\b[,.]?\s*", re.IGNORECASE)

# Collapse 3+ repeated words ("the the the" -> "the") — a common ASR stutter.
_WORD_STUTTER = re.compile(r"\b(\w+)(?:\s+\1\b){2,}", re.IGNORECASE)


def _dedup_adjacent_lines(lines: list[str]) -> list[str]:
    """Drop consecutive identical lines (auto-caption scroll artifact)."""
    out: list[str] = []
    for ln in lines:
        if not out or out[-1].strip().lower() != ln.strip().lower():
            out.append(ln)
    return out


def clean(text: str) -> str:
    lines = text.splitlines() or [text]
    lines = _dedup_adjacent_lines(lines)

    cleaned: list[str] = []
    for ln in lines:
        ln = _SOUND_CUE.sub(" ", ln)
        ln = _FILLER.sub("", ln)
        ln = _WORD_STUTTER.sub(r"\1", ln)
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            cleaned.append(ln)
    return "\n".join(cleaned)


def _approx_tokens(text: str) -> int:
    # ponytail: ~4 chars/token heuristic (good enough for a before/after ratio;
    # the real tokenizer is gemini's and we don't ship it). Upgrade: tiktoken.
    return max(1, len(text) // 4)


def _self_check() -> int:
    raw = (
        "[Music]\n"
        "So uh this is the the the knowledge graph, you know.\n"
        "So uh this is the the the knowledge graph, you know.\n"
        "(laughs) Um, it maps concepts.\n"
        "♪ outro ♪\n"
    )
    got = clean(raw)
    # Sound cues gone, filler gone, stutter collapsed, dup line dropped.
    assert "[Music]" not in got and "♪" not in got and "(laughs)" not in got, got
    assert "uh" not in got.lower().split() and "um" not in got.lower().split(), got
    assert "the the" not in got, got
    assert got.count("knowledge graph") == 1, got  # duplicate line removed
    assert "maps concepts" in got, got
    assert _approx_tokens(raw) > _approx_tokens(got), "should shrink"
    # "album" must survive the um filler rule (word-boundary check).
    assert clean("the album cover") == "the album cover"
    print("self-check OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Clean a transcript before extraction.")
    ap.add_argument("input", nargs="?", help="raw transcript (omit to self-check)")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    ap.add_argument("--stats", action="store_true", help="report token reduction")
    args = ap.parse_args()

    if not args.input:
        return _self_check()

    raw = Path(args.input).read_text(encoding="utf-8")
    out = clean(raw)

    if args.stats:
        ti, to = _approx_tokens(raw), _approx_tokens(out)
        pct = 100 * (ti - to) / ti
        print(f"~tokens: {ti} -> {to}  (-{pct:.0f}%)", file=sys.stderr)

    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
