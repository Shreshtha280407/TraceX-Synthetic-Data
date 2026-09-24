#!/usr/bin/env python3
"""Validate the complete fictional corpus, provenance, media, and isolation."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import struct
import wave
import zlib
from datetime import datetime
from pathlib import Path

from generate_operation_nightfall import (
    CASES, SHARED, SPEAKERS, SAFE, MANIFEST, NOTICE, authored_files, checked_path,
    content_type, digest, entities_for, envelope, expectation, modality, provenance_records, run, scene_plan, speech_plan,
    FULCRUM_CASES, FULCRUM_SEEDS, FULCRUM_MOTIF_CHAIN, MANIFEST_V2, SYN_CORPUS_FULCRUM,
    fulcrum_authored_files, fulcrum_provenance_records,
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load(path):
    return json.loads(path.read_bytes(), object_pairs_hook=unique_object)


def records(path):
    reader = csv.DictReader(io.StringIO(path.read_text(), newline=""), strict=True)
    require(reader.fieldnames and len(set(reader.fieldnames)) == len(reader.fieldnames), f"bad CSV header: {path}")
    rows = list(reader)
    require(rows and all(None not in row and None not in row.values() for row in rows), f"malformed CSV: {path}")
    return rows


def synthetic_fields(value, case_id):
    """Identifiers are SYN-*; case directory keys and composite identity keys are structural exceptions."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "case_id":
                require(item == case_id, f"foreign case ownership: {item}")
            elif key.endswith("_id") and isinstance(item, str):
                require(re.fullmatch(r"SYN-[A-Z0-9-]+", item), f"non-synthetic identifier: {key}={item}")
            synthetic_fields(item, case_id)
    elif isinstance(value, list):
        for item in value:
            synthetic_fields(item, case_id)


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))


def rgb(path, seconds=None):
    args = ["ffmpeg", "-v", "error", "-nostdin"]
    if seconds is not None:
        args += ["-ss", str(seconds)]
    return run(args + ["-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "pipe:1"])


def png_check(path):
    data = path.read_bytes()
    require(data[:8] == b"\x89PNG\r\n\x1a\n", f"invalid PNG: {path}")
    offset, kinds = 8, []
    while offset < len(data):
        size = struct.unpack(">I", data[offset:offset+4])[0]
        kind = data[offset+4:offset+8]
        payload = data[offset+8:offset+8+size]
        crc = struct.unpack(">I", data[offset+8+size:offset+12+size])[0]
        require(crc == zlib.crc32(kind+payload) & 0xffffffff, f"PNG CRC mismatch: {path}")
        if kind == b"IHDR":
            require(struct.unpack(">II", payload[:8]) == (1280, 720), f"PNG dimensions: {path}")
        kinds.append(kind)
        offset += size+12
    require(offset == len(data) and kinds[0] == b"IHDR" and kinds[-1] == b"IEND", f"incomplete PNG: {path}")


def verify_audio(directory, case_id):
    code = CASES[case_id]
    metadata = load(directory / "audio/turns.json")
    turns = metadata["turns"]
    synthetic_fields(metadata, case_id)
    require(metadata["contains_speech"] is True and len(turns) == 9, "nine genuine synthesized speech turns required")
    require(metadata["sample_rate"] == 22050 and metadata["duration_seconds"] == 81, "audio metadata duration/rate")
    require(metadata["transcript_origin"] == "authored TTS script; not an ASR or diarization worker result", "audio origin")
    require(metadata["diarization_expectation"] == "ingestion and reviewable speaker turns; perfect diarization is not required", "unsafe diarization expectation")
    require(metadata["speaker_identity_scope"] == "case_local_audio_role; shared role names never imply shared person identity", "speaker role scope")
    require(set(metadata) == {"case_id", "synthetic", "source", "contains_speech", "recording_id", "duration_seconds", "sample_rate", "transcript_origin", "diarization_expectation", "speaker_identity_scope", "turns"}, "unexpected audio metadata fields")
    with wave.open(str(directory / "audio/discussion.wav")) as handle:
        require((handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getcomptype()) == (1, 2, 22050, "NONE"), "WAV format")
        require(handle.getnframes() == 81*22050, "WAV must contain 81 seconds")
        pcm = handle.readframes(handle.getnframes())
    rate = 22050
    previous = 0
    transcript = [NOTICE, f"Case {case_id}. Authored offline TTS script; not worker output."]
    rttm = []
    for i, (turn, plan) in enumerate(zip(turns, speech_plan(case_id), strict=True)):
        first, last = turn["start_sample"], turn["end_sample"]
        require(first == plan["slot_start"]*rate + rate//4, "speech turn start does not match its slot")
        require(first > previous and rate < last-first < 8.5*rate, "overlap/empty/truncated speech turn")
        require(last <= (i+1)*9*rate, "turn exceeds 9-second slot")
        require(turn == envelope(case_id, segment_id=f"SYN-SEG-{code}-{i+1:02d}",
            speaker_id=plan["speaker_id"], voice=SPEAKERS[plan["speaker_id"]], text=plan["text"], entity_refs=plan["entity_refs"],
            start_sample=first, end_sample=last, start_seconds=round(first/rate, 6), end_seconds=round(last/rate, 6)),
            "turn differs from authored safe speech plan")
        require(not any(pcm[previous*2:first*2]), "unannotated signal outside speaker turns")
        samples = struct.unpack(f"<{last-first}h", pcm[first*2:last*2])
        require(sum(abs(sample) > 100 for sample in samples) > .1*len(samples), "turn contains insufficient speech signal")
        transcript.append(f"[{turn['start_seconds']:.6f} - {turn['end_seconds']:.6f}] {turn['speaker_id']}: {turn['text']}")
        rttm.append(f"SPEAKER SYN-AUD-{code}-01 1 {turn['start_seconds']:.6f} {(last-first)/rate:.6f} <NA> <NA> {turn['speaker_id']} <NA> <NA>")
        previous = last
    require(not any(pcm[previous*2:]), "unannotated trailing audio")
    require((directory / "audio/transcript.txt").read_text() == "\n".join(transcript)+"\n", "transcript disagrees with turns")
    require((directory / "audio/speakers.rttm").read_text() == "\n".join(rttm)+"\n", "RTTM timing/speakers disagree with turns")
    require({t["speaker_id"] for t in turns} == set(SPEAKERS) and len({t["voice"] for t in turns}) == 3, "three distinct voices required")
    return 81


def verify_visual(directory, case_id):
    video = directory / "visual/footage.mp4"
    info = probe(video)
    streams = [s for s in info["streams"] if s["codec_type"] == "video"]
    require(len(streams) == 1, "expected one video stream")
    stream = streams[0]
    require((stream["width"], stream["height"]) == (1280, 720), "video must be 1280x720")
    require(abs(float(info["format"]["duration"])-27) < .01, "video must be 27 seconds")
    require(int(stream["nb_frames"]) == 270, "video frame count mismatch")
    require(stream["codec_name"] == "h264", "expected H.264 video")
    # Decode the full stream to catch truncated media, then compare scene boundaries
    # with their rendered PNGs. These are fixture-integrity checks, not model outputs.
    run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(video), "-f", "null", "-"])
    for scene in scene_plan(case_id):
        x, y, width, height = scene["label_bbox_normalized"]
        require(0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1-x and 0 < height <= 1-y, "invalid label bbox")
        still = directory / scene["still_path"]
        png_check(still)
        expected = rgb(still)
        require(len(expected) == 1280*720*3, "invalid decoded still size")
        for seconds in (scene["start_seconds"]+.2, scene["end_seconds"]-.2):
            frame = rgb(video, seconds)
            require(len(frame) == len(expected), "invalid decoded video frame")
            error = sum(abs(a-b) for a, b in zip(frame, expected))/len(frame)
            require(error < 4, f"video scene/annotation disagree at {seconds}s: mean RGB error={error:.3f}")
    return 27


def verify(root, quiet=False):
    root = root.resolve()
    manifest = load(checked_path(root, MANIFEST))
    require(manifest["schema_version"] == 2 and manifest["case_ids"] == list(CASES), "wrong corpus schema/cases")
    require(all(manifest.get(key) is value for key, value in SAFE.items()), "unsafe manifest assertions")
    entries = manifest["files"]
    require(entries and len({e["path"] for e in entries}) == len(entries), "empty/duplicate manifest paths")
    paths = {e["path"] for e in entries}
    actual = set()
    for path in (root / "operation-nightfall").rglob("*"):
        checked_path(root, path.relative_to(root).as_posix())
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    for case_id in CASES:
        # expected-results/ is shared with the v2 corpus; only claim this case's own file.
        expected_path = root / f"expected-results/{case_id}.json"
        if expected_path.is_file():
            actual.add(f"expected-results/{case_id}.json")
    require(actual == paths, f"manifest coverage mismatch: {sorted(actual ^ paths)}")
    total = (root / MANIFEST).stat().st_size
    per_case = {case: [] for case in CASES}
    for entry in entries:
        case = entry["case_id"]
        require(case in CASES, "unexpected manifest case")
        relative = entry["path"]
        require(relative.startswith(f"operation-nightfall/{case}/") or relative == f"expected-results/{case}.json", "artifact assigned to wrong case")
        path = checked_path(root, relative)
        require(path.is_file(), f"missing artifact: {relative}")
        data = path.read_bytes()
        require(digest(data) == entry["sha256"] and len(data) == entry["bytes"], f"hash/size mismatch: {relative}")
        require(entry["content_type"] == content_type(relative), "wrong content type")
        require(entry["generated_synthetic_data"] is True, "missing synthetic marker")
        require(entry["expected_result_path"] == f"expected-results/{case}.json", "wrong expected result link")
        entry_modality = "expected_result" if relative.startswith("expected-results/") else modality(relative, None)
        require(entry["modality"] == entry_modality, "wrong modality")
        if path.suffix == ".json" and not relative.endswith("entity_resolution_truth.json"):
            # entity_resolution_truth.json legitimately holds real Entity.entity_id UUIDs
            # once ingested (ADR-025), not SYN-* placeholders; validated separately below.
            value = load(path)
            synthetic_fields(value, case)
        total += len(data)
        per_case[case].append(entry)
    require(total < 100_000_000, f"acceptance corpus exceeds 100 MB: {total}")

    inventories = {}
    for case_id, code in CASES.items():
        directory = root / "operation-nightfall" / case_id
        case_entries = per_case[case_id]
        require({entry["modality"] for entry in case_entries} == {"documents", "structured", "social", "audio", "visual", "metadata", "expected_result", "entity_resolution_truth"}, "case missing modality")
        authored = authored_files(case_id, manifest["seed"])
        known_paths = set(authored) | {"audio/discussion.wav", "audio/transcript.txt", "audio/turns.json", "audio/speakers.rttm",
            "visual/footage.mp4", "visual/still-01.png", "visual/still-02.png", "visual/still-03.png", "metadata/evidence-register.json"}
        known_paths = {f"operation-nightfall/{case_id}/{p}" for p in known_paths} | {f"expected-results/{case_id}.json"}
        require({entry["path"] for entry in case_entries} == known_paths, "unexpected or missing case artifact")
        safe_register, safe_expected = provenance_records(case_id, known_paths)
        require(load(directory / "metadata/evidence-register.json") == safe_register, "register differs from authored provenance assertions")
        require(load(root / f"expected-results/{case_id}.json") == safe_expected, "expected result differs from authored safe assertions")
        for relative, expected_bytes in authored.items():
            if relative.endswith("entity_resolution_truth.json"):
                # This file's own authored placeholder documents that it is meant to be
                # overwritten later by generate_nightfall_entity_resolution_truth.py; its
                # post-ingestion content is validated separately below.
                continue
            path = directory / relative
            require(path.read_bytes() == expected_bytes, f"content deviates from closed fictional templates: {case_id}/{relative}")
        truth = load(directory / "entity_resolution_truth.json")
        require(truth["schema_version"] == "entity_resolution_truth.v1", "wrong entity_resolution_truth schema")
        require(truth["case_id"] == case_id, "entity_resolution_truth case mismatch")
        require(isinstance(truth["entity_pairs"], list), "entity_resolution_truth.entity_pairs must be a list")
        if truth.get("pending_ingestion"):
            require(truth["entity_pairs"] == [], "pending entity_resolution_truth must not invent pairs")
        else:
            for pair in truth["entity_pairs"]:
                require(pair["label"] in {"same", "different"}, "bad entity_resolution_truth label")
                require(isinstance(pair["left_entity_id"], str) and isinstance(pair["right_entity_id"], str),
                        "bad entity_resolution_truth entity id")
        # A closed set of fields and authored text rejects injected real data/claims
        # and invented inference fields even if someone recomputes manifest hashes.
        entity_rows = load(directory / "metadata/entities.json")["entities"]
        ids = {row["entity_id"] for row in entity_rows}
        require(len(ids) == len(entity_rows), "duplicate entity ID")
        inventories[case_id] = ids
        minimums = {"PER": 10 if code == "NF" else 8, "VEH": 6 if code == "NF" else 5, "PHONE": 6, "ACC": 5, "LOC": 3}
        for kind, minimum in minimums.items():
            require(sum(row["entity_type"] == kind for row in entity_rows) >= minimum, f"insufficient {kind} entities in {case_id}")
        documents = list((directory / "documents").glob("*.pdf"))
        require(len(documents) >= 2 and len(list((directory / "documents").glob("*.txt"))) >= 2, "insufficient documents")
        for pdf in documents:
            extracted = run(["pdftotext", str(pdf), "-"]).decode()
            require("WHOLLY FICTIONAL" in extracted and case_id in extracted, "PDF content/ownership missing")
        cdr = records(directory / "structured/cdr.csv")
        transactions = records(directory / "structured/transactions.csv")
        social = load(directory / "social/messages.json")["records"]
        require(len(cdr) >= 100 and len(transactions) >= 75 and len(social) >= 50, "record minimums not met")
        events = load(directory / "metadata/events.json")["events"]
        require(len(events) >= 5, "not enough timed events")
        event_map = {event["event_id"]: event for event in events}
        for event in events:
            require(set(event["entity_refs"]) <= ids, "foreign entity in event")
            require(datetime.fromisoformat(event["start"]) < datetime.fromisoformat(event["end"]), "invalid event time window")
        ref_fields = {"person_id", "source_id", "target_id", "vehicle_context", "location_id", "device_id", "from_account", "to_account", "sender", "vehicle_id"}
        for row in cdr + transactions + social:
            synthetic_fields(row, case_id)
            require(row["case_id"] == case_id, "foreign case in records")
            for key in ref_fields.intersection(row):
                if key == "source_id" and row[key].startswith("SYN-SOURCE-"):
                    continue
                require(row[key] in ids, f"foreign entity reference: {row[key]}")
            event = event_map[row["event_id"]]
            moment = datetime.fromisoformat(row["timestamp"])
            require(datetime.fromisoformat(event["start"]) <= moment <= datetime.fromisoformat(event["end"]), "record outside referenced event")

        if code == "NF":
            communities = load(directory / "metadata/communities.json")
            a_ids = set(communities["community_a"]["entity_ids"])
            b_ids = set(communities["community_b"]["entity_ids"])
            bridge = communities["bridge_entity_id"]
            bridge_variant = communities["bridge_alias_variant"]
            require(a_ids <= ids and b_ids <= ids, "Nightfall community roster references unknown entity")
            # The bridge is deliberately NOT a shared literal token (see
            # nightfall_bridge_content()'s own docstring): Community A's own
            # records use `bridge`, Community B's own records use the
            # near-miss `bridge_variant` -- an identical alias on both sides
            # would be Tier 1/2-blocked together before Tier 3 ever sees it
            # as ambiguous, never producing a reviewable candidate at all.
            require(a_ids.isdisjoint(b_ids), "Nightfall communities must be fully disjoint (bridge excluded)")
            require(bridge in a_ids and bridge_variant in b_ids,
                    "Nightfall bridge/bridge-variant not in their expected communities")
            require(bridge == f"SYN-PER-{code}-BRIDGE", "Nightfall bridge entity naming mismatch")
            require(bridge_variant == f"SYN-PER-{code}-BRIDGE-ALT",
                    "Nightfall bridge alias variant naming mismatch")

            motif = load(directory / "metadata/motif.json")
            require(motif["chain"] == FULCRUM_MOTIF_CHAIN, "wrong Nightfall motif chain")
            window_start = datetime.fromisoformat(motif["window"]["start"])
            window_end = datetime.fromisoformat(motif["window"]["end"])
            require(window_start < window_end, "invalid Nightfall motif window")
            motif_events = [event_map[event_id] for event_id in motif["event_ids"]]
            require([e["event_type"] for e in motif_events] == FULCRUM_MOTIF_CHAIN,
                    "Nightfall motif events out of chain order")
            motif_starts = [datetime.fromisoformat(e["start"]) for e in motif_events]
            motif_ends = [datetime.fromisoformat(e["end"]) for e in motif_events]
            require(all(window_start <= s < e <= window_end for s, e in zip(motif_starts, motif_ends)),
                    "Nightfall motif event lands outside its one documented time window")
            require(motif_starts == sorted(motif_starts), "Nightfall motif events are not in chronological order")

            sightings_nf = load(directory / "structured/sightings.json")["records"]
            contradiction = load(directory / "structured/contradictions.json")
            require(contradiction["disposition"] == "review_required", "unsafe Nightfall contradiction disposition")
            claim, conflict = contradiction["claim"], contradiction["conflicting_evidence"]
            require(claim["entity_id"] == conflict["entity_id"], "Nightfall contradiction does not share a common entity")
            require(claim["location_id"] != conflict["location_id"], "Nightfall contradiction locations must differ")
            require(claim["timestamp"] == conflict["timestamp"], "Nightfall contradiction must overlap in time")
            require({claim["entity_id"], claim["location_id"], conflict["location_id"]} <= ids,
                    "Nightfall contradiction references an unknown entity")
            require(claim["record_id"] in {r["record_id"] for r in social}, "Nightfall contradiction claim record missing")
            require(conflict["record_id"] in {r["record_id"] for r in sightings_nf},
                    "Nightfall contradiction evidence record missing")

        require(len(list((directory / "visual").glob("*.png"))) >= 2, "missing stills")
        audio_duration = verify_audio(directory, case_id)
        video_duration = verify_visual(directory, case_id)
        # First vehicle, person, device, account and location recur across real files.
        text_by_modality = {modality: "\n".join(p.read_text() for p in (directory / modality).rglob("*")
                            if p.is_file() and p.suffix in {".txt", ".json", ".csv"})
                            for modality in ("documents", "structured", "social", "audio", "visual")}
        for kind in ("PER", "PHONE", "VEH", "LOC"):
            token = entities_for(case_id)[kind][0]
            require(all(token in text for text in text_by_modality.values()), f"missing cross-modality entity thread: {token}")
        account = entities_for(case_id)["ACC"][0]
        require(all(account in text_by_modality[m] for m in ("documents", "structured", "audio")), "account linkage missing")
        if code in {"NF", "COP"}:
            require(all(SHARED in text for text in text_by_modality.values()), "shared vehicle absent from required modalities")
        else:
            require(all(SHARED not in text for text in text_by_modality.values()), "shared vehicle leaked into Echo")

        register = load(directory / "metadata/evidence-register.json")
        registered = register["files"]
        owned = {entry["path"] for entry in case_entries}
        require(len(registered) == len(owned) and {r["path"] for r in registered} == owned, "evidence register coverage")
        require(register["hash_authority"] == MANIFEST, "register hash authority mismatch")
        for item, entry in zip(sorted(registered, key=lambda e: e["path"]), sorted(case_entries, key=lambda e: e["path"]), strict=True):
            require(item["case_id"] == case_id and item["synthetic"] is True, "register case/synthetic status")
            require(item["modality"] == entry["modality"] and item["content_type"] == entry["content_type"], "register type mismatch")
            require(item["expected_result_path"] == entry["expected_result_path"], "register expected result link")
        expected = load(root / f"expected-results/{case_id}.json")
        require(expected["record_type"] == "human_authored_acceptance_assertion", "expected result is not an authored assertion")
        require("not worker or model output" in expected["assertion_origin"], "expected result origin")
        require(all(expected.get(key) is value for key, value in SAFE.items()), "unsafe automatic conclusion")
        require(expected["artifact_expectations"] == [expectation(case_id, path) for path in sorted(owned)], "missing/unsafe artifact expectation")
        require(expected["speaker_roles_case_scoped"] is True and expected["cross_case_visibility"] == "not_automatically_visible", "isolation defaults")
        if code in {"NF", "COP"}:
            rule = expected["isolation_rule"]
            require(rule["shared_identifier"] == SHARED and rule["nightfall_investigators_can_automatically_see_copper"] is False, "Copper exposure allowed")
            require(set(rule["blocked_scopes"]) == {"records", "entities", "observations", "graph_nodes", "case_details"}, "incomplete Copper isolation rule")
        else:
            require(expected["expected_outcome"] in {"review_required", "rejected"}, "unsafe Echo outcome")
            candidate = expected["candidate_connection"]
            require(candidate["status"] in {"review_required", "rejected"}, "unsafe Echo candidate")
            require(candidate["automatic_identity_merge"] is False and candidate["automatic_cross_case_linking"] is False, "Echo automatic linking allowed")
        if not quiet:
            print(f"verified {case_id}: {len(ids)} entities, {len(events)} events, {len(cdr)} calls, {len(transactions)} transactions, {len(social)} messages; audio={audio_duration}s, video={video_duration}s; {len(case_entries)} artifacts")
    nf, copper, echo = (inventories[case] for case in CASES)
    require(nf & copper == {SHARED} and not nf & echo and not copper & echo, "undeclared cross-case entity sharing")
    if not quiet:
        print(f"PASS: {len(entries)} artifacts + manifest, {total} bytes; hashes, authored assertions, formats, graph references, and isolation verified")
    return len(entries), total


def verify_v2(root, quiet=False):
    """Development/validation graph-truth corpus: referential integrity, community/bridge
    disjointness, and the motif's one documented time window."""
    root = root.resolve()
    manifest = load(checked_path(root, MANIFEST_V2))
    require(manifest["schema_version"] == "operation-nightfall.v2" and manifest["corpus_id"] == SYN_CORPUS_FULCRUM,
            "wrong v2 corpus schema")
    require(manifest["case_ids"] == list(FULCRUM_CASES), "wrong v2 corpus cases")
    require(all(manifest.get(key) is value for key, value in SAFE.items()), "unsafe v2 manifest assertions")
    entries = manifest["files"]
    require(entries and len({e["path"] for e in entries}) == len(entries), "empty/duplicate v2 manifest paths")
    paths = {e["path"] for e in entries}
    actual = set()
    for path in (root / "operation-fulcrum").rglob("*"):
        checked_path(root, path.relative_to(root).as_posix())
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    for case_id in FULCRUM_CASES:
        expected_path = root / f"expected-results/{case_id}.json"
        if expected_path.is_file():
            actual.add(f"expected-results/{case_id}.json")
    require(actual == paths, f"v2 manifest coverage mismatch: {sorted(actual ^ paths)}")

    total = 0
    per_case = {case: [] for case in FULCRUM_CASES}
    for entry in entries:
        case = entry["case_id"]
        require(case in FULCRUM_CASES, "unexpected v2 manifest case")
        relative = entry["path"]
        require(relative.startswith(f"operation-fulcrum/{case}/") or relative == f"expected-results/{case}.json",
                "v2 artifact assigned to wrong case")
        path = checked_path(root, relative)
        require(path.is_file(), f"missing v2 artifact: {relative}")
        data = path.read_bytes()
        require(digest(data) == entry["sha256"] and len(data) == entry["bytes"], f"v2 hash/size mismatch: {relative}")
        require(entry["content_type"] == content_type(relative), "wrong v2 content type")
        require(entry["generated_synthetic_data"] is True, "missing v2 synthetic marker")
        require(entry["expected_result_path"] == f"expected-results/{case}.json", "wrong v2 expected result link")
        if path.suffix == ".json" and Path(relative).name != "entity_resolution_truth.json":
            # entity_resolution_truth.json legitimately holds real Entity.entity_id UUIDs
            # (ADR-025), not SYN-* placeholders; it has its own dedicated check below.
            synthetic_fields(load(path), case)
        total += len(data)
        per_case[case].append(entry)
    require(total < 100_000_000, f"v2 corpus exceeds 100 MB: {total}")

    ref_fields = {"person_id", "source_id", "target_id", "vehicle_context", "location_id", "device_id",
                  "from_account", "to_account", "sender", "vehicle_id"}
    inventories = {}
    for case_id, code in FULCRUM_CASES.items():
        directory = root / "operation-fulcrum" / case_id
        files, meta = fulcrum_authored_files(case_id, FULCRUM_SEEDS[case_id])
        for relative, expected_bytes in files.items():
            if relative.endswith("entity_resolution_truth.json"):
                # This file's own authored placeholder documents that it is meant to be
                # overwritten later by generate_entity_resolution_truth.py (ADR-025); its
                # post-regeneration content is validated separately below, not against the
                # frozen placeholder template.
                continue
            require((directory / relative).read_bytes() == expected_bytes,
                    f"v2 content deviates from closed template: {case_id}/{relative}")

        entity_rows = load(directory / "metadata/entities.json")["entities"]
        ids = {row["entity_id"] for row in entity_rows}
        require(len(ids) == len(entity_rows), "duplicate v2 entity ID")
        require(SHARED not in ids, "Nightfall/Copper shared vehicle token leaked into Fulcrum entities")
        inventories[case_id] = ids

        cdr = records(directory / "structured/cdr.csv")
        transactions = records(directory / "structured/transactions.csv")
        sightings = load(directory / "structured/sightings.json")["records"]
        social = load(directory / "social/messages.json")["records"]
        require(len(cdr) >= 10 and len(transactions) >= 8 and len(sightings) >= 6 and len(social) >= 8,
                "v2 record minimums not met")
        events = load(directory / "metadata/events.json")["events"]
        event_map = {event["event_id"]: event for event in events}
        for event in events:
            require(set(event["entity_refs"]) <= ids, "foreign entity in v2 event")
            require(datetime.fromisoformat(event["start"]) < datetime.fromisoformat(event["end"]),
                    "invalid v2 event time window")
        for row in cdr + transactions + sightings + social:
            synthetic_fields(row, case_id)
            require(row["case_id"] == case_id, "foreign case in v2 records")
            for key in ref_fields.intersection(row):
                if key == "source_id" and str(row[key]).startswith("SYN-SOURCE-"):
                    continue
                require(row[key] in ids, f"foreign v2 entity reference: {row[key]}")
            if "event_id" in row:
                event = event_map[row["event_id"]]
                moment = datetime.fromisoformat(row["timestamp"])
                require(datetime.fromisoformat(event["start"]) <= moment <= datetime.fromisoformat(event["end"]),
                        "v2 record outside referenced event")

        communities = load(directory / "metadata/communities.json")
        a_ids, b_ids = set(communities["community_a"]["entity_ids"]), set(communities["community_b"]["entity_ids"])
        bridge = communities["bridge_entity_id"]
        require(a_ids <= ids and b_ids <= ids, "community roster references unknown entity")
        require(a_ids & b_ids == {bridge}, "communities are not disjoint except at the bridge")
        require(bridge == meta["bridge_id"], "bridge entity mismatch between communities.json and template")

        motif = load(directory / "metadata/motif.json")
        require(motif["chain"] == FULCRUM_MOTIF_CHAIN, "wrong motif chain")
        window_start = datetime.fromisoformat(motif["window"]["start"])
        window_end = datetime.fromisoformat(motif["window"]["end"])
        require(window_start < window_end, "invalid motif window")
        motif_events = [event_map[event_id] for event_id in motif["event_ids"]]
        require([e["event_type"] for e in motif_events] == FULCRUM_MOTIF_CHAIN, "motif events out of chain order")
        motif_starts = [datetime.fromisoformat(e["start"]) for e in motif_events]
        motif_ends = [datetime.fromisoformat(e["end"]) for e in motif_events]
        require(all(window_start <= s < e <= window_end for s, e in zip(motif_starts, motif_ends)),
                "motif event lands outside its one documented time window")
        require(motif_starts == sorted(motif_starts), "motif events are not in chronological order")

        contradiction = load(directory / "structured/contradictions.json")
        require(contradiction["disposition"] == "review_required", "unsafe v2 contradiction disposition")
        claim, conflict = contradiction["claim"], contradiction["conflicting_evidence"]
        require(claim["entity_id"] == conflict["entity_id"], "contradiction does not share a common entity")
        require(claim["location_id"] != conflict["location_id"], "contradiction locations must differ")
        require(claim["timestamp"] == conflict["timestamp"], "contradiction must overlap in time")
        require({claim["entity_id"], claim["location_id"], conflict["location_id"]} <= ids,
                "contradiction references an unknown entity")
        require(claim["record_id"] in {r["record_id"] for r in social}, "contradiction claim record missing")
        require(conflict["record_id"] in {r["record_id"] for r in sightings}, "contradiction evidence record missing")

        truth = load(directory / "entity_resolution_truth.json")
        require(truth["schema_version"] == "entity_resolution_truth.v1", "wrong entity_resolution_truth schema")
        require(truth["case_id"] == case_id, "entity_resolution_truth case mismatch")
        require(isinstance(truth["entity_pairs"], list), "entity_resolution_truth.entity_pairs must be a list")
        if truth.get("pending_ingestion"):
            require(truth["entity_pairs"] == [], "pending entity_resolution_truth must not invent pairs")
        else:
            for pair in truth["entity_pairs"]:
                require(pair["label"] in {"same", "different"}, "bad entity_resolution_truth label")
                require(isinstance(pair["left_entity_id"], str) and isinstance(pair["right_entity_id"], str),
                        "bad entity_resolution_truth entity id")

        text_blob = "\n".join(p.read_text() for p in directory.rglob("*")
                              if p.is_file() and p.suffix in {".txt", ".json", ".csv"})
        require(SHARED not in text_blob, "shared Nightfall/Copper vehicle token leaked into Fulcrum")

        register = load(directory / "metadata/evidence-register.json")
        owned = {entry["path"] for entry in per_case[case_id]}
        registered_register, registered_expected = fulcrum_provenance_records(
            case_id, owned, meta["bridge_id"], meta["motif_id"], meta["contradiction_id"])
        require(register == registered_register, "v2 register differs from authored provenance assertions")
        expected = load(root / f"expected-results/{case_id}.json")
        require(expected == registered_expected, "v2 expected result differs from authored safe assertions")

        if not quiet:
            print(f"verified {case_id}: {len(ids)} entities across 2 clusters, bridge={bridge}, "
                  f"motif={motif['motif_id']}, contradiction={contradiction['contradiction_id']}, "
                  f"{len(events)} events, {len(cdr)} calls, {len(transactions)} transactions, "
                  f"{len(sightings)} sightings, {len(social)} messages")

    dev, val = (inventories[case] for case in FULCRUM_CASES)
    require(not dev & val, "undeclared entity sharing between Fulcrum dev and validation splits")
    if not quiet:
        print(f"PASS v2: {len(entries)} artifacts + manifest, {total} bytes; graph truth "
              "(communities/bridge/motif/contradiction) verified")
    return len(entries), total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    verify(Path(args.root))
    verify_v2(Path(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
