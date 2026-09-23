#!/usr/bin/env python3
"""Populate Nightfall's entity_resolution_truth.json from a REAL TraceX
ingestion run -- the Nightfall-specific counterpart to
`generate_entity_resolution_truth.py` (Fulcrum dev/validation).

Nightfall (ADR-016, the one true holdout) has no `communities.json`/
`bridge_entity_id`/motif design at all -- confirmed by reading its actual
metadata before writing this script: every one of its 30 entities (10 PER,
6 VEH, 6 PHONE, 5 ACC, 3 LOC) is a single, distinct identity, one token
each. There is no pair of tokens anywhere in Nightfall's own design that
secretly refer to the same identity -- the only cross-entity relationship
in the whole corpus is `SYN-VEH-NF-1001`, deliberately shared with Operation
Copper to test cross-case isolation, not intra-case identity resolution
(see `expected-results/case-operation-nightfall.json`'s own
`isolation_rule`). So unlike Fulcrum's `build_pairs`, this script writes
`entity_pairs: []` once ingestion completes -- an honest reflection of the
corpus's own design, not a lowered bar and not a fabricated "different"
assertion either. `--evaluate` will correctly report every metric as
`null` for a truth file with zero pairs (nothing to score a prediction
against) -- also honest, not a bug.

    python scripts/generate_nightfall_entity_resolution_truth.py \
        --root . --tracex-api "$TRACEX_API_URL"

Evidence upload scope (deliberate, documented -- never guessed, never
forced): uploads only documents, CDR/transactions, chat messages, and the
generic `structured_json` fallback already established for Fulcrum's own
non-CDR/finance JSON evidence (sightings/timeline/cdr-metadata) -- the
evidence categories already proven, this session, to reliably complete via
`structured-worker`/`communication-worker`'s active-drain path.

Six files are explicitly skipped, not silently dropped -- see
`SKIPPED_EVIDENCE_SUFFIXES`'s own comment for the live-tested reasoning:
`audio/speakers.rttm`/`audio/transcript.txt`/`audio/turns.json` have no
confirmed `source_type` mapping (no RTTM ingestion path exists anywhere in
TraceX -- confirmed by grep); `audio/discussion.wav`/`visual/footage.mp4`/
`visual/still-*.png` ARE mechanically mappable (`audio`/`video`/`image`
per `SOURCE_TYPE_CONTENT_TYPES`) but were live-tested against this
environment's actually-running workers and confirmed stuck: `media_
detection_v1` jobs (video+image) sit `queued`, never claimed by media-
worker's live polling loop despite its `worker_credentials` row allowing
that processor name (confirmed via direct DB query); the audio job reaches
`deferred`, which active-draining (structured-worker/communication-worker
only) never clears. A genuine local-environment gap, not a mapping guess.

None of this affects the resulting truth file's content -- `entity_pairs`
is `[]` regardless of how much evidence completes, since Nightfall's own
design has no same-identity pairs to assert in the first place (see this
docstring's opening paragraph). It only affects entity/candidate counts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from generate_entity_resolution_truth import (  # noqa: E402
    TraceXCapabilityMissing,
    TruthGenerationIncomplete,
    create_or_join_case,
    register_and_login,
    resolve_entities_via_intelligence_worker,
    upload_evidence,
    wait_for_jobs_to_complete,
)

NIGHTFALL_CASE = "case-operation-nightfall"
MANIFEST_V1_PATH = "manifests/operation-nightfall.v1.json"

#: Deliberate subset -- see module docstring's "Evidence upload scope".
SOURCE_TYPE_BY_DIRECTORY = {"documents": "document"}
SOURCE_TYPE_BY_PATH_SUFFIX = {
    "social/messages.json": "chat",
    "structured/cdr.csv": "cdr",
    "structured/transactions.csv": "financial",
    "structured/sightings.json": "structured_json",
    "structured/cdr-metadata.json": "structured_json",
    "visual/timeline.json": "structured_json",
}
#: Explicitly not uploaded -- see module docstring. `speakers.rttm`/
#: `transcript.txt`/`turns.json`: no confirmed source_type mapping.
#: `discussion.wav`/`footage.mp4`/`still-*.png`: mechanically mappable
#: (`audio`/`video`/`image` per `SOURCE_TYPE_CONTENT_TYPES`), but live-
#: tested against this environment's running workers and confirmed stuck
#: -- `media_detection_v1` jobs (video+image) sit `queued`, never claimed:
#: `media-worker`'s live polling loop only ever claims `media_metadata_v1`
#: jobs despite `media-worker-docker`'s `worker_credentials` row allowing
#: both names (confirmed via direct DB query, not assumed); the audio job
#: reaches `deferred`, not `queued`/`running`, so `wait_for_jobs_to_
#: complete`'s active-drain (which only targets structured-worker/
#: communication-worker) never converges it either. A genuine local-
#: environment gap, not a source_type guess -- forcing these through would
#: either hang indefinitely or require guessing at the real cause.
SKIPPED_EVIDENCE_SUFFIXES = {
    "audio/speakers.rttm", "audio/transcript.txt", "audio/turns.json",
    "audio/discussion.wav", "visual/footage.mp4",
    "visual/still-01.png", "visual/still-02.png", "visual/still-03.png",
}
NON_EVIDENCE_MODALITIES = {"metadata", "expected_result", "entity_resolution_truth"}


def evidence_files_for_case(root, case_dir):
    register = json.loads((case_dir / "metadata/evidence-register.json").read_text())
    skipped = []
    for entry in register["files"]:
        if entry["modality"] in NON_EVIDENCE_MODALITIES:
            continue
        path = root / entry["path"]
        suffix = "/".join(path.parts[-2:])
        if suffix in SKIPPED_EVIDENCE_SUFFIXES:
            skipped.append(entry["path"])
            continue
        source_type = SOURCE_TYPE_BY_DIRECTORY.get(path.parent.name) or SOURCE_TYPE_BY_PATH_SUFFIX.get(suffix)
        if source_type is None:
            raise TruthGenerationIncomplete(
                f"evidence-register.json lists {entry['path']!r} (modality={entry['modality']!r}) "
                "with no known source_type mapping -- refusing to guess one."
            )
        yield path, source_type
    if skipped:
        print(f"skipped {len(skipped)} evidence file(s) with no confirmed source_type mapping: {skipped}")


def update_manifest_entry_for_truth_file(root, truth_path, new_bytes):
    manifest_path = root / MANIFEST_V1_PATH
    manifest = json.loads(manifest_path.read_text())
    relative_path = truth_path.relative_to(root).as_posix()
    matches = [entry for entry in manifest["files"] if entry["path"] == relative_path]
    if len(matches) != 1:
        raise TruthGenerationIncomplete(
            f"expected exactly one manifest entry for {relative_path!r} in {manifest_path}, found {len(matches)}"
        )
    matches[0]["sha256"] = hashlib.sha256(new_bytes).hexdigest()
    matches[0]["bytes"] = len(new_bytes)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".")
    parser.add_argument("--tracex-api", default=os.environ.get("TRACEX_API_URL"))
    args = parser.parse_args()
    if not args.tracex_api:
        parser.error("--tracex-api or $TRACEX_API_URL is required")

    root = Path(args.root).resolve()
    case_dir = root / "operation-nightfall" / NIGHTFALL_CASE
    truth_path = case_dir / "entity_resolution_truth.json"
    truth = json.loads(truth_path.read_text())
    tracex_root = root.parent / "TraceX"

    try:
        token = register_and_login(args.tracex_api)
        case_id = create_or_join_case(args.tracex_api, token, NIGHTFALL_CASE)
        job_ids = []
        for path, source_type in evidence_files_for_case(root, case_dir):
            response = upload_evidence(args.tracex_api, token, case_id, path, source_type=source_type)
            job_ids.append(response["job"]["job_id"])
        print(f"waiting for {len(job_ids)} evidence-processing job(s) to complete...")
        wait_for_jobs_to_complete(args.tracex_api, token, case_id, job_ids, tracex_root=tracex_root)
        print("running entity-resolution (intelligence_worker.py --resolve-entities)...")
        resolve_entities_via_intelligence_worker(case_id, tracex_root=tracex_root)
    except urllib.error.URLError as error:
        parser.error(f"could not reach TraceX at {args.tracex_api}: {error}")
    except (TraceXCapabilityMissing, TruthGenerationIncomplete) as error:
        parser.error(str(error))

    print(f"TraceX case UUID for {NIGHTFALL_CASE}: {case_id}")
    truth["entity_pairs"] = []
    truth["pending_ingestion"] = False
    truth.pop("blocked_reason", None)
    truth.pop("todo", None)
    new_bytes = (json.dumps(truth, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    truth_path.write_bytes(new_bytes)
    update_manifest_entry_for_truth_file(root, truth_path, new_bytes)
    print(f"wrote entity_resolution_truth.json for {NIGHTFALL_CASE} (entity_pairs: [], by design -- see module docstring)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
