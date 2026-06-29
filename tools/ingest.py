#!/usr/bin/env python3
"""One entry point that runs the video→graph→Obsidian RUNBOOK end to end.

Turns the 9-step runbook into a single command a human or an agent can launch,
instead of hand-chaining graphify + the tools/ scripts. It wires together, in
order: clean.py → preflight (HARD STOP) → graphify extract → cluster-only →
export obsidian → wire_sources.py.

The preflight is a deliberate stop: this script does NOT send anything to the
paid Gemini backend until cost is acknowledged. Interactively it asks; for
agents/CI pass --yes to proceed past the gate non-interactively (so the agent
that runs this has explicitly opted into the cost).

graphify is immutable — this only orchestrates the public CLI plus our tools/.
It does NOT download or transcribe (steps 1 of the runbook): point --transcript
at an already-produced transcript. Wiring transcription in is a separate
feature (it needs timestamps, see RUNBOOK "Known limits").

ponytail: shells out to the `graphify` CLI rather than importing internals, so
it stays decoupled from graphify's package layout (the immutable rule). The
cost ceiling is the single Gemini extract call; everything else is local/free.

Usage:
    python tools/ingest.py --transcript raw.txt --work WORK [--yes]
    python tools/ingest.py --transcript raw.txt --work WORK --doc-name video.txt
    python tools/ingest.py        # self-check (no graphify/network)
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Local tools live next to this file.
HERE = Path(__file__).resolve().parent
CLEAN = HERE / "clean.py"
PREFLIGHT = HERE / "preflight.py"
WIRE = HERE / "wire_sources.py"


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a command, streaming output; raise on failure with a clear message."""
    print(f"\n$ {' '.join(cmd)}", flush=True)
    cp = subprocess.run(cmd, cwd=cwd)
    if cp.returncode != 0:
        raise SystemExit(f"step failed (exit {cp.returncode}): {' '.join(cmd)}")
    return cp


def _chunk(clean_path: Path, chunk_dir: Path, chunk_size: int) -> int:
    """Chunk with Chonkie if available; else fall back to one chunk = whole file.

    ponytail: char-based RecursiveChunker (the gotcha we hit: chunk_size is in
    CHARACTERS, not tokens). Fallback keeps the pipeline runnable without
    Chonkie installed — the whole transcript becomes a single chunk.
    """
    chunk_dir.mkdir(parents=True, exist_ok=True)
    text = clean_path.read_text(encoding="utf-8")
    try:
        from chonkie import RecursiveChunker  # type: ignore

        chunks = [c.text for c in RecursiveChunker(chunk_size=chunk_size).chunk(text)]
    except ImportError:
        print("  (chonkie not installed — using one chunk)", flush=True)
        chunks = [text]
    for i, c in enumerate(chunks, 1):
        (chunk_dir / f"part{i:02d}.txt").write_text(c, encoding="utf-8")
    return len(chunks)


def ingest(
    transcript: Path,
    work: Path,
    doc_name: str,
    backend: str,
    model: str,
    chunk_size: int,
    proceed: bool,
) -> Path:
    work.mkdir(parents=True, exist_ok=True)

    # The canonical source doc nodes will link back to (runbook step 8 / the
    # CHEAT CODE "many chunks -> one doc" model). Keep a copy in WORK so
    # wire_sources can find it by name.
    src_copy = work / doc_name
    if transcript.resolve() != src_copy.resolve():
        shutil.copyfile(transcript, src_copy)

    # Step 2 — clean (local, free).
    clean_path = work / "clean_transcript.txt"
    _run([sys.executable, str(CLEAN), str(src_copy), "--stats", "-o", str(clean_path)])

    # Step 3 — preflight (local, free) — the HARD STOP before any paid call.
    chunk_dir = work / "chunked"
    n = _chunk(clean_path, chunk_dir, chunk_size)  # step 4, local
    print(f"  chunked into {n} part(s)", flush=True)
    print("\n--- PREFLIGHT (cost gate) ---")
    subprocess.run([sys.executable, str(PREFLIGHT), str(chunk_dir)])  # informational
    if not proceed:
        print(
            "\nStopping before the paid Gemini extract. Re-run with --yes to proceed.",
            flush=True,
        )
        raise SystemExit(0)

    # Step 5 — extract (PAID: the one Gemini call). graphify uses cwd's
    # graphify-out/, so run from inside chunk_dir.
    _run(["graphify", "extract", ".", "--backend", backend, "--model", model], cwd=str(chunk_dir))

    # Step 6 — cluster + report (local + small Gemini naming call).
    _run(["graphify", "cluster-only", "."], cwd=str(chunk_dir))

    # Step 7 — export Obsidian vault (local, free).
    _run(["graphify", "export", "obsidian", "."], cwd=str(chunk_dir))

    # Step 8 — wire every node to the one source doc (local, free). All chunks
    # (part01, part02, ...) collapse onto the canonical transcript copy.
    vault = chunk_dir / "graphify-out" / "obsidian"
    _run([
        sys.executable, str(WIRE), str(vault),
        "--src-dir", str(work),
        "--map", f"part:{doc_name}",
    ])
    return vault


def _self_check() -> int:
    # Verify orchestration plumbing without graphify or network: clean + chunk
    # + preflight wiring, stopping at the gate.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work"
        transcript = Path(td) / "raw.txt"
        transcript.write_text(
            "[Music]\nSo uh this is a test test test transcript, you know.\n",
            encoding="utf-8",
        )
        try:
            ingest(
                transcript=transcript,
                work=work,
                doc_name="raw.txt",
                backend="gemini",
                model="gemini-2.5-flash",
                chunk_size=4000,
                proceed=False,  # must stop at the gate, never reach graphify
            )
        except SystemExit as e:
            assert e.code == 0, f"should stop cleanly at gate, got {e.code}"
        else:
            raise AssertionError("should have stopped at the preflight gate")
        # clean + chunk happened before the gate
        assert (work / "clean_transcript.txt").exists(), "clean step did not run"
        cleaned = (work / "clean_transcript.txt").read_text()
        assert "[Music]" not in cleaned and "test test" not in cleaned, cleaned
        assert list((work / "chunked").glob("part*.txt")), "chunking did not run"
    print("self-check OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the video→graph→Obsidian pipeline end to end.")
    ap.add_argument("--transcript", help="path to a raw transcript (omit to self-check)")
    ap.add_argument("--work", help="working dir for this ingestion")
    ap.add_argument("--doc-name", help="canonical source-doc filename in the vault (default: transcript's name)")
    ap.add_argument("--backend", default="gemini")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--chunk-size", type=int, default=4000, help="Chonkie chunk size in CHARACTERS")
    ap.add_argument("--yes", action="store_true", help="proceed past the cost gate (paid Gemini call)")
    args = ap.parse_args()

    if not args.transcript:
        return _self_check()
    if not args.work:
        ap.error("--work is required")

    transcript = Path(args.transcript)
    if not transcript.exists():
        ap.error(f"transcript not found: {transcript}")
    doc_name = args.doc_name or transcript.name

    vault = ingest(
        transcript=transcript,
        work=Path(args.work),
        doc_name=doc_name,
        backend=args.backend,
        model=args.model,
        chunk_size=args.chunk_size,
        proceed=args.yes,
    )
    print(f"\nDone. Open as an Obsidian vault: {vault}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
