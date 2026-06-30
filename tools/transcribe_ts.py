#!/usr/bin/env python3
"""Transcribe a recorded video to clean text PLUS a timestamp sidecar.

graphify's own transcribe keeps only segment.text and throws the timestamps
away (transcribe.py:178). We need them: a node in the graph should be able to
say "this concept is explained around 14:13" so you can jump back to the video
and SEE what's on screen when the narration only says "look here". The
timestamps are the bridge to the visual information the transcript can't carry.

This produces two files, by design:
  <stem>.txt        clean text, NO inline timestamps  → this is what goes to
                    Gemini (timestamps inline would waste tokens and pollute
                    extraction).
  <stem>.map.json   sidecar: char-offset → video timestamp, + the video path,
                    so wire_sources can stamp each node with a clickable
                    deep-link into the video at the right moment.

Runs anywhere the pipeline runs (PC or VPS) — same code. On a CPU VPS this is
SLOW (~0.5–1x realtime for medium), so it caps threads and runs int8 to keep
the box from melting; for a 125h backlog prefer a batch/overnight run.

ponytail: faster-whisper on CPU with int8 + capped threads. The map granularity
is per-segment (Whisper's natural unit, a phrase or two) — good enough to seek
a video. Upgrade path: word_timestamps=True for word-level seeking, at more
compute.

Usage:
    python tools/transcribe_ts.py video.mp4 --out WORK
    python tools/transcribe_ts.py video.mp4 --out WORK --model medium --threads 2
    python tools/transcribe_ts.py        # self-check (no model/audio)
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Reuse our deterministic cleaner so the .txt matches the rest of the pipeline.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from clean import clean  # noqa: E402


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def build_text_and_map(segments, video_path: str) -> tuple[str, dict]:
    """From Whisper segments build (clean_text, sidecar_map).

    The map records, for each segment, the char offset where its cleaned text
    starts in the final document and the segment's start time. wire_sources
    finds a node's position in the text, then the latest segment whose offset
    is <= that position gives the timestamp.
    """
    parts: list[str] = []
    entries: list[dict] = []
    offset = 0
    for seg in segments:
        line = clean(seg.text.strip())
        if not line:
            continue
        entries.append({
            "offset": offset,
            "start": round(seg.start, 2),
            "hms": _hms(seg.start),
        })
        parts.append(line)
        offset += len(line) + 1  # +1 for the newline joiner

    text = "\n".join(parts)
    sidecar = {
        "video": video_path,
        "segments": entries,
    }
    return text, sidecar


def transcribe(video: Path, out_dir: Path, model_name: str, threads: int, prompt: str) -> tuple[Path, Path]:
    from faster_whisper import WhisperModel

    out_dir.mkdir(parents=True, exist_ok=True)
    # Cap CPU so a long batch doesn't melt the VPS (the incident we hit before).
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))

    print(f"  transcribing {video.name} (model={model_name}, threads={threads}) ...", flush=True)
    wm = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
    segments, info = wm.transcribe(str(video), beam_size=5, initial_prompt=prompt or None)

    text, sidecar = build_text_and_map(segments, str(video.resolve()))
    sidecar["language"] = getattr(info, "language", "unknown")

    txt_path = out_dir / (video.stem + ".txt")
    map_path = out_dir / (video.stem + ".map.json")
    txt_path.write_text(text, encoding="utf-8")
    map_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    print(f"  -> {txt_path.name} ({len(sidecar['segments'])} segments) + {map_path.name}", flush=True)
    return txt_path, map_path


def _self_check() -> int:
    # Fake Whisper segments — verify text/map building without a model or audio.
    class Seg:
        def __init__(self, text, start):
            self.text = text
            self.start = start

    segs = [
        Seg("[Music]", 0.0),              # cleans to empty -> skipped, no offset burned
        Seg("Welcome, uh, to the lesson.", 5.0),  # filler "uh" stripped by clean()
        Seg("Look here at the diagram.", 12.5),
    ]
    text, sidecar = build_text_and_map(segs, "/videos/lesson.mp4")

    lines = text.split("\n")
    assert lines == ["Welcome, to the lesson.", "Look here at the diagram."], text
    assert sidecar["video"] == "/videos/lesson.mp4"
    # two kept segments
    assert len(sidecar["segments"]) == 2, sidecar
    # first kept segment starts at offset 0, time 5s
    assert sidecar["segments"][0]["offset"] == 0
    assert sidecar["segments"][0]["start"] == 5.0
    assert sidecar["segments"][0]["hms"] == "00:00:05"
    # second starts right after first line + newline
    assert sidecar["segments"][1]["offset"] == len("Welcome, to the lesson.") + 1, sidecar
    assert sidecar["segments"][1]["hms"] == "00:00:12"
    # offset of a node found at char 25 (inside line 2) maps back to the 2nd seg
    pos = 25
    chosen = max((s for s in sidecar["segments"] if s["offset"] <= pos), key=lambda s: s["offset"])
    assert chosen["start"] == 12.5, chosen
    print("self-check OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe a video to clean text + timestamp sidecar.")
    ap.add_argument("video", nargs="?", help="video/audio file (omit to self-check)")
    ap.add_argument("--out", help="output dir (default: alongside the video)")
    ap.add_argument("--model", default=os.environ.get("GRAPHIFY_WHISPER_MODEL", "small"),
                    help="faster-whisper model (tiny/base/small/medium/large-v3)")
    ap.add_argument("--threads", type=int, default=2, help="CPU threads (keep low on a shared VPS)")
    ap.add_argument("--prompt", default=os.environ.get("GRAPHIFY_WHISPER_PROMPT", ""),
                    help="domain hint to fix technical vocabulary")
    args = ap.parse_args()

    if not args.video:
        return _self_check()

    video = Path(args.video)
    if not video.exists():
        ap.error(f"video not found: {video}")
    out_dir = Path(args.out) if args.out else video.parent
    transcribe(video, out_dir, args.model, args.threads, args.prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
