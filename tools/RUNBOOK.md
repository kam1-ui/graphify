# Runbook — Ingestion vidéo → graphe de connaissances → vault Obsidian

Procédure standard, suivie de la même façon par un humain ou un agent. Chaque
étape dit **qui paie** (local/gratuit vs Gemini/payant) et **comment vérifier**
avant de continuer. Ne jamais sauter le preflight : c'est le garde-fou contre
une facture surprise.

> Règle absolue : **graphify est immuable.** Toute la valeur ajoutée vit dans
> `tools/` (pré/post-processeurs). On ne patche jamais graphify pour une
> intégration. On n'utilise jamais le skill `/graphify` ni `--backend claude-cli`
> (ça brûle la session Claude Code).

## Architecture code / données (2026-07-18)

Le fork (`~/graphify`) = **code pur**, il ne reçoit jamais de données. Chaque
corpus a son propre repo git (ex. `~/corpus-gex` pour la série GEX) qui contient
les sources (`episodes/<slug>/`) **et** le graphe committé (`graphify-out/` —
artefact de build versionné, doctrine « Team setup » du README graphify +
issue #369 : un seul publisher régénère, tout le reste query).

Conséquences pratiques :
- `WORK` vit **toujours dans le repo corpus** : `WORK=~/corpus-gex/episodes/<slug>`.
  graphify écrit dans le `graphify-out/` du répertoire courant → ne jamais
  lancer une commande `graphify` avec un cwd dans `~/graphify`.
- Les outils s'invoquent par chemin depuis le fork :
  `python ~/graphify/tools/ingest.py --transcript "$WORK/….txt" --work "$WORK"`
  (venv du fork désactivé : il masque l'install globale uv).
- Média lourd (mp4/mp3/m4a, frames) : `~/video-staging/` ou `~/watch-runs/<slug>/`,
  jamais dans un repo. Évidence légère (transcript, `.map.json`, ANALYSIS.md) :
  copiée dans `episodes/<slug>/` et committée avant le run.
- Après chaque (re)génération : `git add graphify-out/ && git commit` dans le
  repo corpus. Une régénération faussée se répare par `git revert`.

## Pré-requis (une fois)

- Clé Gemini valide dans `~/.gemini/.env` (`GEMINI_API_KEY=...`).
  **Vérifier qu'elle marche pour la *vraie* inférence** (pas juste lister les modèles) :
  ```bash
  K=$(grep -oE 'GEMINI_API_KEY=.*' ~/.gemini/.env | head -1 | cut -d= -f2- | tr -d '"')
  curl -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=$K" \
    -H 'Content-Type: application/json' -d '{"contents":[{"parts":[{"text":"Reply OK"}]}]}' | head -c 120
  ```
  Doit renvoyer un `"candidates"` avec "OK". Un `401 ACCOUNT_STATE_INVALID` =
  service account désactivé → régénérer la clé (jamais la coller en clair).
- `yt-dlp` configuré (cookies + EJS) pour les téléchargements YouTube sur VPS.

## Voie rapide — une seule commande (recommandé pour les agents)

`tools/ingest.py` enchaîne toute la procédure. Le **preflight reste un arrêt
dur** : sans `--yes`, ça s'arrête avant tout appel payant à Gemini ; avec
`--yes`, l'agent a explicitement accepté le coût.

```bash
K=$(grep -oE 'GEMINI_API_KEY=.*' ~/.gemini/.env | head -1 | cut -d= -f2- | tr -d '"')

# Depuis un fichier vidéo (.mp4 OBS) : transcrit (timestamps) PUIS tout le reste.
GEMINI_API_KEY="$K" python tools/ingest.py --video lesson.mp4 --work WORK \
    --whisper-model small --threads 2 --yes

# Depuis un transcript déjà produit :
GEMINI_API_KEY="$K" python tools/ingest.py --transcript clean.txt --work WORK --yes
```
Sortie : un vault Obsidian dont chaque node a un lien source + un lien vidéo
horodaté (`[14:13](file://…#t=853)`). Sans `--yes`, lance d'abord la commande
pour voir le preflight, puis relance avec `--yes` si le coût convient.

> Câblage timestamp : `--video` produit un sidecar `<doc>.map.json` aligné sur
> le transcript nettoyé. Ne JAMAIS re-nettoyer ni re-chunker ce doc canonique
> après coup (ça désynchronise les offsets — cf. revue adversariale). ingest.py
> respecte déjà cet invariant (saute clean.py pour les vidéos).

## Étape 0 — Capture OBS (sur ton PC) + transfert au VPS

Pour du contenu **streamé/protégé** auquel tu as accès légalement (ton
abonnement payé, usage perso d'étude) : on **capture l'écran** avec OBS plutôt
que de télécharger un fichier. La capture se fait **sur ton PC** (OBS y est, il
a un écran) — PAS sur le VPS (CPU-only, headless, risque de surchauffe).

**OBS (PC) :**
1. Ouvre l'épisode dans ton navigateur.
2. OBS → source **Display Capture** (ou Window Capture sur l'onglet) + capture
   de l'**audio du bureau/système** (Desktop Audio), pas le micro.
3. Réglages (Settings → Output) : format **MP4**, ~1080p, 30 fps, bitrate
   modéré (~2500 kbps). La transcription ne lit que l'**audio** → inutile de
   viser une qualité vidéo élevée ; ça allège juste le fichier.
4. Start Recording → laisse jouer l'épisode entier → Stop.
5. Renomme : `episode-NN-titre.mp4`. Ce nom devient l'identité dans le vault.

**Transfert PC → VPS** (les .mp4 sont gros → pas par git, qui les ignore) :
```bash
scp episode-03-titre.mp4 dev-work@VPS:~/video-staging/a-traiter/
# reprise possible sur gros fichier :
rsync -P episode-03-titre.mp4 dev-work@VPS:~/video-staging/a-traiter/
```

> ponytail : la capture reste manuelle (1 clic Start/Stop par épisode). On
> n'automatise PAS OBS+Chrome headless sur le VPS — Xvfb+Playwright sur un CPU
> qui a déjà fondu = fragile et lent pour un gain marginal. Si tu veux
> semi-automatiser, fais-le sur le PC (Playwright local lance la lecture, OBS
> enregistre), jamais sur le VPS.

À partir d'ici, le `.mp4` est sur le VPS → utilise la **voie rapide** ci-dessus
(`ingest.py --video`), ou la procédure manuelle détaillée ci-dessous.

## Procédure manuelle (détaillée / dépannage)

Variables : `VIDEO=<chemin .mp4>`, `WORK=<dossier de travail>`.

### 1. Transcrire le .mp4 capturé — *local, gratuit*
```bash
# faster-whisper sur le fichier OBS → texte + sidecar timestamps.
python tools/transcribe_ts.py "$VIDEO" --out "$WORK" --model small --threads 2
# Sortie : WORK/<stem>.txt + WORK/<stem>.map.json
# (pour une URL téléchargeable, yt-dlp+cookies/EJS via graphify/transcribe.py)
```
**Vérifier :** `raw_transcript.txt` existe et contient du texte.

### 2. Nettoyer le transcript — *local, gratuit (LE gain coût)*
```bash
uv run python tools/clean.py "$WORK/raw_transcript.txt" --stats -o "$WORK/clean_transcript.txt"
```
**Vérifier :** `--stats` affiche une réduction (~30–40% sur du Whisper brut).
Source déjà propre → 0%, c'est normal.

### 3. Preflight — *local, gratuit (GARDE-FOU)*
```bash
uv run python tools/preflight.py "$WORK"
```
**Vérifier :** lis l'estimation. Si « QUE des docs/medias », l'extraction passera
au modèle (payant). Code = gratuit. Ne lance le 4 que si le coût te convient.

### 4. Chunker — *local, gratuit*
```bash
# Chonkie RecursiveChunker, chunk_size en CARACTÈRES (~4000 → chunks cohérents).
# Sortie : WORK/chunked/part01.txt ...
```
**Vérifier :** quelques fichiers `partNN.txt`, pas 1 énorme ni 50 minuscules.

### 5. Extraire le graphe — *Gemini Flash : SEULE étape payante*
```bash
K=$(grep -oE 'GEMINI_API_KEY=.*' ~/.gemini/.env | head -1 | cut -d= -f2- | tr -d '"')
( cd "$WORK/chunked" && GEMINI_API_KEY="$K" \
    graphify extract . --backend gemini --model gemini-2.5-flash )
```
**Vérifier :** « wrote graph.json: N nodes, M edges ». Note le coût affiché.
⚠️ graphify opère sur le `graphify-out/` du **répertoire courant** — toujours
`cd` dans le dossier des chunks avant de lancer.

### 6. Clusteriser + nommer — *local + petit appel Gemini*
```bash
( cd "$WORK/chunked" && GEMINI_API_KEY="$K" graphify cluster-only . )
```
**Vérifier :** `GRAPH_REPORT.md` généré. Si le nommage LLM échoue
(`Expecting value`), on garde « Community N » — non bloquant.

### 7. Exporter le vault Obsidian — *local, gratuit*
```bash
( cd "$WORK/chunked" && graphify export obsidian . )
```
**Vérifier :** `graphify-out/obsidian/` contient une note `.md` par nœud.

### 8. Câbler les sources — *local, gratuit (le "territory")*
```bash
uv run python tools/wire_sources.py "$WORK/chunked/graphify-out/obsidian" \
    --src-dir "$WORK" \
    --map "part:clean_transcript.txt"
```
**Vérifier :** « wired N note(s) ». Chaque note a une section `## Source` avec
un lien cliquable `[[sources/...]]`. Re-lancer = 0 (idempotent).

### 9. Ouvrir dans Obsidian
Pointer Obsidian sur `…/graphify-out/obsidian/` (Open folder as vault). Cliquer
un nœud → ses connexions → son document source. C'est le « map + territory ».

## En cas de batch (plusieurs vidéos / 100h+)
Boucler les étapes 1→8 par vidéo, un `WORK` par vidéo. Le preflight (3) cumule
le coût. Fusionner les graphes ensuite avec `graphify merge-graphs` si on veut
un seul vault. Garder code et docs en passes séparées (code = gratuit) pour ne
payer Gemini que sur ce qui l'exige.

## Limites connues / features à venir (branches séparées)
- Timestamps : graphify jette les timestamps Whisper (`transcribe.py:178`).
  Pour pointer « concept = 14:13 » → transcrire en direct avec faster-whisper
  (sidecar) avant le nettoyage. Brique `transcribe_ts.py` à faire.
- Langfuse (suivi coût batch), LiteLLM (routing modèles), Marker/Crawl4AI
  (sources PDF/web) : Niveau 1, à brancher quand le besoin arrive.
