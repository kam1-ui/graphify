#!/usr/bin/env python3
"""Max-quality GPU transcription on a rented box (RunPod RTX 4090).

Same output contract as tools/transcribe_ts.py (clean text + a timestamp
sidecar) so the rest of the pipeline is unchanged — but configured for MAXIMUM
quality on an NVIDIA GPU, because technical trading jargon (GEX, gamma, NQ,
scalping) must come out right. Confirmed via the faster-whisper docs and a
gemini-2.5-pro review:
    model=large-v3, device=cuda, compute_type=float16, beam_size=5,
    SEQUENTIAL (not batched, for max timestamp fidelity), vad_filter,
    word_timestamps, language=en, + a domain initial_prompt.

This runs on the rented GPU. Self-contained: only needs faster-whisper (the
same dep we already use on CPU — zero NEW dependency). Copy this file + the
audio to the box, run it, copy back the .txt + .map.json.

Speed/cost (gemini estimate, RTX 4090): a 2h file ~8-12 min; the ~100h backlog
~7-10 GPU hours ≈ ~$4 total.

ponytail: the secret sauce is the initial_prompt seeding the jargon — extend
DOMAIN_PROMPT per series. Sequential over batched is a deliberate quality call.

Usage (on the GPU box):
    python transcribe_gpu.py ep14-audio.mp3 --out .
    python transcribe_gpu.py audio.mp3 --prompt "extra, terms, here"
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Domain vocab seeded into Whisper to fix technical-term spelling. The trailing
# period gives the model a clean sentence boundary to start from.
DOMAIN_PROMPT = "GEX, gamma exposure, NQ, VIX, SPX, scalping, options flow, dealer positioning."


def _clean_line(text: str) -> str:
    """Light cleanup mirroring tools/clean.py so the .txt matches the pipeline."""
    text = re.sub(r"[\[(][^\])]*[\])]|♪[^♪]*♪", " ", text)          # [Music], (laughs)
    text = re.sub(r"\b(?:uh+|um+|erm+|hmm+|uh-huh)\b[,.]?\s*", "", text, flags=re.I)
    text = re.sub(r"\b(\w+)(?:\s+\1\b){2,}", r"\1", text, flags=re.I)  # word stutter
    return re.sub(r"\s+", " ", text).strip()


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def transcribe(audio: Path, out_dir: Path, prompt: str) -> tuple[Path, Path]:
    from faster_whisper import WhisperModel

    print(f"  loading large-v3 (cuda, float16) ...", flush=True)
    model = WhisperModel("large-v3", device="cuda", compute_type="float16")

    print(f"  transcribing {audio.name} (max quality, sequential) ...", flush=True)
    segments, info = model.transcribe(
        str(audio),
        language="en",
        beam_size=5,
        vad_filter=True,                 # drop silence (e.g. dead tail) for free
        word_timestamps=True,
        initial_prompt=prompt or DOMAIN_PROMPT,
    )

    # Build clean text + sidecar (same shape as transcribe_ts.py).
    parts, entries, offset = [], [], 0
    for seg in segments:
        line = _clean_line(seg.text.strip())
        if not line:
            continue
        entries.append({"offset": offset, "start": round(seg.start, 2), "hms": _hms(seg.start)})
        parts.append(line)
        offset += len(line) + 1
        # progress ping every ~5 min of audio
        if len(entries) % 50 == 0:
            print(f"    .. {_hms(seg.start)}", flush=True)

    text = "\n".join(parts)
    sidecar = {"video": audio.name, "language": getattr(info, "language", "en"),
               "model": "large-v3", "segments": entries}

    out_dir.mkdir(parents=True, exist_ok=True)
    txt = out_dir / (audio.stem + ".txt")
    mp = out_dir / (audio.stem + ".map.json")
    txt.write_text(text, encoding="utf-8")
    mp.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    print(f"\n  done: {txt.name} ({len(entries)} segments) + {mp.name}", flush=True)
    return txt, mp


def main() -> int:
    ap = argparse.ArgumentParser(description="Max-quality GPU transcription (faster-whisper large-v3).")
    ap.add_argument("audio", help="audio/video file")
    ap.add_argument("--out", default=".", help="output dir")
    ap.add_argument("--prompt", default="", help="extra domain vocab (defaults to trading jargon)")
    args = ap.parse_args()

    audio = Path(args.audio)
    if not audio.exists():
        ap.error(f"not found: {audio}")
    transcribe(audio, Path(args.out), args.prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
