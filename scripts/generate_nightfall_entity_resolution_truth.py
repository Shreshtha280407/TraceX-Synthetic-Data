#!/usr/bin/env python3
"""Populate Nightfall's entity_resolution_truth.json from a REAL TraceX
ingestion run -- the Nightfall-specific counterpart to
`generate_entity_resolution_truth.py` (Fulcrum dev/validation).

UPDATED (master plan sections 10/17.2/18.3/22, the P99 bridge-candidate
content-authoring pass): Nightfall now DOES carry a `metadata/
communities.json`/`bridge_entity_id`/`metadata/motif.json` design, scaled
onto its own existing roster -- see `nightfall_bridge_content()` in
`generate_operation_nightfall.py`. The docstring paragraph this replaced
("every entity is a single, distinct identity, no communities.json at
all") described the corpus honestly at the time it was written, but is no
longer accurate; kept only as history in git blame, not restated here.

Reuses Fulcrum's own generic `fetch_resolved_mentions`/`build_pairs`
helpers unchanged (they already take `known_tokens`/`bridge_id`/community
rosters as plain arguments, nothing Fulcrum-specific) -- same `SYN-PER-*`/
`SYN-LOC-*` out-of-scope boundary applies identically here: TraceX's
entity-resolution pipeline never treats a raw person/location identifier
as identity-bearing, so the bridge candidate's own cross-community
ambiguity is demonstrated by directly inspecting the real entity-
resolution candidates for this case (Part D's live-proof step), never by
this truth file's PHONE/ACC/VEH-only `entity_pairs` mechanism -- exactly
the same honest limitation Fulcrum's own script already documents.

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
    OUT_OF_SCOPE_TOKEN_PREFIXES,
    TraceXCapabilityMissing,
    TruthGenerationIncomplete,
    build_pairs,
    create_or_join_case,
    fetch_resolved_mentions,
    register_and_login,
    resolve_entities_via_intelligence_worker,
    upload_evidence,
    wait_for_jobs_to_complete,
)

NIGHTFALL_CASE = "case-operation-nightfall"
MANIFEST_V1_PATH = "manifests/operation-nightfall.v1.json"

#: Deliberate subset -- see module docstring's "Evidence upload scope".
#: `structured/contradictions.json` maps the same way Fulcrum's own script
#: maps it (`structured_json`, the generic JSON-evidence fallback).
SOURCE_TYPE_BY_DIRECTORY = {"documents": "document"}
SOURCE_TYPE_BY_PATH_SUFFIX = {
    "social/messages.json": "chat",
    "structured/cdr.csv": "cdr",
    "structured/transactions.csv": "financial",
    "structured/sightings.json": "structured_json",
    "structured/cdr-metadata.json": "structured_json",
    "structured/contradictions.json": "structured_json",
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
    communities_path = case_dir / "metadata/communities.json"
    communities = json.loads(communities_path.read_text()) if communities_path.is_file() else None
    known_tokens = set()
    if communities is not None:
        all_tokens = {communities["bridge_entity_id"]}
        all_tokens.update(communities["community_a"]["entity_ids"])
        all_tokens.update(communities["community_b"]["entity_ids"])
        known_tokens = {t for t in all_tokens if not any(t.startswith(p) for p in OUT_OF_SCOPE_TOKEN_PREFIXES)}
        out_of_scope = sorted(all_tokens - known_tokens)
        if out_of_scope:
            print(
                f"{len(out_of_scope)} SYN-PER-*/SYN-LOC-* token(s) excluded from the 100%-coverage "
                f"requirement (deliberate, documented boundary -- see OUT_OF_SCOPE_TOKEN_PREFIXES): "
                f"{out_of_scope}"
            )
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
        grouped = fetch_resolved_mentions(args.tracex_api, token, case_id, known_tokens) if known_tokens else {}
    except urllib.error.URLError as error:
        parser.error(f"could not reach TraceX at {args.tracex_api}: {error}")
    except (TraceXCapabilityMissing, TruthGenerationIncomplete) as error:
        parser.error(str(error))

    print(f"TraceX case UUID for {NIGHTFALL_CASE}: {case_id}")
    if communities is not None:
        truth["entity_pairs"] = build_pairs(grouped, communities["bridge_entity_id"],
            communities["community_a"]["entity_ids"], communities["community_b"]["entity_ids"])
    else:
        truth["entity_pairs"] = []
    truth["pending_ingestion"] = False
    truth.pop("blocked_reason", None)
    truth.pop("todo", None)
    new_bytes = (json.dumps(truth, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    truth_path.write_bytes(new_bytes)
    update_manifest_entry_for_truth_file(root, truth_path, new_bytes)
    print(f"wrote {len(truth['entity_pairs'])} entity_resolution_truth pairs for {NIGHTFALL_CASE} "
          f"(case UUID {case_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
