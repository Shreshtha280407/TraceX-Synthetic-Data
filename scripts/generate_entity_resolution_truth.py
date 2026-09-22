#!/usr/bin/env python3
"""Populate entity_resolution_truth.json from a REAL TraceX ingestion run.

Not part of the offline, deterministic corpus generator: this talks to a live
TraceX instance, so it is never invoked by generate_operation_nightfall.py or
by CI.

STATUS (checked against a live tracex-api instance): auth (register/login)
and evidence upload are real, working endpoints and are implemented below
using TraceX's actual OpenAPI contract (GET /openapi.json). But two things
this script needs do not exist in that build yet:

  1. Case creation. There is no `POST /api/v1/cases` (or equivalent) in the
     API. `POST /api/v1/cases/{case_id}/evidence` requires the caller to
     already be a member of an existing case and returns 403 otherwise.
     Nothing reachable from this script can create or join a case.
  2. Entity resolution readback. The only entity-shaped object the API
     produces is `ExtractedEntityMention`, documented in its own schema as
     "a raw, unresolved entity mention as it literally appeared in the
     source" -- it has no id field. There is no `/entities` endpoint, and
     no endpoint at all for a client to read back `ObservationV1` records
     (only internal worker endpoints touch them). Docstrings elsewhere in
     that API mark the running build as Phase 1-2.3; the entity-resolution
     layer is later-phase work (this host also has `tracex-phase4-gate-*`
     containers present but not started, consistent with that).

So `main()` below will get past auth, then stop with a clear error at
whichever of the two gaps it hits first -- it will not fabricate a case ID
or entity UUIDs to paper over a capability TraceX doesn't have yet. Rerun
this once TraceX ships both:

    python scripts/generate_entity_resolution_truth.py \
        --case case-fulcrum-dev --root . --tracex-api "$TRACEX_API_URL"

What it does once TraceX actually supports it:
  1. Registers/logs in a probe account and creates (or is given) a case.
  2. Uploads every file in the case bundle (operation-fulcrum/<case>/) as
     evidence.
  3. Reads back the per-mention entity UUIDs TraceX's entity layer assigned
     for each raw `SYN-*` identifier (before any resolution/merge step --
     this asks "what UUID did this raw mention get?", never "did TraceX
     decide to merge these?").
  4. Builds ground-truth pairs from facts *we* authored and already know:
     two mentions of the same SYN-* identifier are "same"; mentions of two
     distinct identifiers (in particular the ambiguous bridge vs. an
     unrelated community member) are "different".
  5. Rewrites entity_resolution_truth.json with real UUIDs,
     `pending_ingestion: false`, and no `blocked_reason`/`todo`.

CASE_PATH and ENTITY_MENTIONS_PATH are the two integration points to fill in
once TraceX ships them; everything else (grouping, pairing, file rewrite) is
already API-shape independent.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from generate_operation_nightfall import FULCRUM_CASES  # noqa: E402

# Confirmed against a live instance's /openapi.json.
REGISTER_PATH = "/api/v1/auth/register"
LOGIN_PATH = "/api/v1/auth/login"
EVIDENCE_UPLOAD_PATH = "/api/v1/cases/{case_id}/evidence"

# Not implemented by TraceX as of this writing -- see the module docstring.
CASE_CREATE_PATH = None
ENTITY_MENTIONS_PATH = None


class TraceXCapabilityMissing(RuntimeError):
    """Raised when a TraceX endpoint this script needs does not exist yet."""


def post_json(api_url, path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(api_url.rstrip("/") + path, data=json.dumps(payload).encode(),
                                      headers=headers, method="POST")
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
    email = f"fulcrum-truth-{uuid.uuid4().hex[:12]}@example.test"
    password = uuid.uuid4().hex + "Aa1!"
    post_json(api_url, REGISTER_PATH, {"email": email, "password": password, "display_name": "Fulcrum Truth Probe"})
    login = post_json(api_url, LOGIN_PATH, {"email": email, "password": password})
    return login["access_token"]


def create_or_join_case(api_url, token, case_id):
    if CASE_CREATE_PATH is None:
        raise TraceXCapabilityMissing(
            "TraceX has no case-creation endpoint yet (checked /openapi.json). Evidence upload requires "
            "existing case membership and 403s otherwise. Fill in CASE_CREATE_PATH once TraceX ships one.")
    return post_json(api_url, CASE_CREATE_PATH, {"case_id": case_id}, token=token)["case_id"]


def fetch_resolved_mentions(api_url, token, case_id):
    """Return {synthetic_identifier: [entity_uuid, ...]} for every raw SYN-* mention TraceX resolved."""
    if ENTITY_MENTIONS_PATH is None:
        raise TraceXCapabilityMissing(
            "TraceX exposes no entity-resolution read surface yet (checked /openapi.json): "
            "ExtractedEntityMention has no id field, and there is no /entities or observation-listing "
            "endpoint. Fill in ENTITY_MENTIONS_PATH once TraceX's entity layer ships one.")
    raise NotImplementedError  # pragma: no cover -- unreachable until the path above is filled in


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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", required=True, choices=list(FULCRUM_CASES))
    parser.add_argument("--root", default=".")
    parser.add_argument("--tracex-api", default=os.environ.get("TRACEX_API_URL"))
    args = parser.parse_args()
    if not args.tracex_api:
        parser.error("--tracex-api or $TRACEX_API_URL is required")

    root = Path(args.root).resolve()
    case_dir = root / "operation-fulcrum" / args.case
    truth_path = case_dir / "entity_resolution_truth.json"
    communities = json.loads((case_dir / "metadata/communities.json").read_text())
    truth = json.loads(truth_path.read_text())

    try:
        token = register_and_login(args.tracex_api)
        case_id = create_or_join_case(args.tracex_api, token, args.case)
        for path in sorted(case_dir.rglob("*")):
            if path.is_file() and path.name not in {"entity_resolution_truth.json", "evidence-register.json"}:
                upload_evidence(args.tracex_api, token, case_id, path, source_type="structured_json")
        grouped = fetch_resolved_mentions(args.tracex_api, token, case_id)
    except urllib.error.URLError as error:
        parser.error(f"could not reach TraceX at {args.tracex_api}: {error}")
    except TraceXCapabilityMissing as error:
        parser.error(str(error))

    truth["entity_pairs"] = build_pairs(grouped, communities["bridge_entity_id"],
        communities["community_a"]["entity_ids"], communities["community_b"]["entity_ids"])
    truth["pending_ingestion"] = False
    truth.pop("blocked_reason", None)
    truth.pop("todo", None)
    truth_path.write_text(json.dumps(truth, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(truth['entity_pairs'])} entity_resolution_truth pairs for {args.case}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
