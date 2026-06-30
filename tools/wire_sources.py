#!/usr/bin/env python3
"""Wire every node note in a graphify Obsidian vault to its source document.

`graphify export obsidian` writes one note per node with a plain-text
`source_file:` field in the frontmatter, but it does NOT bring the source
documents into the vault and the field is not a clickable link. So in Obsidian
each concept note is "bare bones": you can't jump from a node to the text it
came from. This reproduces, as an external post-processor, the manual step the
"Graphify + Obsidian = CHEAT CODE" tutorial does in natural language:
"pull the source docs in and wire every node to its origin".

graphify is immutable — this never touches graphify. It runs AFTER export,
operating only on the produced vault folder plus the original source files.

What it does:
  1. Copies each source document into <vault>/sources/ as Markdown.
  2. Appends a `## Source` section to each node note with a clickable
     [[sources/<doc>]] wikilink to its origin.

Source resolution (the CHEAT CODE point): the tutorial wires nodes to the real
*document* (146 docs), not to the extraction *chunk* (658 stubs). For a single
video that means every node points at the one full transcript, not at the
Chonkie fragment it happened to be extracted from. Pass --map to collapse many
chunks onto one canonical doc:

    --map cheatcode_part:cheatcode-transcript.txt

means "any source_file starting with 'cheatcode_part' -> this one doc". Without
a map, each node links to its own source_file copied verbatim.

Idempotent: re-running won't duplicate the `## Source` section. Community notes
(no source_file) are left untouched.

ponytail: prefix-match mapping, not a full router. One video / one prefix is
the case we have; for a real multi-source batch the upgrade is a manifest
(json) mapping each source_file to its canonical doc + URL + timestamp.

Usage:
    python tools/wire_sources.py <vault> --src-dir <dir-with-source-files>
    python tools/wire_sources.py <vault> --src-dir in/ --map cheatcode_part:transcript.txt
    python tools/wire_sources.py        # self-check
"""
import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

_SOURCE_FILE = re.compile(r'^source_file:\s*"?(.*?)"?\s*$', re.MULTILINE)
_NODE_LABEL = re.compile(r'^# (.+)$', re.MULTILINE)
_MARKER = "## Source"


def _load_sidecar(src_dir: Path, canonical: str) -> dict | None:
    """Load <canonical>.map.json (the timestamp sidecar) if it exists."""
    stem = _vault_link_name(canonical)
    p = src_dir / f"{stem}.map.json"
    if not p.exists():
        return None
    import json
    return json.loads(p.read_text(encoding="utf-8"))


def _timestamp_for(label: str, source_text: str, sidecar: dict) -> dict | None:
    """Pick the video timestamp for a node (decision: label-search w/ fallback).

    Find where the node's label appears in the source text; map that char
    position to the latest segment that starts at or before it. If the label
    isn't found verbatim, fall back to the first segment (start of the doc).
    Returns the chosen segment dict ({offset,start,hms}) or None if no segments.
    """
    segs = sidecar.get("segments") or []
    if not segs:
        return None
    pos = source_text.lower().find(label.lower())
    if pos < 0:
        return segs[0]  # fallback: couldn't locate the label, use doc start
    candidates = [s for s in segs if s["offset"] <= pos]
    return max(candidates, key=lambda s: s["offset"]) if candidates else segs[0]


def _video_link(sidecar: dict, seg: dict) -> str:
    """Build a clickable deep-link into the local video at the timestamp."""
    video = sidecar.get("video", "")
    # ponytail: local file link with a media fragment (#t=seconds). Obsidian
    # opens it; the fragment lets players seek. Upgrade: emit a YouTube
    # ?t=Ns link instead when the video lives online (we keep start in s).
    return f"[{seg['hms']}](file://{video}#t={int(seg['start'])})"


def _resolve(source_file: str, mapping: list[tuple[str, str]]) -> str:
    """Map a node's source_file to its canonical source doc name."""
    for prefix, canonical in mapping:
        if source_file.startswith(prefix):
            return canonical
    return source_file


def _vault_link_name(doc_name: str) -> str:
    """Filename stem used inside the vault (always .md, no double extension)."""
    stem = re.sub(r"\.(txt|md|markdown|vtt|srt)$", "", doc_name, flags=re.IGNORECASE)
    return stem


def wire(vault: Path, src_dir: Path, mapping: list[tuple[str, str]]) -> tuple[int, int]:
    """Returns (notes_wired, sources_copied)."""
    sources_dir = vault / "sources"
    sources_dir.mkdir(exist_ok=True)

    wired = 0
    copied: set[str] = set()
    # Cache per canonical doc: its source text + timestamp sidecar (if any).
    cache: dict[str, tuple[str, dict | None]] = {}

    for note in vault.glob("*.md"):
        text = note.read_text(encoding="utf-8")
        m = _SOURCE_FILE.search(text)
        if not m or not m.group(1).strip():
            continue  # community notes & anything without a source
        if _MARKER in text:
            continue  # idempotent: already wired

        canonical = _resolve(m.group(1).strip(), mapping)
        link_stem = _vault_link_name(canonical)
        src = src_dir / canonical

        # Copy the source into the vault (once), as .md so Obsidian opens it.
        if canonical not in copied:
            dst = sources_dir / f"{link_stem}.md"
            if src.exists():
                if not dst.exists():
                    shutil.copyfile(src, dst)
                copied.add(canonical)
            # ponytail: if the source file is missing we still wire the link
            # (dangling link in Obsidian) rather than skip — the node should
            # always advertise where it came from, even if the doc isn't here.

        # Load source text + timestamp sidecar once per canonical doc.
        if canonical not in cache:
            stext = src.read_text(encoding="utf-8") if src.exists() else ""
            cache[canonical] = (stext, _load_sidecar(src_dir, canonical))
        source_text, sidecar = cache[canonical]

        section = f"{_MARKER}\n- [[sources/{link_stem}]]"
        # If we have timestamps, deep-link this node into the video.
        if sidecar:
            label_m = _NODE_LABEL.search(text)
            label = label_m.group(1).strip() if label_m else ""
            seg = _timestamp_for(label, source_text, sidecar) if label else None
            if seg:
                section += f"\n- {_video_link(sidecar, seg)}"
        # Insert before the trailing inline-tags line if present (graphify ends
        # each note with a "#graphify/... " tag line), else append at the end.
        lines = text.rstrip("\n").split("\n")
        if lines and lines[-1].lstrip().startswith("#graphify/"):
            body = "\n".join(lines[:-1]).rstrip("\n")
            note.write_text(f"{body}\n\n{section}\n\n{lines[-1]}\n", encoding="utf-8")
        else:
            note.write_text(text.rstrip("\n") + f"\n\n{section}\n", encoding="utf-8")
        wired += 1

    return wired, len(copied)


def _self_check() -> int:
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        srcdir = Path(td) / "in"
        vault.mkdir()
        srcdir.mkdir()
        (srcdir / "transcript.txt").write_text("full transcript text", encoding="utf-8")

        node = vault / "Knowledge Graph.md"
        node.write_text(
            '---\nsource_file: "cheatcode_part02.txt"\ntype: "concept"\n---\n\n'
            "# Knowledge Graph\n\n## Connections\n- [[Graphify]] - `part_of`\n\n"
            "#graphify/concept #graphify/EXTRACTED\n",
            encoding="utf-8",
        )
        community = vault / "_COMMUNITY_Community 1.md"
        community.write_text("---\ntype: community\n---\n\n# Community 1\n", encoding="utf-8")

        mapping = [("cheatcode_part", "transcript.txt")]
        wired, copied = wire(vault, srcdir, mapping)
        out = node.read_text(encoding="utf-8")

        assert wired == 1 and copied == 1, (wired, copied)
        assert (vault / "sources" / "transcript.md").exists(), "source not copied"
        assert "## Source" in out and "[[sources/transcript]]" in out, out
        # community note untouched
        assert "## Source" not in community.read_text(encoding="utf-8")
        # inline tags still last line
        assert out.rstrip().endswith("#graphify/EXTRACTED"), out
        # idempotent: second run adds nothing
        w2, _ = wire(vault, srcdir, mapping)
        assert w2 == 0, "second run should wire nothing"
        assert node.read_text(encoding="utf-8").count("## Source") == 1

    # --- timestamp path: a sidecar present -> node gets a deep-link ---
    import json as _json

    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        srcdir = Path(td) / "in"
        vault.mkdir()
        srcdir.mkdir()
        # source text: "Knowledge Graph" appears at a known offset.
        src_text = "intro line about graphify\nThe Knowledge Graph maps concepts."
        (srcdir / "lesson.txt").write_text(src_text, encoding="utf-8")
        (srcdir / "lesson.map.json").write_text(
            _json.dumps({
                "video": "/videos/lesson.mp4",
                "segments": [
                    {"offset": 0, "start": 0.0, "hms": "00:00:00"},
                    {"offset": 26, "start": 833.0, "hms": "00:13:53"},
                ],
            }),
            encoding="utf-8",
        )
        node = vault / "Knowledge Graph.md"
        node.write_text(
            '---\nsource_file: "lesson.txt"\ntype: "concept"\n---\n\n'
            "# Knowledge Graph\n\n#graphify/concept\n",
            encoding="utf-8",
        )
        wire(vault, srcdir, [])
        out = node.read_text(encoding="utf-8")
        # "Knowledge Graph" is at offset 30 (>=26) -> second segment, 13:53.
        assert "00:13:53" in out, out
        assert "file:///videos/lesson.mp4#t=833" in out, out
        # a node whose label isn't in the text falls back to the first segment.
        node2 = vault / "Absent Concept.md"
        node2.write_text(
            '---\nsource_file: "lesson.txt"\ntype: "concept"\n---\n\n'
            "# Absent Concept\n\n#graphify/concept\n",
            encoding="utf-8",
        )
        wire(vault, srcdir, [])
        out2 = node2.read_text(encoding="utf-8")
        assert "00:00:00" in out2, out2  # fallback to doc start

    print("self-check OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Wire vault nodes to their source docs.")
    ap.add_argument("vault", nargs="?", help="Obsidian vault dir (omit to self-check)")
    ap.add_argument("--src-dir", help="dir holding the original source files")
    ap.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="PREFIX:DOC",
        help="collapse source_files starting with PREFIX onto DOC (repeatable)",
    )
    args = ap.parse_args()

    if not args.vault:
        return _self_check()
    if not args.src_dir:
        ap.error("--src-dir is required when wiring a vault")

    mapping: list[tuple[str, str]] = []
    for pair in args.map:
        if ":" not in pair:
            ap.error(f"--map must be PREFIX:DOC, got {pair!r}")
        prefix, doc = pair.split(":", 1)
        mapping.append((prefix, doc))

    wired, copied = wire(Path(args.vault), Path(args.src_dir), mapping)
    print(f"wired {wired} note(s), copied {copied} source doc(s) into sources/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
