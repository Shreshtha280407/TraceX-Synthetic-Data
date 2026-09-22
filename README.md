# TraceX Synthetic Data

This repository contains small, fully fictional inputs for controlled TraceX
acceptance testing. It is not a production evidence store, an ML-training
dataset, a benchmark, or evidence of real-world performance. Nothing here
identifies a real person or organisation, describes a real operation, proves
guilt, or should be used to make an investigative conclusion.

## Operation Nightfall

Operation Nightfall is a deterministic Phase 8 acceptance/demo corpus. Its
three complete, independent case bundles exercise multimodal ingestion,
provenance, graph construction, case isolation, and review of a misleading link.

```text
DATA-NOTICE.md
manifests/operation-nightfall.v1.json
operation-nightfall/
  case-operation-nightfall/  # 10 persons, 6 vehicles, 6 devices, 5 accounts, 3 locations
  case-operation-copper/     # 8 persons, 5 vehicles, 6 devices, 5 accounts, 3 locations
  case-operation-echo/       # 8 persons, 5 vehicles, 6 devices, 5 accounts, 3 locations
    documents/              # each case has every directory shown here
    structured/
    social/
    audio/
    visual/
    metadata/
expected-results/
scripts/
  generate_operation_nightfall.py
  verify_operation_nightfall.py
```

Generate and verify it from the repository root:

```bash
python scripts/generate_operation_nightfall.py --output-root .
python scripts/verify_operation_nightfall.py --root .
```

The corpus is already included in this repository, so the first generation
command intentionally refuses an overwrite in an existing checkout. To rebuild:

```bash
python scripts/generate_operation_nightfall.py --output-root . --force
python scripts/verify_operation_nightfall.py --root .
```

Generation uses a fixed default seed (`8242026`); `--seed` changes the seed.
It stages the complete corpus within the output root, verifies it, then replaces
only recognized managed files. Modified managed files and symlink paths are
rejected. Prior managed output is recoverable under ignored
`artifacts/nightfall-previous-*`; obsolete files from the original tiny corpus
are retired during upgrade. The separate scale output is unaffected.

Every case contains two real PDFs and matching TXT notes, 120 CDR rows,
90 transaction rows, 60 social messages, 18 sighting annotations, ten timed
events, an 81-second speech recording, a 27-second 1280×720 H.264 video, three
PNG stills, and an evidence register. People, devices, vehicles, accounts, and
locations recur across the files. Event references and time windows are explicit
in `metadata/events.json`; canonical entities are in `metadata/entities.json`.

Offline speech uses three distinct eSpeak NG voices (`en-us+m3`, `en-gb+f3`,
`en-sc+m7`) assigned to `SYN-SPEAKER-ALPHA`, `SYN-SPEAKER-BETA`, and
`SYN-SPEAKER-GAMMA`. Nine separated turns discuss harmless fictional evidence
handling. `audio/turns.json`, `audio/transcript.txt`, and `audio/speakers.rttm`
describe the authored speech and exact sample-based timing. These support local
ASR/diarization workflow review; perfect transcription or diarization is not an
acceptance requirement. Sidecars are authored input annotations, never fabricated
worker results. These three repeated speaker labels are case-local role names,
not shared person identities.

The procedural video presents a stylized sedan, van, and truck in successive
intervals `[0,9)`, `[9,18)`, and `[18,27)` seconds. Large fictional vehicle labels
and `SYNTHETIC TEST FOOTAGE — NOT REAL SURVEILLANCE` are embedded in every scene.
`visual/timeline.json` gives timestamps, entity references, synthetic status,
and normalized `[x, y, width, height]` label-panel bounding boxes (including
padding). The three PNGs are the actual source frames used to encode the video.
Object/face/plate detection accuracy is not required. Verification fully decodes
the video and compares frames near both ends of each annotated interval with
the associated still image.

`SYN-VEH-NF-1001` is the only shared entity identifier between Nightfall and
Copper; it appears across their documents, structured metadata, chat, audio,
and labelled visual scenes. Nightfall access must not reveal Copper records,
entities, observations, graph nodes, or case details by matching this token.
Echo uses similar `SYN-ALIAS-ECHO-01O` / `SYN-ALIAS-ECHO-010` tokens for distinct
persons with simultaneous conflicting locations. Its expected disposition is
`review_required`, with automatic merging and cross-case linking prohibited.

All domain, evidence, speaker, and observation identifiers use `SYN-` prefixes.
The required `case-operation-*` directory keys and composite
`case_id:synthetic_identifier` identity keys are structural scope keys.

Each evidence register covers every file belonging to its case, including the
register and expected-result JSON. Every artifact has its own authored safe
acceptance assertion in `expected-results/` and SHA-256, case ID, content type,
modality, byte size, and synthetic marker in the global manifest. The manifest
is the hash root and cannot contain its own hash; pin its SHA-256 and the Git
commit externally. Verification checks exact coverage, entity/record minimums,
case-local references, isolated sharing, speech/RTTM alignment, PDF readability,
PNG integrity, video timing, and the 100,000,000-byte corpus ceiling. Closed
fictional text templates reject unexpected text or inferred-result fields even
when artifact hashes have been recomputed. This checks authored fixture safety;
it is not a general detector of personal information in arbitrary input.

Only Python's standard library and already installed local `espeak-ng`,
`ffmpeg` (libx264, PNG, drawtext), `ffprobe`, `pdftotext`, `fc-match`, and a local
monospace font are used. No models, packages, media assets, or datasets are
downloaded. The manifest records tool versions and the font hash. Byte-for-byte
regeneration requires the same seed and local tool/font versions; hashes make
changes in those inputs visible. Verification needs Python, FFmpeg/FFprobe,
and `pdftotext`. The sub-100 MB ceiling applies to acceptance media/metadata;
ignored scale output and recoverable local rebuild backups are separate.

### Phase 8 Part 1 use

Clone this repository beside or otherwise outside the main TraceX code
repository. Pin the exact corpus revision in the Phase 8 Part 1 test
configuration using the immutable Git commit hash (for example, the output of
`git rev-parse HEAD`). Do not copy this repository into the main TraceX tree,
and do not refer to a moving branch name when reproducibility matters.

Real benchmark data, model weights, real agency evidence, database dumps,
secrets, caches, and external dataset copies must never be committed here.

## Separate synthetic scale generator

The scale generator is deliberately unrelated to the Operation Nightfall
story. It creates large structured inputs locally under an ignored output
directory for Phase 8 scale and graph testing. It is not training data, a
benchmark, evidence, or a measure of real-world performance.

```bash
python scripts/generate_synthetic_scale.py \
  --profile scale-profiles/synthetic-scale-v1.json \
  --output-root generated/synthetic-scale-v1

python scripts/verify_synthetic_scale.py \
  --root generated/synthetic-scale-v1
```

The default profile creates 25 isolated fictional cases, about 10,000
entities, 50,000 observations, 100,000 CDR rows, 75,000 transaction rows,
25,000 social/chat records, 10,000 vehicle sightings, and 2,000 fictional
intelligence text records. Generated scale files stay outside Git; only the
profile, scripts, and documentation are tracked.

All identifiers use conspicuous `SYN-*` forms. A small declared set of
same-looking vehicle identifiers occurs across case boundaries solely to test
isolation. It is never a confirmed cross-case link.

## Operation Fulcrum (v2 graph-truth development/validation corpus)

Operation Fulcrum is a small, separate, additive corpus (`manifests/operation-nightfall.v2.json`)
giving the evaluation harness graph truth that Nightfall alone doesn't exercise: two
entity clusters, one deliberately ambiguous bridge identity connecting them, a
CALL → TRANSFER → VEHICLE MOVEMENT → MEETING evidentiary motif inside one documented
time window, and a deliberately contradictory clue. It never touches Nightfall's v1
files or hashes.

```text
manifests/operation-nightfall.v2.json
operation-fulcrum/
  case-fulcrum-dev/    # tune against this split
  case-fulcrum-val/    # measure against this split
expected-results/case-fulcrum-dev.json
expected-results/case-fulcrum-val.json
```

Per Phase 7 governance, `case-fulcrum-dev` and `case-fulcrum-val` are for tuning and
measurement; Operation Nightfall itself remains a frozen holdout and is never tuned on.
Fulcrum's audio/visual sidecars are metadata-only (`media_present: false`) — no
rendered WAV/MP4, since the story only needs to be baked into evidence content and
media *metadata*, not synthesized media.

Each Fulcrum case ships `entity_resolution_truth.json` as a documented pending
placeholder (`pending_ingestion: true`, empty `entity_pairs`) — its real UUIDs can
only come from TraceX's own entity layer after a live ingestion, so none are
fabricated here. Once TraceX and this repository are both reachable:

```bash
python scripts/generate_entity_resolution_truth.py \
  --case case-fulcrum-dev --root . --tracex-api "$TRACEX_API_URL"
```

`python scripts/generate_operation_nightfall.py --output-root . --force` builds both
corpora in one pass, and `python scripts/verify_operation_nightfall.py --root .`
checks both v1 and v2.
