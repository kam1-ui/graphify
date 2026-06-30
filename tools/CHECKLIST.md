# Checklist — d'un épisode vidéo au vault Obsidian câblé

Une page, usage quotidien. Détails/dépannage → [RUNBOOK.md](RUNBOOK.md).
Chaque épisode est traité **séparément** (pas de batch 100h en bloc).

## A. CAPTURE — sur ton PC (OBS)
- [ ] Ouvre l'épisode (ton abonnement, usage perso d'étude).
- [ ] OBS : source **Display/Window Capture** + **audio du système** (pas le micro).
- [ ] Réglages OBS conseillés : MP4, 1080p, 30 fps, ~2500 kbps (l'audio suffit pour la transcription ; pas besoin de haute qualité vidéo).
- [ ] **Démarre l'enregistrement**, regarde/laisse jouer l'épisode en entier, **stoppe**.
- [ ] Renomme clairement : `episode-03-titre.mp4` (le nom devient l'identité dans le vault).

## B. TRANSFERT — PC → VPS
- [ ] `scp episode-03-titre.mp4 dev-work@VPS:~/graphify/video_input/`
  *(rsync si reprise : `rsync -P episode-03-titre.mp4 dev-work@VPS:~/graphify/video_input/`)*
- [ ] Les gros .mp4 ne vont **pas** dans git (déjà gitignoré). On transfère, on ne commit pas.

## C. TRAITEMENT — sur le VPS (une commande)
```bash
cd ~/graphify
K=$(grep -oE 'GEMINI_API_KEY=.*' ~/.gemini/.env | head -1 | cut -d= -f2- | tr -d '"')

# 1) D'ABORD SANS --yes : voir le coût (preflight), rien n'est payé encore.
GEMINI_API_KEY="$K" python tools/ingest.py \
    --video video_input/episode-03-titre.mp4 --work work/ep03 \
    --whisper-model small --threads 2

# 2) Coût OK ? relance AVEC --yes pour produire le graphe + vault câblé.
GEMINI_API_KEY="$K" python tools/ingest.py \
    --video video_input/episode-03-titre.mp4 --work work/ep03 \
    --whisper-model small --threads 2 --yes
```
- [ ] Le preflight affiche le coût → tu décides.
- [ ] `--yes` lance : transcription (timestamps) → clean → graphe Gemini → export Obsidian → câblage sources.

## D. RÉSULTAT — le vault câblé (objectif vidéo CHEAT CODE 12:53)
- [ ] Vault produit : `work/ep03/scan/graphify-out/obsidian/`
- [ ] Chaque note de concept a : `[[sources/episode-03-titre]]` **+** `[14:13](file://...#t=853)` cliquable.
- [ ] Ouvre le dossier comme vault dans Obsidian (Manage vault → Open folder as vault).
- [ ] Vérifie : clique un concept → tu vois la source + tu peux sauter à la vidéo au bon moment.

## Autres formats (le stack traite tout)
- **PDF / doc** : `python tools/ingest.py --transcript fichier.md --work work/X` (pas de transcription, va direct au graphe).
- **Plusieurs épisodes** : répète A→D, un `work/epNN` par épisode. Graphes **séparés** par épisode (focalisés, rapides).

## Règles de sécurité (ne pas sauter)
- Jamais `--yes` au premier run d'un nouvel épisode : regarde le coût d'abord.
- Jamais le skill `/graphify` ni `--backend claude-cli` (brûle la session Claude Code).
- Backend = `gemini-2.5-flash` (par défaut). Vérifie la clé si erreur 401 (voir RUNBOOK).
