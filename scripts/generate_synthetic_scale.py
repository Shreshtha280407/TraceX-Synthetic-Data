#!/usr/bin/env python3
"""Generate large deterministic, fictional structured inputs outside Git."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_SEED = 8242026
DATA_FILES = (
    "data/entities.csv",
    "data/observations.jsonl",
    "data/cdr.csv",
    "data/transactions.csv",
    "data/social.jsonl",
    "data/vehicle_sightings.csv",
    "data/intelligence.txt",
    "metadata.json",
    "manifest.json",
)


def json_dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def distribute(total: int, cases: int) -> list[int]:
    base, extra = divmod(total, cases)
    return [base + (1 if index < extra else 0) for index in range(cases)]


def timestamp(index: int, seed: int) -> str:
    base = datetime(2035, 1, 1, tzinfo=timezone.utc)
    value = base + timedelta(seconds=(index * 37 + seed) % 31_536_000)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_profile(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        profile = json.load(handle)
    required = {"entities", "observations", "cdr", "transactions", "social", "vehicle_sightings", "intelligence_text"}
    if not isinstance(profile.get("counts"), dict) or set(profile["counts"]) != required:
        raise ValueError("profile counts must contain exactly the supported record types")
    if int(profile.get("case_count", 0)) <= 0:
        raise ValueError("profile case_count must be positive")
    if any(int(value) < 0 for value in profile["counts"].values()):
        raise ValueError("profile record counts must be non-negative")
    return profile


def safe_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if root in {Path(root.anchor), Path.home(), Path.cwd().resolve()}:
        raise ValueError(f"unsafe scale output root: {root}")
    return root


def preflight(root: Path, force: bool) -> None:
    existing = [relative for relative in DATA_FILES if (root / relative).exists()]
    if existing and not force:
        raise FileExistsError(f"refusing to overwrite {len(existing)} existing managed output(s); use --force")
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)


def generate(profile: dict[str, object], profile_path: Path, root: Path, seed: int) -> dict[str, object]:
    rng = random.Random(seed)
    case_count = int(profile["case_count"])
    counts = {key: int(value) for key, value in profile["counts"].items()}
    case_ids = [f"case-synthetic-scale-{index:03d}" for index in range(1, case_count + 1)]
    allocations = {name: distribute(total, case_count) for name, total in counts.items()}
    entity_by_case: dict[str, dict[str, list[str]]] = {}
    row_counts: dict[str, int] = {}

    entities_path = root / "data/entities.csv"
    with entities_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["case_id", "entity_id", "entity_type", "synthetic_label", "fictional"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for case_index, case_id in enumerate(case_ids, start=1):
            buckets = {key: [] for key in ("PER", "PHONE", "VEH", "ACC", "LOC")}
            for local_index in range(allocations["entities"][case_index - 1]):
                kind = ("PER", "PHONE", "VEH", "ACC", "LOC")[local_index % 5]
                entity_id = f"SYN-{kind}-C{case_index:03d}-{local_index + 1:06d}"
                buckets[kind].append(entity_id)
                writer.writerow({
                    "case_id": case_id, "entity_id": entity_id, "entity_type": kind,
                    "synthetic_label": f"SYNTHETIC-{kind}-{case_index:03d}-{local_index + 1:06d}",
                    "fictional": "true",
                })
            entity_by_case[case_id] = buckets
    row_counts["data/entities.csv"] = counts["entities"]

    observations_path = root / "data/observations.jsonl"
    with observations_path.open("w", encoding="utf-8", newline="\n") as handle:
        global_index = 0
        for case_index, case_id in enumerate(case_ids, start=1):
            all_entities = sum(entity_by_case[case_id].values(), [])
            for local_index in range(allocations["observations"][case_index - 1]):
                record = {
                    "case_id": case_id,
                    "observation_id": f"SYN-OBS-C{case_index:03d}-{local_index + 1:07d}",
                    "entity_id": all_entities[(local_index * 7 + seed) % len(all_entities)],
                    "source_id": f"SYN-SOURCE-C{case_index:03d}-{local_index % 11 + 1:03d}",
                    "timestamp": timestamp(global_index, seed),
                    "assertion": "synthetic observation for scale mechanics only; no real-world claim",
                }
                handle.write(json_dump(record) + "\n")
                global_index += 1
    row_counts["data/observations.jsonl"] = counts["observations"]

    cdr_path = root / "data/cdr.csv"
    with cdr_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["case_id", "record_id", "source_id", "target_id", "timestamp", "duration_seconds"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        global_index = 0
        for case_index, case_id in enumerate(case_ids, start=1):
            phones = entity_by_case[case_id]["PHONE"]
            for local_index in range(allocations["cdr"][case_index - 1]):
                writer.writerow({
                    "case_id": case_id, "record_id": f"SYN-CDR-C{case_index:03d}-{local_index + 1:07d}",
                    "source_id": phones[local_index % len(phones)],
                    "target_id": phones[(local_index * 3 + 1) % len(phones)],
                    "timestamp": timestamp(global_index, seed), "duration_seconds": 1 + rng.randrange(300),
                })
                global_index += 1
    row_counts["data/cdr.csv"] = counts["cdr"]

    transactions_path = root / "data/transactions.csv"
    with transactions_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["case_id", "record_id", "from_account", "to_account", "timestamp", "amount", "currency"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        global_index = 0
        for case_index, case_id in enumerate(case_ids, start=1):
            accounts = entity_by_case[case_id]["ACC"]
            for local_index in range(allocations["transactions"][case_index - 1]):
                writer.writerow({
                    "case_id": case_id, "record_id": f"SYN-TXN-C{case_index:03d}-{local_index + 1:07d}",
                    "from_account": accounts[local_index % len(accounts)],
                    "to_account": accounts[(local_index * 5 + 1) % len(accounts)],
                    "timestamp": timestamp(global_index, seed),
                    "amount": f"{1 + rng.randrange(100000) / 100:.2f}", "currency": "SYN",
                })
                global_index += 1
    row_counts["data/transactions.csv"] = counts["transactions"]

    social_path = root / "data/social.jsonl"
    with social_path.open("w", encoding="utf-8", newline="\n") as handle:
        global_index = 0
        for case_index, case_id in enumerate(case_ids, start=1):
            people = entity_by_case[case_id]["PER"]
            for local_index in range(allocations["social"][case_index - 1]):
                record = {
                    "case_id": case_id, "record_id": f"SYN-SOC-C{case_index:03d}-{local_index + 1:07d}",
                    "author_id": people[local_index % len(people)], "timestamp": timestamp(global_index, seed),
                    "source_id": f"SYN-SOCSRC-C{case_index:03d}-{local_index % 7 + 1:03d}",
                    "message": f"Synthetic chat token C{case_index:03d}-{local_index + 1:07d}; no real-person or guilt claim.",
                }
                handle.write(json_dump(record) + "\n")
                global_index += 1
    row_counts["data/social.jsonl"] = counts["social"]

    fixture_count = int(profile.get("cross_case_isolation_fixture_count", 0))
    if fixture_count > max(0, case_count - 1):
        raise ValueError("too many cross-case fixtures for case count")
    fixtures = []
    for index in range(fixture_count):
        fixtures.append({
            "identifier": f"SYN-VEH-ISO-{index + 1:04d}",
            "case_ids": [case_ids[index], case_ids[index + 1]],
            "meaning": "same-looking isolation fixture only; not a confirmed cross-case link",
            "automatic_cross_case_visibility": False,
        })
    fixture_lookup: dict[tuple[str, int], str] = {}
    for fixture_index, fixture in enumerate(fixtures):
        for fixture_case in fixture["case_ids"]:
            fixture_lookup[(fixture_case, fixture_index)] = fixture["identifier"]

    sightings_path = root / "data/vehicle_sightings.csv"
    with sightings_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["case_id", "record_id", "vehicle_id", "location_id", "timestamp", "fixture_status"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        global_index = 0
        for case_index, case_id in enumerate(case_ids, start=1):
            vehicles = entity_by_case[case_id]["VEH"]
            locations = entity_by_case[case_id]["LOC"]
            for local_index in range(allocations["vehicle_sightings"][case_index - 1]):
                fixture_id = fixture_lookup.get((case_id, local_index))
                writer.writerow({
                    "case_id": case_id, "record_id": f"SYN-SIGHT-C{case_index:03d}-{local_index + 1:07d}",
                    "vehicle_id": fixture_id or vehicles[local_index % len(vehicles)],
                    "location_id": locations[(local_index * 3) % len(locations)],
                    "timestamp": timestamp(global_index, seed),
                    "fixture_status": "cross_case_isolation_only_not_confirmed_link" if fixture_id else "case_local_synthetic",
                })
                global_index += 1
    row_counts["data/vehicle_sightings.csv"] = counts["vehicle_sightings"]

    intelligence_path = root / "data/intelligence.txt"
    with intelligence_path.open("w", encoding="utf-8", newline="\n") as handle:
        for case_index, case_id in enumerate(case_ids, start=1):
            for local_index in range(allocations["intelligence_text"][case_index - 1]):
                record_id = f"SYN-INTEL-C{case_index:03d}-{local_index + 1:06d}"
                text = f"Fictional acceptance note {record_id}; synthetic context only; no real person, event, wrongdoing, or guilt conclusion."
                handle.write(f"{case_id}|{record_id}|{text}\n")
    row_counts["data/intelligence.txt"] = counts["intelligence_text"]

    metadata = {
        "dataset_id": profile["profile_id"], "fictional": True, "generated_synthetic_data": True,
        "purpose": "controlled TraceX Phase 8 scale and graph testing only",
        "not_training_data": True, "not_a_benchmark": True, "not_evidence": True,
        "seed": seed, "case_ids": case_ids, "cross_case_isolation_fixtures": fixtures,
        "automatic_identity_merge": False, "automated_result_proves_guilt": False,
    }
    metadata_path = root / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    row_counts["metadata.json"] = 1

    manifest_files = []
    for relative, rows in sorted(row_counts.items()):
        path = root / relative
        manifest_files.append({
            "path": relative, "sha256": sha256(path), "bytes": path.stat().st_size,
            "row_count": rows, "case_count": case_count if relative.startswith("data/") else 0,
        })
    manifest = {
        "schema_version": 1, "dataset_id": profile["profile_id"],
        "description": "Fully fictional deterministic scale input; not evidence, training data, or a benchmark.",
        "seed": seed, "case_count": case_count, "case_ids": case_ids,
        "expected_counts": counts, "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        "files": manifest_files, "cross_case_isolation_fixtures": fixtures,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="scale-profiles/synthetic-scale-v1.json")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    profile_path = Path(args.profile).expanduser().resolve()
    profile = load_profile(profile_path)
    seed = int(args.seed if args.seed is not None else profile.get("default_seed", DEFAULT_SEED))
    root = safe_root(args.output_root)
    try:
        preflight(root, args.force)
    except FileExistsError as error:
        print(str(error))
        return 2
    manifest = generate(profile, profile_path, root, seed)
    total_bytes = sum(item["bytes"] for item in manifest["files"]) + (root / "manifest.json").stat().st_size
    print(f"generated {manifest['dataset_id']} at {root}: {manifest['case_count']} cases, {sum(manifest['expected_counts'].values())} records, {total_bytes} bytes, seed {seed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
