#!/usr/bin/env python3
"""Populate entity_resolution_truth.json from a REAL TraceX ingestion run.

Not part of the offline, deterministic corpus generator: this talks to a live
TraceX instance, so it is never invoked by generate_operation_nightfall.py or
by CI.

STATUS (checked against a live tracex-api instance): auth (register/login),
evidence upload, case creation, and entity-resolution readback are all real,
working endpoints, confirmed against a live instance's `GET /openapi.json`.
`CASE_CREATE_PATH` and `ENTITY_MENTIONS_PATH` (below) closed the last two
gaps -- `POST /api/v1/cases` and `GET /api/v1/cases/{case_id}/entities`, the
latter added specifically to close this script's own blocker (see TraceX's
`docs/qa/known-limitations.md`, WP-2 section, "Resolved (Gap-Closure
follow-up)").

    python scripts/generate_entity_resolution_truth.py \
        --case case-fulcrum-dev --root . --tracex-api "$TRACEX_API_URL"

What it does:
  1. Registers/logs in a probe account and creates a case.
  2. Uploads every file in the case bundle (operation-fulcrum/<case>/) as
     evidence.
  3. Waits (bounded) for every uploaded file's worker job to leave
     queued/running, then shells out to TraceX's `intelligence_worker.py
     --resolve-entities --case-id <uuid>` -- see the "Cross-repo dependency"
     note below -- since entity/candidate generation is a separate, manual,
     CLI-only step (deliberately not wired automatically; see TraceX's
     `docs/qa/known-limitations.md`, WP-2 section) with no HTTP-reachable
     form, matching `--evaluate`'s own sanctioned-entry-point-only design.
  4. Reads back, via `GET /api/v1/cases/{case_id}/entities` (paginated),
     every entity TraceX's entity layer created, then groups their UUIDs by
     which raw `SYN-*` identifier (from this case's own `communities.json`)
     each one's `stable_identifiers`/`aliases` carries -- exact match only,
     never substring, to avoid false positives between identifiers sharing
     a prefix. This asks "what UUID(s) did this raw identifier resolve to?",
     never "did TraceX decide to merge these?": WP-2 creates one entity per
     *observation*, so the same real-world identity legitimately produces
     several distinct entity rows when it appears in several observations --
     that's exactly the "same" ground truth this script needs.
  5. Refuses to proceed if any *in-scope* `SYN-*` identifier from
     `communities.json` matched zero entities -- that would mean silently
     writing an incomplete truth file over a real ingestion or entity-
     resolution bug, not merely a missing capability. "In-scope" excludes
     `SYN-PER-*`/`SYN-LOC-*` tokens (see `OUT_OF_SCOPE_TOKEN_PREFIXES`
     above): TraceX's entity-resolution pipeline deliberately never treats
     a raw person/location identifier as identity-bearing, however it
     arrives -- a documented, permanent boundary, not a lowered bar. This
     also means the bridge-ambiguity scenario itself (`bridge_entity_id`
     is always a `SYN-PER-*` token) never produces its own "different"
     pair through this path -- `build_pairs` handles that honestly (no
     pair fabricated when the bridge never resolves to an entity).
  6. Builds ground-truth pairs from facts *we* authored and already know:
     two entities tracing back to the same SYN-* identifier are "same";
     the ambiguous bridge identity vs. an unrelated community member is
     "different".
  7. Rewrites entity_resolution_truth.json with real UUIDs,
     `pending_ingestion: false`, and no `blocked_reason`/`todo`.
  8. (Only with `--simulate-reviews-for-evaluation`, off by default.) Applies
     ground-truth-driven review decisions to this case's real candidates --
     see `simulate_reviews_for_evaluation.py`'s module docstring and
     TraceX's `docs/decisions/ADR-028-simulated-reviewer-evaluation-oracle.md`
     for the full design and its explicit non-production, Fulcrum-only
     boundary. Never used for Nightfall.

Cross-repo dependency (dev/test-data-generation script only -- not
production code): step 3 assumes TraceX is checked out as a sibling
directory (`../TraceX` relative to this script's own `--root`) and running
via `docker compose` with an `api` service reachable through `docker
compose exec`. Both assumptions are reasonable for the local dev workflow
this script is written for, but neither is validated up front -- if
TraceX lives elsewhere, or isn't running under Docker Compose, the
`subprocess.run` call in `resolve_entities_via_intelligence_worker` below
will fail with a plain "command not found"/nonzero-exit error, not a
helpful one. Noted explicitly here so this isn't silently fragile for
someone running this script from a different directory layout later.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from generate_operation_nightfall import FULCRUM_CASES  # noqa: E402

#: The v2 (Fulcrum) corpus manifest, matching `generate_operation_
#: nightfall.py`'s own `MANIFEST_V2`/`encoded()` conventions exactly --
#: this script rewrites `entity_resolution_truth.json` directly, but never
#: touched the manifest's recorded hash/size for it, so `verify_operation_
#: nightfall.py` failed with a real (if mechanical) hash/size mismatch
#: after the first real run. Not a design decision: purely recomputing and
#: patching one existing entry's bookkeeping fields, the same way the
#: corpus generator itself would if it regenerated the whole manifest.
MANIFEST_V2_PATH = "manifests/operation-nightfall.v2.json"

# Confirmed against a live instance's /openapi.json.
LOGIN_PATH = "/api/v1/auth/login"
ADMIN_PROVISION_USER_PATH = "/api/v1/admin/users"
EVIDENCE_UPLOAD_PATH = "/api/v1/cases/{case_id}/evidence"
CASE_CREATE_PATH = "/api/v1/cases"
ENTITY_MENTIONS_PATH = "/api/v1/cases/{case_id}/entities"
JOB_STATUS_PATH = "/api/v1/cases/{case_id}/jobs/{job_id}"

#: Gap-Closure follow-up: `SYN-PER-*`/`SYN-LOC-*` tokens are excluded from
#: the 100%-coverage requirement below -- a deliberate, documented scope
#: boundary, not a lowered bar. TraceX's entity-resolution pipeline
#: (`app/modules/graph/intelligence/sourcing.py`, the sibling repo)
#: deliberately never treats a raw `person_id`/`location_id`-shaped field
#: as an identity-bearing identifier, however it arrives (structured field
#: or OCR'd text): unlike a vehicle registration plate (independently
#: verifiable against a real-world registry, which is why `SYN-VEH-*`
#: tokens ARE required below, after TraceX's sourcing.py was extended to
#: recognize a structural vehicle_context/vehicle_id field the same way it
#: already recognized one extracted from document text), a person's or a
#: location's identity has no such external, directly-labeled ground
#: truth in real evidence -- that is exactly what entity resolution exists
#: to *infer* from contact-method/vehicle signals, never to accept as
#: given. Accepting a raw `person_id`/`location_id` field directly would
#: be circular for real (non-synthetic) data, so TraceX's `_IDENTIFIER_
#: TYPES` has no `person`/`location` kind at all, deliberately -- see
#: TraceX's `docs/qa/known-limitations.md` for the full writeup on that
#: side. This means Fulcrum's bridge-ambiguity scenario specifically
#: (`bridge_entity_id` is always a `SYN-PER-*` token) never produces its
#: own "different" pair through this path either -- `build_pairs` already
#: handles that honestly (no pair fabricated when the bridge never
#: resolves), not something this script papers over.
OUT_OF_SCOPE_TOKEN_PREFIXES = ("SYN-PER-", "SYN-LOC-")

#: `WorkerStatus` (TraceX's own enum, confirmed against /openapi.json):
#: queued/running are in-progress; every other value is terminal. Not
#: `succeeded` alone -- a `failed`/`cancelled` job has still finished
#: processing and must not be waited on further, even though it produced
#: no observations.
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}
JOB_POLL_INTERVAL_SECONDS = 3
#: Bounded wait, matching TruthGenerationIncomplete's existing "refuse
#: rather than guess" pattern: if jobs are still queued/running after this
#: long despite actively draining the queue (see
#: `DRAINABLE_WORKER_SERVICES`/`_drain_worker_queues_once` below), workers
#: are genuinely stuck (not running, crash-looping) -- proceeding to
#: resolve-entities against incompletely-processed evidence would silently
#: produce a partial, misleading truth file.
JOB_WAIT_TIMEOUT_SECONDS = 240

#: `POST /api/v1/auth/register` was removed by a TraceX gap-closure pass
#: (G5, "no public self-signup") that this script's own repo had no
#: visibility into -- a cross-repo integration gap distinct from the
#: CASE_CREATE_PATH/ENTITY_MENTIONS_PATH ones above. User provisioning is
#: now admin-gated (`ADMIN_PROVISION_USER_PATH`), which itself needs a
#: bootstrapped admin credential to already exist -- see
#: `app/modules/access_control/cli.py create-admin` in the TraceX repo.
#: This script never creates that admin itself; it only authenticates as
#: one that an operator has already bootstrapped, via these two env vars.
ADMIN_BOOTSTRAP_EMAIL_ENV_VAR = "TRACEX_ADMIN_BOOTSTRAP_EMAIL"
ADMIN_BOOTSTRAP_PASSWORD_ENV_VAR = "TRACEX_ADMIN_BOOTSTRAP_PASSWORD"

#: `CaseCreateRequest.classification` has no source in Fulcrum's own metadata
#: (no case bundle carries a clearance/classification field) -- this is a
#: script default, not a value read from the corpus.
DEFAULT_CASE_CLASSIFICATION = "confidential"

#: TraceX's own pagination ceiling for this route (`entity_api.py`'s
#: `MAX_ENTITY_LIST_LIMIT`).
ENTITY_PAGE_LIMIT = 200

#: `metadata/evidence-register.json` modalities that are ground-truth/
#: corpus-authoring artifacts, never real evidence -- uploading them would
#: leak the answer key (communities.json, entities.json, ...) into the case
#: as if an investigator had produced it. `expected_result_path` lives
#: outside `case_dir` entirely and was never reachable by the old rglob
#: walk either.
NON_EVIDENCE_MODALITIES = {"metadata", "expected_result", "entity_resolution_truth"}

#: `documents/*` always maps to `document` regardless of filename (both
#: `.pdf` and `.txt` are accepted content types for `SourceType.DOCUMENT`);
#: every other directory needs a per-file mapping since e.g. `structured/`
#: mixes three different source_types across its four files. Confirmed
#: against `app/modules/evidence_lifecycle/routing.py`'s
#: `SOURCE_TYPE_CONTENT_TYPES` (accepted content types per source_type) and
#: each file's declared content_type in evidence-register.json
#: (cross-checked: declared content_type, extension-guessed type, and
#: actual magic bytes all agree for every file in both case-fulcrum-dev and
#: case-fulcrum-val). `audio/turns.json` is `structured_json`, NOT
#: `audio_transcript`: TraceX's `transcript_import_v1` (`app/modules/
#: communication_processing/audio/transcript_import.py`) requires each
#: segment to carry `start_ms`/`end_ms`/`confidence`/`source_segment_id` --
#: real ASR-derived timing this file was never going to have. Its own
#: content confirms why: `"media_present": false`, `"note": "Metadata-only
#: fixture for development/validation; no rendered audio."`. Renaming its
#: `turns` array to `segments` would still fail on those missing required
#: fields; fabricating fake millisecond timings/confidence to satisfy them
#: would contradict the fixture's own honest "not rendered" design. It's
#: generic case metadata, not a real transcript -- `structured_json` (the
#: same generic fallback below) reflects that honestly.
#: `structured/sightings.json`, `structured/contradictions.json`, and
#: `visual/timeline.json` also fall to `structured_json`, the documented
#: generic fallback for JSON evidence that isn't specifically
#: CDR/financial-shaped or a real ASR transcript.
SOURCE_TYPE_BY_DIRECTORY = {"documents": "document"}
SOURCE_TYPE_BY_PATH_SUFFIX = {
    "audio/turns.json": "structured_json",
    "social/messages.json": "chat",
    "structured/cdr.csv": "cdr",
    "structured/transactions.csv": "financial",
    "structured/sightings.json": "structured_json",
    "structured/contradictions.json": "structured_json",
    "visual/timeline.json": "structured_json",
}


def evidence_files_for_case(root, case_dir):
    """Yield `(Path, source_type)` for every real evidence file in `case_dir`,
    per its own `metadata/evidence-register.json` -- the generator's
    authoritative manifest, not a blind directory walk. Raises
    `TruthGenerationIncomplete` if a listed evidence file has no known
    source_type mapping, rather than guessing one.
    """
    register = json.loads((case_dir / "metadata/evidence-register.json").read_text())
    for entry in register["files"]:
        if entry["modality"] in NON_EVIDENCE_MODALITIES:
            continue
        path = root / entry["path"]
        suffix = "/".join(path.parts[-2:])
        source_type = SOURCE_TYPE_BY_DIRECTORY.get(path.parent.name) or SOURCE_TYPE_BY_PATH_SUFFIX.get(
            suffix
        )
        if source_type is None:
            raise TruthGenerationIncomplete(
                f"evidence-register.json lists {entry['path']!r} (modality={entry['modality']!r}) "
                "with no known source_type mapping -- refusing to guess one. Add it to "
                "SOURCE_TYPE_BY_DIRECTORY or SOURCE_TYPE_BY_PATH_SUFFIX after confirming the right "
                "source_type against app/modules/evidence_lifecycle/routing.py's "
                "SOURCE_TYPE_CONTENT_TYPES."
            )
        yield path, source_type


class TraceXCapabilityMissing(RuntimeError):
    """Raised when a TraceX endpoint this script needs does not exist yet."""


class TruthGenerationIncomplete(RuntimeError):
    """Raised when TraceX responded but the result can't be trusted as
    complete ground truth -- e.g. a known SYN-* identifier resolved to zero
    entities. Never papered over by writing a truth file anyway."""


def post_json(api_url, path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(api_url.rstrip("/") + path, data=json.dumps(payload).encode(),
                                      headers=headers, method="POST")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def get_json(api_url, path, token=None, params=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = api_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def upload_evidence(api_url, token, case_id, file_path, source_type, classification="unclassified"):
    """Multipart POST matching the confirmed Body_upload_evidence_...  schema."""
    boundary = uuid.uuid4().hex
    data = file_path.read_bytes()
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    def part(name, value=None, filename=None, file_content_type=None, file_bytes=None):
        header = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            header += f'; filename="{filename}"'
        chunk = f"--{boundary}\r\n{header}\r\n"
        if file_content_type:
            chunk += f"Content-Type: {file_content_type}\r\n"
        chunk = chunk.encode() + b"\r\n"
        return chunk + (file_bytes if file_bytes is not None else value.encode()) + b"\r\n"

    body = (part("source_type", value=source_type) + part("classification", value=classification)
            + part("file", filename=file_path.name, file_content_type=content_type, file_bytes=data)
            + f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(api_url.rstrip("/") + EVIDENCE_UPLOAD_PATH.format(case_id=case_id),
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def register_and_login(api_url):
    """No public self-registration exists (G5) -- provisioning a probe user
    requires an already-bearer-authenticated admin. Logs in as an admin an
    operator has already bootstrapped (`ADMIN_BOOTSTRAP_EMAIL_ENV_VAR`/
    `ADMIN_BOOTSTRAP_PASSWORD_ENV_VAR`, never a CLI argument -- see
    `app/modules/access_control/cli.py create-admin` in the TraceX repo),
    provisions a fresh throwaway probe account via `ADMIN_PROVISION_USER_PATH`,
    then logs in as *that* account exactly as the old self-registration flow
    did -- everything downstream of this function is unchanged.
    """
    admin_email = os.environ.get(ADMIN_BOOTSTRAP_EMAIL_ENV_VAR)
    admin_password = os.environ.get(ADMIN_BOOTSTRAP_PASSWORD_ENV_VAR)
    if not admin_email or not admin_password:
        raise TraceXCapabilityMissing(
            f"No public self-registration exists on this TraceX build (G5). Set "
            f"{ADMIN_BOOTSTRAP_EMAIL_ENV_VAR} and {ADMIN_BOOTSTRAP_PASSWORD_ENV_VAR} to an "
            "already-bootstrapped admin's credentials (see "
            "app/modules/access_control/cli.py create-admin in the TraceX repo)."
        )
    admin_login = post_json(api_url, LOGIN_PATH, {"email": admin_email, "password": admin_password})
    admin_token = admin_login["access_token"]

    probe_email = f"fulcrum-truth-{uuid.uuid4().hex[:12]}@example.test"
    probe_password = uuid.uuid4().hex + "Aa1!"
    post_json(
        api_url,
        ADMIN_PROVISION_USER_PATH,
        {"email": probe_email, "password": probe_password, "display_name": "Fulcrum Truth Probe"},
        token=admin_token,
    )
    login = post_json(api_url, LOGIN_PATH, {"email": probe_email, "password": probe_password})
    return login["access_token"]


def create_or_join_case(api_url, token, case_id):
    """`case_id` (the Fulcrum case slug, e.g. `case-fulcrum-dev`) is never
    sent to TraceX verbatim as `case_reference`: `cases.uq_cases_case_
    reference` is a real UNIQUE constraint, TraceX has no case deletion or
    archival path (`CaseStatus` has no write-side transition at all -- only
    `GET .../status` reads it), and this script has no way to resume a case
    owned by an earlier run's now-credential-less throwaway probe account.
    A short random suffix keeps every run's `case_reference` unique without
    ever touching existing rows. Nothing downstream depends on TraceX's
    internal `case_reference` matching this Fulcrum slug -- the truth file's
    own `case_id` field and every other repo-local reference use `case_id`
    unchanged; only the value sent to TraceX gets suffixed. TraceX assigns
    its own UUID `case_id`, returned here."""
    case_reference = f"{case_id}-{uuid.uuid4().hex[:8]}"
    body = {"case_reference": case_reference, "classification": DEFAULT_CASE_CLASSIFICATION}
    return post_json(api_url, CASE_CREATE_PATH, body, token=token)["case_id"]


#: `structured-worker`/`communication-worker` have no real poll-loop mode
#: (`--once` + `restart: unless-stopped`; Docker's own restart backoff
#: grows between consecutive restarts and does not converge in any bounded
#: wait in practice -- measured directly this session: 300s left 6 of 11
#: jobs still queued). `docker compose run --rm <service>` runs a fresh
#: one-off container per call, bypassing that backoff entirely -- the same
#: fix applied manually, successfully, earlier this session. `media-worker`
#: is not included: it runs `--loop` (a real, continuously-polling mode),
#: so it never needs this nudge.
DRAINABLE_WORKER_SERVICES = ("structured-worker", "communication-worker")


def _drain_worker_queues_once(tracex_root):
    """One fresh `docker compose run --rm` per drainable worker service --
    best-effort: a nonzero exit here (e.g. genuinely no job available) is
    not itself a failure, only `wait_for_jobs_to_complete`'s own timeout
    is."""
    for service in DRAINABLE_WORKER_SERVICES:
        subprocess.run(
            ["docker", "compose", "run", "--rm", service],
            cwd=tracex_root,
            capture_output=True,
            text=True,
        )


def wait_for_jobs_to_complete(api_url, token, case_id, job_ids, *, tracex_root):
    """Bounded wait: actively drain `DRAINABLE_WORKER_SERVICES` (see above)
    until every job in `job_ids` reaches a terminal `WorkerStatus`
    (succeeded/failed/cancelled), or raise `TruthGenerationIncomplete`
    after `JOB_WAIT_TIMEOUT_SECONDS`.

    Entity/candidate generation reads whatever observations already exist
    at the moment it runs -- calling it while evidence is still queued/
    running would silently produce a partial, misleading truth file rather
    than a clear failure, exactly the "refuse rather than guess" this
    script already applies to unresolved SYN-* tokens.
    """
    pending = set(job_ids)
    deadline = time.monotonic() + JOB_WAIT_TIMEOUT_SECONDS
    while pending:
        _drain_worker_queues_once(tracex_root)
        for job_id in list(pending):
            job = get_json(api_url, JOB_STATUS_PATH.format(case_id=case_id, job_id=job_id), token=token)
            if job["status"] in TERMINAL_JOB_STATUSES:
                pending.discard(job_id)
        if not pending:
            break
        if time.monotonic() >= deadline:
            raise TruthGenerationIncomplete(
                f"{len(pending)} of {len(job_ids)} evidence-processing job(s) for case {case_id} "
                f"still queued/running after {JOB_WAIT_TIMEOUT_SECONDS}s: {sorted(pending)}. "
                "Refusing to run entity-resolution against incompletely-processed evidence -- "
                "check whether TraceX's workers (structured-worker/communication-worker/"
                "media-worker) are actually running."
            )
        time.sleep(JOB_POLL_INTERVAL_SECONDS)


def resolve_entities_via_intelligence_worker(case_id, *, tracex_root):
    """Shell out to TraceX's `intelligence_worker.py --resolve-entities` --
    see this module's own docstring, "Cross-repo dependency", for the
    assumptions this makes (sibling checkout, Docker Compose, an `api`
    service reachable via `docker compose exec`). Entity/candidate
    generation is a separate, manual, CLI-only capability with no HTTP-
    reachable form (matching `--evaluate`'s own sanctioned-entry-point-only
    design) -- this is the only way this script can trigger it.

    Raises `TruthGenerationIncomplete` on any nonzero exit -- a failure
    here means entity-resolution genuinely didn't run, not something to
    silently proceed past.
    """
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "api",
            "python",
            "-m",
            "app.modules.graph.intelligence_worker",
            "--resolve-entities",
            "--case-id",
            str(case_id),
        ],
        cwd=tracex_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TruthGenerationIncomplete(
            f"intelligence_worker.py --resolve-entities --case-id {case_id} failed "
            f"(exit {result.returncode}) in {tracex_root}: {result.stderr.strip() or result.stdout.strip()}"
        )
    print(f"  {result.stdout.strip()}")


def fetch_resolved_mentions(api_url, token, case_id, known_tokens):
    """Return {synthetic_identifier: [entity_uuid, ...]} for every raw SYN-*
    identifier (from `known_tokens`, this case's own `communities.json`) that
    at least one TraceX entity's `stable_identifiers`/`aliases` carries.

    Exact match only (`value == token`), never substring -- two identifiers
    sharing a prefix (e.g. two `SYN-PHONE-FULD-*` values) must never cross-
    match. Raises `TruthGenerationIncomplete` if any `known_tokens` entry
    matches zero entities: a silently incomplete truth file would hide a
    real ingestion or entity-resolution bug, not just a missing capability.
    """
    grouped: dict[str, list[str]] = {}
    cursor = None
    while True:
        params = {"limit": ENTITY_PAGE_LIMIT}
        if cursor is not None:
            params["cursor"] = cursor
        page = get_json(
            api_url, ENTITY_MENTIONS_PATH.format(case_id=case_id), token=token, params=params
        )
        for entity in page["items"]:
            candidates = list(entity.get("stable_identifiers", {}).values())
            candidates.extend(entity.get("aliases", []))
            for value in candidates:
                if value in known_tokens:
                    grouped.setdefault(value, []).append(entity["entity_id"])
        cursor = page.get("next_cursor")
        if cursor is None:
            break

    unresolved = sorted(token for token in known_tokens if token not in grouped)
    if unresolved:
        raise TruthGenerationIncomplete(
            f"{len(unresolved)} known SYN-* identifier(s) from communities.json never resolved to any "
            f"TraceX entity for case {case_id}: {unresolved}. Refusing to write an incomplete truth "
            "file -- this signals an ingestion or entity-resolution bug, not a reachability problem."
        )
    return grouped


def build_pairs(grouped, bridge_id, community_a_ids, community_b_ids):
    pairs = []
    for identifier, uuids in grouped.items():
        if len(uuids) >= 2:
            pairs.append({"left_entity_id": uuids[0], "right_entity_id": uuids[1], "label": "same"})
    other_a = next((i for i in community_a_ids if i != bridge_id and i in grouped), None)
    other_b = next((i for i in community_b_ids if i != bridge_id and i in grouped), None)
    if bridge_id in grouped and other_a:
        pairs.append({"left_entity_id": grouped[bridge_id][0], "right_entity_id": grouped[other_a][0], "label": "different"})
    if bridge_id in grouped and other_b:
        pairs.append({"left_entity_id": grouped[bridge_id][0], "right_entity_id": grouped[other_b][0], "label": "different"})
    if other_a and other_b:
        pairs.append({"left_entity_id": grouped[other_a][0], "right_entity_id": grouped[other_b][0], "label": "different"})
    return pairs


def update_manifest_entry_for_truth_file(root, truth_path, new_bytes):
    """Patch the v2 manifest's recorded `sha256`/`bytes` for exactly the one
    entry matching `truth_path`, after rewriting it with real content.

    Mechanical bookkeeping only, matching `generate_operation_nightfall.
    py`'s own `encoded()` formatting exactly (`sort_keys=True, indent=2,
    ensure_ascii=False` + trailing newline) so a subsequent real corpus
    regeneration's manifest diff stays clean. Raises if no matching entry
    exists -- a manifest missing this case's truth-file entry entirely is
    a real inconsistency to surface, not something to silently add to.
    """
    manifest_path = root / MANIFEST_V2_PATH
    manifest = json.loads(manifest_path.read_text())
    relative_path = truth_path.relative_to(root).as_posix()
    matches = [entry for entry in manifest["files"] if entry["path"] == relative_path]
    if len(matches) != 1:
        raise TruthGenerationIncomplete(
            f"expected exactly one manifest entry for {relative_path!r} in {manifest_path}, "
            f"found {len(matches)}"
        )
    matches[0]["sha256"] = hashlib.sha256(new_bytes).hexdigest()
    matches[0]["bytes"] = len(new_bytes)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", required=True, choices=list(FULCRUM_CASES))
    parser.add_argument("--root", default=".")
    parser.add_argument("--tracex-api", default=os.environ.get("TRACEX_API_URL"))
    parser.add_argument(
        "--simulate-reviews-for-evaluation",
        action="store_true",
        help=(
            "EVALUATION-ONLY, off by default. After writing the truth file, apply "
            "ground-truth-driven review decisions to this case's real candidates via "
            "scripts/simulate_reviews_for_evaluation.py, so graph/intelligence/"
            "evaluation.py (TraceX) has real decisions to score against -- see "
            "TraceX's docs/decisions/ADR-028-simulated-reviewer-evaluation-oracle.md. "
            "Never pass this for Nightfall; this flag only ever runs against Fulcrum."
        ),
    )
    args = parser.parse_args()
    if not args.tracex_api:
        parser.error("--tracex-api or $TRACEX_API_URL is required")

    root = Path(args.root).resolve()
    case_dir = root / "operation-fulcrum" / args.case
    truth_path = case_dir / "entity_resolution_truth.json"
    communities = json.loads((case_dir / "metadata/communities.json").read_text())
    truth = json.loads(truth_path.read_text())
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
        case_id = create_or_join_case(args.tracex_api, token, args.case)
        job_ids = []
        for path, source_type in evidence_files_for_case(root, case_dir):
            response = upload_evidence(args.tracex_api, token, case_id, path, source_type=source_type)
            job_ids.append(response["job"]["job_id"])
        print(f"waiting for {len(job_ids)} evidence-processing job(s) to complete...")
        wait_for_jobs_to_complete(args.tracex_api, token, case_id, job_ids, tracex_root=tracex_root)
        print("running entity-resolution (intelligence_worker.py --resolve-entities)...")
        resolve_entities_via_intelligence_worker(case_id, tracex_root=tracex_root)
        grouped = fetch_resolved_mentions(args.tracex_api, token, case_id, known_tokens)
    except urllib.error.URLError as error:
        parser.error(f"could not reach TraceX at {args.tracex_api}: {error}")
    except (TraceXCapabilityMissing, TruthGenerationIncomplete) as error:
        parser.error(str(error))

    truth["entity_pairs"] = build_pairs(grouped, communities["bridge_entity_id"],
        communities["community_a"]["entity_ids"], communities["community_b"]["entity_ids"])
    truth["pending_ingestion"] = False
    truth.pop("blocked_reason", None)
    truth.pop("todo", None)
    new_bytes = (json.dumps(truth, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    truth_path.write_bytes(new_bytes)
    update_manifest_entry_for_truth_file(root, truth_path, new_bytes)
    print(f"wrote {len(truth['entity_pairs'])} entity_resolution_truth pairs for {args.case}")

    if args.simulate_reviews_for_evaluation:
        # Deferred import: avoids a top-level circular import, since
        # simulate_reviews_for_evaluation.py itself imports post_json/
        # get_json from this module.
        from simulate_reviews_for_evaluation import apply_simulated_reviews

        print("applying simulated-reviewer-oracle decisions (evaluation-only)...")
        counts = apply_simulated_reviews(args.tracex_api, token, case_id, truth_path)
        print(
            f"  verified_same={counts['verified_same']} rejected={counts['rejected']} "
            f"skipped_uncovered={counts['skipped_uncovered']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
