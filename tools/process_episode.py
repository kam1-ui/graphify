#!/usr/bin/env python3
"""Process one video episode end to end into a standardized OKF output.

The daily driver: drop a video in a-traiter/, run this with its path (or a url),
and get a wired Obsidian vault PLUS an episode fiche and an updated index — the
same shape every time, so a community can rely on it. ONE graph, ONE pipeline:
this wraps ingest.py (which already does video→graph→wired-vault) and adds the
two navigation pages on top.

Output per episode (graphify's native graphify-out/ layout is left UNTOUCHED):
    episodes/INDEX.md            one line per episode (the listing asset)
    episodes/<slug>/
        episode.md               OKF fiche — type:episode + metadata + god nodes
        graphify-out/obsidian/   the wired vault (notes, sources/, timestamps)

The fiche + index are generated AFTER processing ("réinjection"): they read the
produced graph (god nodes) and stats, so the index reflects the REAL content,
not just a filename. This is free (no extra LLM call) and stays pure-markdown
(OKF) — no vector store, no second graph, no second source of truth.

Metadata for protected/streamed content (no accessible metadata like YouTube):
duration is auto (ffprobe); title/author come from --title/--author flags, or
interactive prompts when a human is at the terminal. ZERO token for metadata.

Safety (from adversarial review):
- ATOMICITY: the video is moved to traite/ only as the LAST step, after the
  fiche+index succeed. Any failure (or the preflight cost-gate stopping) leaves
  the video in a-traiter/ — an unprocessed video is never lost.
- IDEMPOTENCY: if episodes/<slug>/ already exists, stop (use --force to redo);
  INDEX.md updates the slug's line in place rather than appending a duplicate.
- COST-GATE: the paid extract runs only with --yes; non-interactive without
  --yes stops at the gate. An AI must pass --yes deliberately.

Usage:
    python tools/process_episode.py a-traiter/ep01.mp4 --title "..." --author "..." --yes
    python tools/process_episode.py https://...                 # downloads first
    python tools/process_episode.py        # self-check
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
INGEST = HERE / "ingest.py"
A_TRAITER = REPO / "a-traiter"
TRAITE = REPO / "traite"
EPISODES = REPO / "episodes"
INDEX = EPISODES / "INDEX.md"

_URL = re.compile(r"^https?://", re.IGNORECASE)


def slugify(name: str) -> str:
    s = re.sub(r"\.(mp4|mkv|webm|mov|m4a|mp3|wav)$", "", name, flags=re.IGNORECASE)
    s = re.sub(r"[^\w]+", "-", s.lower()).strip("-")
    return s or "episode"


def trim_video(video: Path, until: str) -> Path:
    """Cut the video at `until` (HH:MM:SS) before processing, free and local.

    Recorded streams often have dead tail (frozen frame, no audio — e.g. you
    fell asleep). Trimming to the real content cuts transcription time in half
    and avoids Whisper hallucinating on silence. `-c copy` stream-copies (no
    re-encode), so it's near-instant. The trimmed file becomes the reference,
    so timestamps stay consistent — no remapping needed (nothing useful is past
    `until` anyway).
    """
    out = video.with_name(f"{video.stem}.trim{video.suffix}")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-t", until, "-c", "copy", str(out)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not out.exists():
        raise SystemExit(f"trim failed: {r.stderr[-300:]}")
    print(f"  trimmed to {until} -> {out.name}", flush=True)
    return out


def ffprobe_duration(video: Path) -> str:
    """HH:MM:SS via ffprobe, or '?' if unavailable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True,
        )
        secs = int(float(out.stdout.strip()))
        return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
    except (ValueError, OSError):
        return "?"


def _content_duration(sidecar: Path | None, video: Path) -> str:
    """Real content length: last sidecar timestamp if available, else ffprobe.

    Uses the transcript's last segment so the fiche shows the actual content
    duration (e.g. 2h), not the raw recording (e.g. 3h46 with a dead tail).
    """
    if sidecar is not None and sidecar.exists():
        try:
            segs = json.loads(sidecar.read_text(encoding="utf-8")).get("segments") or []
            if segs:
                s = int(segs[-1]["start"])
                return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
        except (ValueError, KeyError):
            pass
    return ffprobe_duration(video)


def god_nodes(graph_json: Path, n: int = 8) -> list[str]:
    """The n most-connected concept labels — free, read from the graph."""
    g = json.loads(graph_json.read_text(encoding="utf-8"))
    id2label = {x["id"]: x.get("label", x["id"]) for x in g.get("nodes", [])}
    deg: Counter = Counter()
    for l in g.get("links", []):
        deg[l["source"]] += 1
        deg[l["target"]] += 1
    return [id2label.get(i, i) for i, _ in deg.most_common(n)]


def graph_stats(graph_json: Path) -> dict:
    g = json.loads(graph_json.read_text(encoding="utf-8"))
    comms = {x.get("community") for x in g.get("nodes", []) if x.get("community") is not None}
    return {"nodes": len(g.get("nodes", [])), "edges": len(g.get("links", [])),
            "communities": len(comms)}


def write_fiche(ep_dir: Path, slug: str, title: str, author: str, duration: str,
                source: str, topics: list[str], stats: dict) -> Path:
    """The OKF episode fiche (read first; cascade entry point)."""
    lines = [
        "---",
        "type: episode",                       # OKF required field
        f'title: "{title}"',
        f"slug: {slug}",
        f'author: "{author}"',
        f"date: {date.today().isoformat()}",
        f"duration: {duration}",
        f'source: "{source}"',
        "resource: \"[[graphify-out/obsidian/sources/" + slug + "-transcript]]\"",
        "topics:",
        *[f"  - {t}" for t in topics],
        "tags:",
        "  - episode",
        "stats:",
        f"  nodes: {stats['nodes']}",
        f"  edges: {stats['edges']}",
        f"  communities: {stats['communities']}",
        "---",
        "",
        f"# {title}",
        "",
        "> Fiche d'épisode. Détail : "
        "[graph report](./graphify-out/GRAPH_REPORT.md) · "
        "[vault](./graphify-out/obsidian/)",
        "",
        "## Concepts clés",
        *[f"- {t}" for t in topics],
        "",
    ]
    p = ep_dir / "episode.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def update_index(slug: str, title: str, duration: str, stats: dict) -> None:
    """Append or update the episode's line in INDEX.md (idempotent by slug)."""
    EPISODES.mkdir(parents=True, exist_ok=True)
    header = "# Episodes index\n\n| slug | title | date | duration | nodes |\n|---|---|---|---|---|\n"
    row = (f"| [{slug}]({slug}/episode.md) | {title} | {date.today().isoformat()} "
           f"| {duration} | {stats['nodes']} |")
    existing = INDEX.read_text(encoding="utf-8") if INDEX.exists() else header
    lines = existing.splitlines()
    # Replace a line already referencing this slug, else append.
    marker = f"[{slug}]("
    replaced = False
    for i, ln in enumerate(lines):
        if marker in ln:
            lines[i] = row
            replaced = True
            break
    if not replaced:
        lines.append(row)
    INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")


def process(src: str, title: str | None, author: str | None, proceed: bool,
            force: bool, interactive: bool, until: str | None = None,
            transcript_done: Path | None = None, sidecar: Path | None = None,
            token_budget: int = 4000) -> Path:
    A_TRAITER.mkdir(exist_ok=True)
    TRAITE.mkdir(exist_ok=True)

    # 1-2. Resolve input: a url downloads into a-traiter/ first (so ffprobe has
    # the file); a path is used as-is.
    if _URL.match(src):
        original = _download(src)
    else:
        original = Path(src)
        if not original.exists():
            raise SystemExit(f"not found: {original}")

    # 4. Resolve title/author FIRST so the slug can be built from the title
    # (a readable slug like "live-session-14-may-2026" beats the raw filename).
    if title is None:
        title = input(f"Titre [{original.stem}] : ").strip() if interactive else original.stem
        title = title or original.stem
    if author is None:
        author = input("Auteur [unknown] : ").strip() if interactive else "unknown"
        author = author or "unknown"

    slug = slugify(title)
    ep_dir = EPISODES / slug

    # IDEMPOTENCY: refuse to clobber an already-processed episode.
    if ep_dir.exists() and not force:
        raise SystemExit(f"already processed: {ep_dir} (use --force to redo)")

    # Optional: trim dead tail BEFORE processing (free, local). The trimmed file
    # is what gets transcribed/graphed and is the timestamp reference. The
    # ORIGINAL is what moves to traite/ (source of record). Skip trimming when a
    # transcript is already supplied (it was produced from the right cut).
    video = trim_video(original, until) if (until and transcript_done is None) else original

    # FIX: point the timestamp deep-links at the real VIDEO file, not the audio
    # the sidecar happens to name. wire_sources reads sidecar['video'] — rewrite
    # it to the original video so clicking a node opens the .mp4.
    if sidecar is not None:
        import json as _json
        sc = _json.loads(sidecar.read_text(encoding="utf-8"))
        sc["video"] = original.name
        fixed = sidecar.with_name(sidecar.stem + ".fixed.json")
        fixed.write_text(_json.dumps(sc), encoding="utf-8")
        sidecar = fixed

    # 3. Duration = the REAL content length, not the raw file. With a sidecar,
    # take the last timestamp (the trimmed/transcribed content); else ffprobe.
    duration = _content_duration(sidecar, video)

    # 5-6. Preflight + extract via ingest.py (cost-gate: needs --yes).
    # FIX: run in a temp WORK dir OUTSIDE the repo, because episodes/ is listed
    # in the repo's .graphifyignore (so a scan dir inside it is ignored and the
    # transcript is never detected). We copy the result into episodes/<slug>/
    # afterwards.
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix=f"ep_{slug}_"))
    work = tmp
    doc_name = f"{slug}-transcript.txt"
    cmd = [sys.executable, str(INGEST), "--work", str(work), "--doc-name", doc_name]
    if transcript_done is not None:
        # Transcript already produced (e.g. on a rented GPU): skip transcription,
        # feed the text + its timestamp sidecar straight in (no re-clean).
        cmd += ["--transcript", str(transcript_done)]
        if sidecar is not None:
            cmd += ["--sidecar", str(sidecar)]
    else:
        cmd += ["--video", str(video)]
    if token_budget:
        cmd += ["--token-budget", str(token_budget)]
    if proceed:
        cmd.append("--yes")
    # ATOMICITY: everything below must succeed before we move the video.
    print(f"\n=== processing {video.name} -> {slug} ===", flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        # ingest stopped at the gate (no --yes) or failed → leave video in place.
        raise SystemExit("ingest did not complete (cost-gate or error) — "
                         "video left in a-traiter/. Re-run with --yes to proceed.")

    # ingest runs graphify from inside work/scan/, so the output lands there.
    out_dir = work / "scan" / "graphify-out"
    graph_json = out_dir / "graph.json"
    if not graph_json.exists():
        raise SystemExit(f"no graph produced at {graph_json} — video left in place.")

    # Move the produced output into episodes/<slug>/ (out of the ignored temp).
    import shutil as _sh
    ep_dir.mkdir(parents=True, exist_ok=True)
    _sh.copytree(out_dir, ep_dir / "graphify-out", dirs_exist_ok=True)

    # 7-8. Réinjection: fiche + index from the REAL produced content (free).
    topics = god_nodes(graph_json)
    stats = graph_stats(graph_json)
    write_fiche(ep_dir, slug, title, author, duration, video.name, topics, stats)
    update_index(slug, title, duration, stats)

    # 9. LAST: move the ORIGINAL out of the drop zone (only now it's all done).
    # The trimmed temp file (if any) is discarded — graphify-out keeps the copy.
    if until and video != original and video.exists():
        video.unlink()
    dest = TRAITE / original.name
    if original.resolve() != dest.resolve():
        original.rename(dest)
    print(f"\nDone: episodes/{slug}/  (video -> traite/{original.name})", flush=True)
    return ep_dir


def _download(url: str) -> Path:
    """Download a url into a-traiter/ via yt-dlp (cookies/EJS env honored)."""
    A_TRAITER.mkdir(exist_ok=True)
    out = A_TRAITER / "%(title)s.%(ext)s"
    subprocess.run(["yt-dlp", "-o", str(out), url], check=True)
    # newest file in a-traiter/ is the download
    files = sorted(A_TRAITER.glob("*"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("download produced no file")
    return files[-1]


def _self_check() -> int:
    import tempfile

    # Pure-function checks: slug, fiche, index idempotency — no network/ingest.
    assert slugify("Ep 01 - OKF vs RAG.mp4") == "ep-01-okf-vs-rag", slugify("Ep 01 - OKF vs RAG.mp4")
    assert slugify("x.MKV") == "x"

    with tempfile.TemporaryDirectory() as td:
        gj = Path(td) / "graph.json"
        gj.write_text(json.dumps({
            "nodes": [{"id": "a", "label": "OKF", "community": 0},
                      {"id": "b", "label": "RAG", "community": 1},
                      {"id": "c", "label": "Router", "community": 1}],
            "links": [{"source": "a", "target": "b"}, {"source": "a", "target": "c"}],
        }), encoding="utf-8")
        assert god_nodes(gj, 2) == ["OKF", "RAG"] or god_nodes(gj, 2)[0] == "OKF", god_nodes(gj, 2)
        st = graph_stats(gj)
        assert st == {"nodes": 3, "edges": 2, "communities": 2}, st

        ep = Path(td) / "episodes" / "ep01"
        ep.mkdir(parents=True)
        f = write_fiche(ep, "ep01", "OKF vs RAG", "Cloud Codes", "09:40",
                        "ep01.mp4", ["OKF", "RAG"], st)
        body = f.read_text()
        assert "type: episode" in body and 'title: "OKF vs RAG"' in body, body
        assert "- OKF" in body and "stats:" in body

        # INDEX idempotency: two runs of same slug = one line, updated not dupd.
        global EPISODES, INDEX
        EPISODES = Path(td) / "episodes"
        INDEX = EPISODES / "INDEX.md"
        update_index("ep01", "OKF vs RAG", "09:40", st)
        update_index("ep01", "OKF vs RAG v2", "09:40", st)  # re-run
        idx = INDEX.read_text()
        assert idx.count("[ep01](") == 1, "slug line must not duplicate"
        assert "v2" in idx, "re-run should update the line in place"

        # trim_video: make a 4s tone, trim to 2s, check the result is shorter.
        src = Path(td) / "tone.wav"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=duration=4",
                        str(src)], capture_output=True)
        if src.exists():
            trimmed = trim_video(src, "00:00:02")
            assert trimmed.exists() and trimmed != src, trimmed
            assert float(subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(trimmed)], capture_output=True, text=True
            ).stdout) < 3.5, "trimmed clip should be ~2s"
    print("self-check OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Process one episode into the OKF template.")
    ap.add_argument("source", nargs="?", help="video path or url (omit to self-check)")
    ap.add_argument("--title")
    ap.add_argument("--author")
    ap.add_argument("--until", help="trim the video at HH:MM:SS before processing (cuts dead tail)")
    ap.add_argument("--transcript", help="already-produced transcript (e.g. from a GPU run); skips transcription")
    ap.add_argument("--sidecar", help="timestamp .map.json that goes with --transcript")
    ap.add_argument("--token-budget", type=int, default=4000,
                    help="graphify per-chunk token budget (smaller avoids truncated chunks; default 4000)")
    ap.add_argument("--yes", action="store_true", help="proceed past the paid cost-gate")
    ap.add_argument("--force", action="store_true", help="reprocess even if the episode exists")
    args = ap.parse_args()

    if not args.source:
        return _self_check()

    process(args.source, args.title, args.author, proceed=args.yes,
            force=args.force, interactive=sys.stdin.isatty(), until=args.until,
            transcript_done=Path(args.transcript) if args.transcript else None,
            sidecar=Path(args.sidecar) if args.sidecar else None,
            token_budget=args.token_budget)
    return 0


if __name__ == "__main__":
    sys.exit(main())
