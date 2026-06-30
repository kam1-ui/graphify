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


def _stage_source(clean_path: Path, doc_name: str, scan_dir: Path) -> None:
    """Place the whole cleaned doc where graphify will scan it.

    We do NOT pre-chunk: graphify chunks natively by TOKEN budget
    (_pack_chunks_by_tokens, llm.py), which is both more accurate than a
    char-based pre-split and what actually drives cost. Pre-chunking with
    Chonkie was redundant — graphify repacks everything by tokens anyway and a
    char-split can cut mid-idea. Granularity is tuned with graphify's own
    --token-budget, not by us. (Confirmed via the graphify graph: no Chonkie
    inside graphify; _pack_chunks_by_tokens groups files by token budget.)
    """
    scan_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(clean_path, scan_dir / doc_name)


def ingest(
    work: Path,
    doc_name: str,
    backend: str,
    model: str,
    token_budget: int,
    proceed: bool,
    transcript: Path | None = None,
    video: Path | None = None,
    whisper_model: str = "small",
    threads: int = 2,
    prompt: str = "",
) -> Path:
    work.mkdir(parents=True, exist_ok=True)

    # The canonical source doc nodes link back to (runbook step 8 / the CHEAT
    # CODE "many chunks -> one doc" model). It is the CLEANED transcript, and —
    # critically — its char offsets must match the timestamp sidecar exactly, so
    # nothing may rewrite it after it's produced (adversarial review: cleaning
    # twice desyncs the map; chunking with a per-chunk doc desyncs it too — which
    # is why every node collapses onto this one whole doc, never onto a chunk).
    src_copy = work / doc_name

    if video is not None:
        # Step 1+2 — transcribe the video: produces the cleaned transcript AND an
        # aligned <stem>.map.json sidecar in WORK. clean() is applied per-segment
        # inside transcribe_ts, so we DO NOT run clean.py again (Option A): the
        # text/sidecar are an atomic, already-clean pair.
        import transcribe_ts  # noqa: E402  (tools/ is on sys.path)

        txt_path, _map_path = transcribe_ts.transcribe(
            video, work, whisper_model, threads, prompt
        )
        # Name the canonical doc + its sidecar after doc_name so wire_sources
        # finds both by the same stem.
        if txt_path.resolve() != src_copy.resolve():
            shutil.copyfile(txt_path, src_copy)
            shutil.copyfile(_map_path, work / (Path(doc_name).stem + ".map.json"))
        clean_path = src_copy  # already clean; no second pass
    else:
        # Text source: copy it in, then clean (local, free).
        assert transcript is not None
        if transcript.resolve() != src_copy.resolve():
            shutil.copyfile(transcript, src_copy)
        clean_path = work / "clean_transcript.txt"
        _run([sys.executable, str(CLEAN), str(src_copy), "--stats", "-o", str(clean_path)])
        # Keep the canonical doc identical to what's chunked/searched, so
        # wire_sources finds node labels at consistent positions.
        shutil.copyfile(clean_path, src_copy)

    # Step 3 — stage the whole doc, then preflight (the HARD STOP before paid).
    scan_dir = work / "scan"
    _stage_source(clean_path, doc_name, scan_dir)
    print("\n--- PREFLIGHT (cost gate) ---")
    subprocess.run([sys.executable, str(PREFLIGHT), str(scan_dir)])  # informational
    if not proceed:
        print(
            "\nStopping before the paid Gemini extract. Re-run with --yes to proceed.",
            flush=True,
        )
        raise SystemExit(0)

    # Step 4 — extract (PAID: the one Gemini call). graphify chunks by tokens
    # itself; --token-budget tunes granularity. It uses cwd's graphify-out/, so
    # run from inside scan_dir.
    extract_cmd = ["graphify", "extract", ".", "--backend", backend, "--model", model]
    if token_budget:
        extract_cmd += ["--token-budget", str(token_budget)]
    _run(extract_cmd, cwd=str(scan_dir))

    # Step 5 — cluster + report (local + small Gemini naming call).
    _run(["graphify", "cluster-only", "."], cwd=str(scan_dir))

    # Step 6 — export Obsidian vault (local, free).
    _run(["graphify", "export", "obsidian", "."], cwd=str(scan_dir))

    # Step 7 — wire every node to the one source doc (local, free).
    vault = scan_dir / "graphify-out" / "obsidian"
    _run([
        sys.executable, str(WIRE), str(vault),
        "--src-dir", str(work),
        "--map", f"{Path(doc_name).stem}:{doc_name}",
    ])
    return vault


def _self_check() -> int:
    # Verify orchestration plumbing without graphify or network: clean + stage
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
                token_budget=0,
                proceed=False,  # must stop at the gate, never reach graphify
            )
        except SystemExit as e:
            assert e.code == 0, f"should stop cleanly at gate, got {e.code}"
        else:
            raise AssertionError("should have stopped at the preflight gate")
        # clean + stage happened before the gate
        assert (work / "clean_transcript.txt").exists(), "clean step did not run"
        cleaned = (work / "clean_transcript.txt").read_text()
        assert "[Music]" not in cleaned and "test test" not in cleaned, cleaned
        # the WHOLE doc is staged for graphify (no pre-chunking)
        assert (work / "scan" / "raw.txt").exists(), "source not staged for scan"
        assert (work / "scan" / "raw.txt").read_text() == cleaned, "staged != cleaned"
        # invariant: the canonical doc nodes link to == the cleaned text that
        # gets searched, so wire_sources finds labels at stable offsets.
        assert (work / "raw.txt").read_text() == cleaned, "canonical doc must equal cleaned text"
    print("self-check OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the video→graph→Obsidian pipeline end to end.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--video", help="video/audio file: transcribe (w/ timestamps) then ingest")
    src.add_argument("--transcript", help="an already-produced transcript to ingest")
    ap.add_argument("--work", help="working dir for this ingestion")
    ap.add_argument("--doc-name", help="canonical source-doc filename in the vault (default: source's name)")
    ap.add_argument("--backend", default="gemini")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--token-budget", type=int, default=0,
                    help="graphify per-chunk token budget (0 = graphify default ~60k)")
    ap.add_argument("--whisper-model", default="small", help="faster-whisper model when --video is used")
    ap.add_argument("--threads", type=int, default=2, help="CPU threads for transcription")
    ap.add_argument("--prompt", default="", help="Whisper domain hint to fix technical vocab")
    ap.add_argument("--yes", action="store_true", help="proceed past the cost gate (paid Gemini call)")
    args = ap.parse_args()

    if not args.video and not args.transcript:
        return _self_check()
    if not args.work:
        ap.error("--work is required")

    source = Path(args.video or args.transcript)
    if not source.exists():
        ap.error(f"source not found: {source}")
    # For a video, the canonical doc is the .txt transcript, not the .mp4.
    doc_name = args.doc_name or (source.stem + ".txt" if args.video else source.name)

    vault = ingest(
        work=Path(args.work),
        doc_name=doc_name,
        backend=args.backend,
        model=args.model,
        token_budget=args.token_budget,
        proceed=args.yes,
        transcript=None if args.video else source,
        video=source if args.video else None,
        whisper_model=args.whisper_model,
        threads=args.threads,
        prompt=args.prompt,
    )
    print(f"\nDone. Open as an Obsidian vault: {vault}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
