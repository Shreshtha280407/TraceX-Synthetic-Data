#!/usr/bin/env python3
"""Verify deterministic synthetic scale output, counts, hashes, and isolation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

SYN_ID = re.compile(r"^SYN-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
CASE_ID = re.compile(r"^case-synthetic-scale-\d{3}$")
FORBIDDEN_CLAIMS = ("is guilty", "committed a crime", "confirmed criminal", "proves guilt")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def synthetic(identifier: str, context: str) -> None:
    if not SYN_ID.fullmatch(identifier):
        raise ValueError(f"non-synthetic identifier in {context}: {identifier!r}")


def validate_case_local(identifier: str, case_id: str, fixtures: set[str], context: str) -> None:
    synthetic(identifier, context)
    if identifier in fixtures:
        return
    match = re.search(r"-C(\d{3})-", identifier)
    if match and case_id != f"case-synthetic-scale-{match.group(1)}":
        raise ValueError(f"cross-case identifier leakage in {context}: {identifier} under {case_id}")


def verify(root: Path) -> tuple[dict[str, int], int, dict[str, str]]:
    with (root / "manifest.json").open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    expected = {key: int(value) for key, value in manifest["expected_counts"].items()}
    profile_path = Path(__file__).resolve().parents[1] / "scale-profiles" / f"{manifest['dataset_id']}.json"
    with profile_path.open("r", encoding="utf-8") as handle:
        profile = json.load(handle)
    if hashlib.sha256(profile_path.read_bytes()).hexdigest() != manifest.get("profile_sha256"):
        raise ValueError("manifest does not match the selected checked-in profile hash")
    profile_counts = {key: int(value) for key, value in profile["counts"].items()}
    if expected != profile_counts:
        raise ValueError("manifest expected counts do not match the selected profile")
    if int(manifest["case_count"]) != int(profile["case_count"]):
        raise ValueError("manifest case count does not match the selected profile")
    cases = set(manifest["case_ids"])
    if len(cases) != int(manifest["case_count"]):
        raise ValueError("manifest case count does not match unique case IDs")
    if any(not CASE_ID.fullmatch(case) for case in cases):
        raise ValueError("manifest contains an invalid case ID")
    fixtures = manifest.get("cross_case_isolation_fixtures", [])
    fixture_ids = {item["identifier"] for item in fixtures}
    for item in fixtures:
        synthetic(item["identifier"], "fixture manifest")
        if set(item["case_ids"]) - cases:
            raise ValueError("fixture references an unknown case")
        if item.get("automatic_cross_case_visibility") is not False:
            raise ValueError("fixture allows automatic cross-case visibility")
        if "not a confirmed cross-case link" not in item.get("meaning", ""):
            raise ValueError("fixture does not deny a confirmed cross-case link")

    hashes: dict[str, str] = {}
    total_bytes = (root / "manifest.json").stat().st_size
    manifest_rows = {}
    for item in manifest["files"]:
        relative = item["path"]
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"unsafe manifest path: {relative}")
        path = root / rel
        actual = sha256(path)
        if actual != item["sha256"] or path.stat().st_size != item["bytes"]:
            raise ValueError(f"hash/size mismatch: {relative}")
        hashes[relative] = actual
        manifest_rows[relative] = int(item["row_count"])
        expected_case_count = int(manifest["case_count"]) if relative.startswith("data/") else 0
        if int(item["case_count"]) != expected_case_count:
            raise ValueError(f"manifest file case count mismatch: {relative}")
        total_bytes += path.stat().st_size

    for relative in hashes:
        raw_text = (root / relative).read_bytes().decode("utf-8", errors="ignore").lower()
        if any(claim in raw_text for claim in FORBIDDEN_CLAIMS):
            raise ValueError(f"real-person or guilt conclusion found in {relative}")

    actual_counts: dict[str, int] = {}
    entities_by_case: dict[str, set[str]] = {case: set() for case in cases}
    with (root / "data/entities.csv").open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        count = 0
        for row in reader:
            case_id = row["case_id"]
            if case_id not in cases or row["fictional"] != "true":
                raise ValueError("invalid entity case or fictional marker")
            validate_case_local(row["entity_id"], case_id, fixture_ids, "entities.csv")
            entities_by_case[case_id].add(row["entity_id"])
            count += 1
    actual_counts["entities"] = count

    with (root / "data/observations.jsonl").open("r", encoding="utf-8") as handle:
        count = 0
        for line in handle:
            row = json.loads(line)
            case_id = row["case_id"]
            if case_id not in cases or row["entity_id"] not in entities_by_case[case_id]:
                raise ValueError("observation violates case-local entity scope")
            validate_case_local(row["observation_id"], case_id, fixture_ids, "observations.jsonl")
            validate_case_local(row["source_id"], case_id, fixture_ids, "observations.jsonl")
            count += 1
    actual_counts["observations"] = count

    csv_specs = {
        "cdr": ("data/cdr.csv", ("record_id", "source_id", "target_id"), ("source_id", "target_id")),
        "transactions": ("data/transactions.csv", ("record_id", "from_account", "to_account"), ("from_account", "to_account")),
        "vehicle_sightings": ("data/vehicle_sightings.csv", ("record_id", "vehicle_id", "location_id"), ("vehicle_id", "location_id")),
    }
    fixture_cases: dict[str, set[str]] = {identifier: set() for identifier in fixture_ids}
    for name, (relative, id_fields, entity_fields) in csv_specs.items():
        with (root / relative).open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            count = 0
            for row in reader:
                case_id = row["case_id"]
                if case_id not in cases:
                    raise ValueError(f"unknown case in {relative}")
                for field in id_fields:
                    validate_case_local(row[field], case_id, fixture_ids, relative)
                for field in entity_fields:
                    value = row[field]
                    if value in fixture_ids:
                        fixture_cases[value].add(case_id)
                    elif value not in entities_by_case[case_id]:
                        raise ValueError(f"non-local entity in {relative}: {value}")
                count += 1
        actual_counts[name] = count

    with (root / "data/social.jsonl").open("r", encoding="utf-8") as handle:
        count = 0
        for line in handle:
            row = json.loads(line)
            case_id = row["case_id"]
            if case_id not in cases or row["author_id"] not in entities_by_case[case_id]:
                raise ValueError("social record violates case isolation")
            validate_case_local(row["record_id"], case_id, fixture_ids, "social.jsonl")
            validate_case_local(row["source_id"], case_id, fixture_ids, "social.jsonl")
            count += 1
    actual_counts["social"] = count

    with (root / "data/intelligence.txt").open("r", encoding="utf-8") as handle:
        count = 0
        for line in handle:
            case_id, record_id, text = line.rstrip("\n").split("|", 2)
            if case_id not in cases:
                raise ValueError("unknown intelligence case")
            validate_case_local(record_id, case_id, fixture_ids, "intelligence.txt")
            lowered = text.lower()
            if any(claim in lowered for claim in FORBIDDEN_CLAIMS):
                raise ValueError("real-person/guilt conclusion in intelligence text")
            if "no real person" not in lowered or "guilt conclusion" not in lowered:
                raise ValueError("intelligence disclaimer missing")
            count += 1
    actual_counts["intelligence_text"] = count

    if actual_counts != expected:
        raise ValueError(f"profile count mismatch: expected {expected}, got {actual_counts}")
    row_mapping = {
        "data/entities.csv": "entities",
        "data/observations.jsonl": "observations",
        "data/cdr.csv": "cdr",
        "data/transactions.csv": "transactions",
        "data/social.jsonl": "social",
        "data/vehicle_sightings.csv": "vehicle_sightings",
        "data/intelligence.txt": "intelligence_text",
    }
    for path, key in row_mapping.items():
        if manifest_rows.get(path) != actual_counts[key]:
            raise ValueError(f"manifest row count mismatch: {path}")
    expected_fixture_cases = {item["identifier"]: set(item["case_ids"]) for item in fixtures}
    if fixture_cases != expected_fixture_cases:
        raise ValueError(f"cross-case fixture occurrences do not match declarations: {fixture_cases}")
    with (root / "metadata.json").open("r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    if metadata.get("fictional") is not True:
        raise ValueError("scale metadata is not explicitly fictional")
    if metadata.get("automatic_identity_merge") is not False:
        raise ValueError("scale metadata allows automatic identity merge")
    if metadata.get("automated_result_proves_guilt") is not False:
        raise ValueError("scale metadata allows guilt conclusion")
    if metadata.get("seed") != manifest.get("seed") or set(metadata.get("case_ids", [])) != cases:
        raise ValueError("scale metadata seed/cases do not match the manifest")
    return actual_counts, total_bytes, hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    counts, total_bytes, hashes = verify(root)
    print(f"verified {len(counts)} record groups, {sum(counts.values())} records, {total_bytes} bytes")
    print("counts=" + json.dumps(counts, sort_keys=True))
    for path, digest in sorted(hashes.items()):
        print(f"sha256 {digest}  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
