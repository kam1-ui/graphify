#!/usr/bin/env python3
"""Pre-flight check for graphify: preview a corpus and recommend a method BEFORE
extracting, so you never accidentally send hundreds of docs to a paid/session LLM.

Usage:
    uv run python tools/preflight.py <folder> [<folder> ...]

Code is extracted locally via tree-sitter (free, no model). Everything else
(docs, PDFs, images, video transcripts) goes through an LLM. This script counts
each category and tells you which method is safe for the mix it finds.
"""
import sys
from pathlib import Path

from graphify.detect import detect

# Categories detect() can return that require an LLM call (everything but code).
# ponytail: derived by "not code" rather than an allowlist, so a new paid file
# type added to detect() is treated as paid by default (the safe direction).
FREE = "code"


def preflight(folder: str) -> int:
    root = Path(folder)
    if not root.exists():
        print(f"  ! {folder}: introuvable")
        return 0
    files = detect(root).get("files", {})
    counts = {k: len(v) for k, v in files.items() if v}
    free = counts.get(FREE, 0)
    paid = sum(n for k, n in counts.items() if k != FREE)

    print(f"\n=== {folder} ===")
    for k, n in sorted(counts.items()):
        tag = "gratuit (AST local)" if k == FREE else "PASSE AU MODELE (cout)"
        print(f"  {k:10} {n:4}  -> {tag}")
    print(f"  total: {free} gratuit, {paid} payant(s)")

    # Recommend a method based on the mix.
    print("  reco:")
    if free == 0 and paid == 0:
        print("    aucun fichier indexable trouve -> passe un DOSSIER, pas un fichier isole.")
    elif paid == 0:
        print("    corpus 100% code -> AUCUNE cle. Gratuit, local, safe.")
        print("    -> graphify extract <dossier>            (ou /graphify dans Claude Code, safe)")
    elif free == 0:
        print("    QUE des docs/medias -> tout passe au modele.")
        print("    -> JAMAIS /graphify dans Claude Code (brule la session).")
        print("    -> graphify extract <dossier> --backend gemini --model gemini-2.5-flash")
    else:
        print(f"    mix: {paid} fichier(s) iront au modele.")
        print("    -> soit .graphifyignore pour exclure les docs (= code gratuit seul),")
        print("    -> soit graphify extract <dossier> --backend gemini  (pas la session Claude).")
    return paid


if __name__ == "__main__":
    targets = sys.argv[1:] or ["."]
    total_paid = sum(preflight(t) for t in targets)
    # Non-zero exit when something would cost, so it's usable as a CI/pre-commit gate.
    sys.exit(1 if total_paid else 0)
