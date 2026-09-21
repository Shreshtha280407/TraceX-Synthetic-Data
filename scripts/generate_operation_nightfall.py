#!/usr/bin/env python3
"""Build the fictional multimodal acceptance corpus using offline tools only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_SEED = 8242026
CASES = {"case-operation-nightfall": "NF", "case-operation-copper": "COP", "case-operation-echo": "ECHO"}
SHARED = "SYN-VEH-NF-1001"
SPEAKERS = {"SYN-SPEAKER-ALPHA": "en-us+m3", "SYN-SPEAKER-BETA": "en-gb+f3", "SYN-SPEAKER-GAMMA": "en-sc+m7"}
NOTICE = "WHOLLY FICTIONAL SYNTHETIC ACCEPTANCE FIXTURE. Human review required. No real-world claim."
BANNER = "SYNTHETIC TEST FOOTAGE — NOT REAL SURVEILLANCE"
MANIFEST = "manifests/operation-nightfall.v1.json"
SAFE = {"automatic_identity_merge": False, "automatic_cross_case_linking": False, "automated_result_proves_guilt": False}


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def csv_data(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def run(command):
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def pdf_data(lines):
    """Small real PDF with a complete xref, printable ASCII, and wrapped pages."""
    lines = [part for line in lines for part in (textwrap.wrap(line, width=86) or [""])]
    pages = [lines[i:i + 48] for i in range(0, len(lines), 48)]
    kids = " ".join(f"{4 + 2*i} 0 R" for i in range(len(pages)))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"]
    for i, page in enumerate(pages):
        commands = ["BT /F1 9 Tf 40 750 Td 14 TL"]
        for line in page:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({escaped}) Tj T*")
        commands.append("ET")
        stream = "\n".join(commands).encode("ascii")
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {5+2*i} 0 R >>".encode(),
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        ])
    result = bytearray(b"%PDF-1.4\n%Synthetic\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)


def entities_for(case_id):
    code = CASES[case_id]
    sizes = {"PER": 10 if code == "NF" else 8, "VEH": 6 if code == "NF" else 5, "PHONE": 6, "ACC": 5, "LOC": 3}
    entities = {kind: [f"SYN-{kind}-{code}-{i:02d}" for i in range(1, count+1)] for kind, count in sizes.items()}
    if code in {"NF", "COP"}:
        entities["VEH"][0] = SHARED
    return entities


def time_at(case_id, seconds):
    month = list(CASES).index(case_id) + 1
    return (datetime(2031, month, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def envelope(case_id, **values):
    return {"case_id": case_id, "synthetic": True, "source": "offline_authored_fixture", **values}


def speech_plan(case_id):
    entities = entities_for(case_id)
    p, v, a, d, loc = (entities[k] for k in ("PER", "VEH", "ACC", "PHONE", "LOC"))
    # Exactly this harmless scripted text is sent to the offline synthesizer.
    lines = [
        f"Synthetic review. Please register vehicle {v[0]} with the case evidence list.",
        f"Person {p[0]} checks the fictional inventory. Keep every source available for review.",
        f"Device {d[0]} belongs in the synthetic call table. This is a handling exercise.",
        f"Account {a[0]} references a fictional transfer. Please retain its source metadata.",
        f"Vehicle {v[1]} appears on the second illustrated card. Preserve that timeline annotation.",
        f"Location {loc[0]} is a fictional token. Please check its event timestamp.",
        f"Vehicle {v[2]} appears on the final card. Keep the label available for review.",
        f"Person {p[1]} checks the notes. Any apparent relationship still requires human review.",
        "Close the fictional handling exercise. Preserve case boundaries and provenance. Never merge identities automatically.",
    ]
    if CASES[case_id] == "ECHO":
        lines[-1] = "The similar alias has conflicting locations. Keep the candidate under review. Never merge identities automatically."
    refs = [[v[0]], [p[0]], [d[0]], [a[0]], [v[1]], [loc[0]], [v[2]], [p[1]], [p[0], p[1]]]
    return [{"speaker_id": list(SPEAKERS)[i % 3], "text": text, "entity_refs": refs[i], "slot_start": i * 9}
            for i, text in enumerate(lines)]


def scene_plan(case_id):
    entities = entities_for(case_id)
    return [envelope(case_id, annotation_id=f"SYN-VIS-{CASES[case_id]}-{i+1:02d}",
                     start_seconds=i*9, end_seconds=(i+1)*9, timestamp=time_at(case_id, i*9),
                     vehicle_id=entities["VEH"][i], person_id=entities["PER"][i],
                     device_id=entities["PHONE"][i], location_id=entities["LOC"][i],
                     style=["sedan", "van", "truck"][i],
                     label_bbox_normalized=[320/1280, 400/720, 640/1280, 64/720],
                     bbox_format="x_y_width_height; label panel including padding",
                     still_path=f"visual/still-{i+1:02d}.png") for i in range(3)]


def authored_files(case_id, seed):
    """Closed vocabulary authored input templates, never worker/model outputs."""
    code = CASES[case_id]
    entities = entities_for(case_id)
    files = {}
    def add(path, data):
        files[path] = data if isinstance(data, bytes) else encoded(data)

    entity_rows = [dict(entity_id=identifier, entity_type=kind, synthetic=True,
                        case_id=case_id, identity_key=f"{case_id}:{identifier}")
                   for kind, values in entities.items() for identifier in values]
    add("metadata/entities.json", envelope(case_id, entities=entity_rows, speakers=[
        {"speaker_id": speaker, "scope": "case_local_audio_role", "voice": voice,
         "identity_key": f"{case_id}:{speaker}"} for speaker, voice in SPEAKERS.items()]))
    events = []
    for i in range(10):
        events.append(envelope(case_id, event_id=f"SYN-EVENT-{code}-{i+1:02d}",
                              start=time_at(case_id, i*900), end=time_at(case_id, i*900+899),
                              event_type=["call", "transfer", "sighting", "handling_meeting", "message"][i % 5],
                              entity_refs=[values[i % len(values)] for values in entities.values()],
                              disposition="review_required", claim="fictional evidence handling association only"))
    add("metadata/events.json", envelope(case_id, events=events, seed=seed))
    entity_lines = [f"{kind}: {', '.join(values)}" for kind, values in entities.items()]
    note = [NOTICE, f"Case {case_id}. Fictional evidence handling exercise.",
            "All relationships are case-local reviewable observations.", *entity_lines,
            "CDR, transactions, social, audio and illustrated scenes refer to this inventory.",
            "Entity identity is keyed by case ID plus synthetic identifier.",
            "No automated relationship is a guilt conclusion."]
    chronology = [NOTICE, f"Case {case_id}. Review chronology."]
    for event in events:
        chronology.extend([f"{event['event_id']} {event['start']} to {event['end']}",
                           f"{event['event_type']}: {', '.join(event['entity_refs'])}; review required."])
    if code == "COP":
        note.append("The shared vehicle token is solely an isolation test. Other cases remain invisible.")
    if code == "ECHO":
        note.extend(["Similar alias tokens refer to distinct persons at conflicting locations.",
                     "The candidate link must remain review_required or be rejected. Never merge."])
    for name, lines in (("briefing", note), ("chronology", chronology)):
        add(f"documents/{name}.txt", ("\n".join(lines) + "\n").encode())
        add(f"documents/{name}.pdf", pdf_data(lines))

    cdr, transactions, social = [], [], []
    for i in range(120):
        cdr.append(dict(case_id=case_id, record_id=f"SYN-CDR-{code}-{i+1:04d}",
                        person_id=entities["PER"][i % len(entities["PER"])],
                        source_id=entities["PHONE"][i % 6], target_id=entities["PHONE"][(i+1) % 6],
                        vehicle_context=entities["VEH"][i % len(entities["VEH"])],
                        location_id=entities["LOC"][i % 3], event_id=events[i % 10]["event_id"],
                        timestamp=time_at(case_id, (i % 10)*900+(i//10)*60), duration_seconds=12+(i+seed) % 30,
                        synthetic="true", source="offline_authored_fixture", disposition="review_required"))
    for i in range(90):
        transactions.append(dict(case_id=case_id, record_id=f"SYN-TXN-{code}-{i+1:04d}",
                                 person_id=entities["PER"][i % len(entities["PER"])],
                                 from_account=entities["ACC"][i % 5], to_account=entities["ACC"][(i+1) % 5],
                                 device_id=entities["PHONE"][i % 6], vehicle_context=entities["VEH"][i % len(entities["VEH"])],
                                 event_id=events[i % 10]["event_id"], timestamp=time_at(case_id, (i % 10)*900+(i//10)*60+5),
                                 amount=f"{10+(i+seed) % 80}.25", currency="SYN", synthetic="true",
                                 source="offline_authored_fixture", disposition="review_required"))
    for i in range(60):
        social.append(envelope(case_id, record_id=f"SYN-SOC-{code}-{i+1:04d}",
                               author_id=entities["PER"][i % len(entities["PER"])],
                               device_id=entities["PHONE"][i % 6], vehicle_id=entities["VEH"][i % len(entities["VEH"])],
                               location_id=entities["LOC"][i % 3], event_id=events[i % 10]["event_id"],
                               timestamp=time_at(case_id, (i % 10)*900+(i//10)*60+10), source_id=f"SYN-SOURCE-{code}-CHAT",
                               message=f"Fictional handling note for {entities['VEH'][i % len(entities['VEH'])]}; retain provenance and request human review."))
    add("structured/cdr.csv", csv_data(cdr))
    add("structured/transactions.csv", csv_data(transactions))
    add("social/messages.json", envelope(case_id, records=social))
    add("structured/cdr-metadata.json", envelope(case_id, vehicles=entities["VEH"],
        records_path="structured/cdr.csv", context="vehicle context is an authored case-local fixture, not a telecom inference"))
    sightings = [envelope(case_id, record_id=f"SYN-SIGHT-{code}-{i+1:03d}",
                         vehicle_id=entities["VEH"][i % len(entities["VEH"])],
                         person_id=entities["PER"][i % len(entities["PER"])],
                         location_id=entities["LOC"][i % 3], timestamp=time_at(case_id, i*9),
                         disposition="review_required") for i in range(18)]
    add("structured/sightings.json", envelope(case_id, records=sightings))
    add("visual/timeline.json", envelope(case_id, duration_seconds=27, width=1280, height=720,
        video_path="visual/footage.mp4", banner=BANNER, annotation_origin="authored scene schedule; not model detection output",
        model_detection_required=False, annotations=scene_plan(case_id)))
    if code == "ECHO":
        add("structured/candidate-links.json", envelope(case_id, **SAFE, candidates=[{
            "candidate_id": "SYN-LINK-ECHO-01", "left_id": entities["PER"][0], "right_id": entities["PER"][1],
            "left_alias": "SYN-ALIAS-ECHO-01O", "right_alias": "SYN-ALIAS-ECHO-010",
            "left_timestamp": time_at(case_id, 0), "right_timestamp": time_at(case_id, 0),
            "left_location": entities["LOC"][0], "right_location": entities["LOC"][2],
            "signal": "superficially similar O/0 alias text", "conflict": "distinct case-local persons, simultaneous incompatible location contexts",
            "disposition": "review_required", "automatic_merge_allowed": False,
        }]))
    return files


def make_audio(case_id, directory, work):
    rate, total_samples = 22050, 81 * 22050
    output = bytearray(total_samples*2)
    turns = []
    code = CASES[case_id]
    for i, item in enumerate(speech_plan(case_id)):
        wav = work / f"turn-{i}.wav"
        run(["espeak-ng", "-v", SPEAKERS[item["speaker_id"]], "-s", "205", "-w", str(wav), item["text"]])
        with wave.open(str(wav)) as handle:
            if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) != (1, 2, rate):
                raise ValueError("unexpected offline speech format")
            pcm = handle.readframes(handle.getnframes())
        # Remove only digital leading/trailing silence, then annotate exact sample boundaries.
        start, end = 0, len(pcm)
        while start < end and pcm[start:start+2] == b"\0\0":
            start += 2
        while end > start and pcm[end-2:end] == b"\0\0":
            end -= 2
        pcm = pcm[start:end]
        frames = len(pcm)//2
        if not 1 < frames/rate < 8.5:
            raise ValueError(f"speech turn {case_id}/{i} does not fit its 9-second slot: {frames/rate:.3f}s")
        first = item["slot_start"]*rate + rate//4
        output[first*2:first*2+len(pcm)] = pcm
        turns.append(envelope(case_id, segment_id=f"SYN-SEG-{code}-{i+1:02d}",
            speaker_id=item["speaker_id"], voice=SPEAKERS[item["speaker_id"]], text=item["text"], entity_refs=item["entity_refs"],
            start_sample=first, end_sample=first+frames, start_seconds=round(first/rate, 6), end_seconds=round((first+frames)/rate, 6)))
    audio = directory / "audio"
    audio.mkdir(parents=True)
    with wave.open(str(audio / "discussion.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(output))
    (audio / "turns.json").write_bytes(encoded(envelope(case_id, contains_speech=True,
        recording_id=f"SYN-AUD-{code}-01", duration_seconds=81, sample_rate=rate,
        transcript_origin="authored TTS script; not an ASR or diarization worker result",
        diarization_expectation="ingestion and reviewable speaker turns; perfect diarization is not required",
        speaker_identity_scope="case_local_audio_role; shared role names never imply shared person identity", turns=turns)))
    transcript = [NOTICE, f"Case {case_id}. Authored offline TTS script; not worker output."]
    rttm = []
    for turn in turns:
        transcript.append(f"[{turn['start_seconds']:.6f} - {turn['end_seconds']:.6f}] {turn['speaker_id']}: {turn['text']}")
        duration = (turn["end_sample"]-turn["start_sample"])/rate
        rttm.append(f"SPEAKER SYN-AUD-{code}-01 1 {turn['start_seconds']:.6f} {duration:.6f} <NA> <NA> {turn['speaker_id']} <NA> <NA>")
    (audio / "transcript.txt").write_text("\n".join(transcript)+"\n")
    (audio / "speakers.rttm").write_text("\n".join(rttm)+"\n")


def draw_filter(scene, font, banner_file):
    """Procedural shapes and locally installed text, no downloaded visual assets."""
    filters = [f"drawtext=fontfile='{font}':textfile='{banner_file}':fontcolor=white:fontsize=25:x=24:y=26",
               "drawbox=x=0:y=130:w=1280:h=500:color=0x25354a:t=fill",
               "drawbox=x=0:y=370:w=1280:h=8:color=0x8d9cb0:t=fill"]
    color = {"sedan": "0x4fbdc5", "van": "0xe5a34c", "truck": "0xb095de"}[scene["style"]]
    boxes = [(360, 265, 530, 95), (455, 205, 285, 75)]
    if scene["style"] == "van":
        boxes = [(350, 205, 560, 155), (390, 220, 110, 80)]
    if scene["style"] == "truck":
        boxes = [(350, 210, 350, 145), (710, 260, 195, 95), (745, 205, 130, 70)]
    for x, y, w, h in boxes:
        filters.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill")
    for x in (410, 800):
        filters.append(f"drawbox=x={x}:y=334:w=58:h=50:color=0x101825:t=fill")
        filters.append(f"drawbox=x={x+12}:y=346:w=34:h=26:color=0x97a2b1:t=fill")
    filters.extend([
        "drawbox=x=320:y=400:w=640:h=64:color=0x101825:t=fill",
        f"drawtext=fontfile='{font}':text='{scene['vehicle_id']}':fontcolor=white:fontsize=36:x=338:y=412",
        f"drawtext=fontfile='{font}':text='{scene['person_id']}  {scene['device_id']}':fontcolor=white:fontsize=25:x=300:y=510",
        f"drawtext=fontfile='{font}':text='{scene['location_id']}   {scene['style'].upper()}   REVIEW REQUIRED':fontcolor=white:fontsize=24:x=300:y=554",
        f"drawtext=fontfile='{font}':text='FICTIONAL CASE {CASES[scene['case_id']]}  /  {scene['start_seconds']:02d}-{scene['end_seconds']:02d} seconds':fontcolor=white:fontsize=25:x=24:y=660",
    ])
    return ",".join(filters)


def make_visual(case_id, directory, work, font):
    visual = directory / "visual"
    visual.mkdir(exist_ok=True)
    banner = work / "banner.txt"
    banner.write_text(BANNER)
    for scene in scene_plan(case_id):
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=0x111d2d:s=1280x720:r=10",
             "-vf", draw_filter(scene, font, banner), "-frames:v", "1", "-threads", "1", str(directory / scene["still_path"])])
    playlist = work / "frames.ffconcat"
    playlist.write_text("ffconcat version 1.0\n" + "".join(
        f"file '{directory / scene['still_path']}'\nduration 9\n" for scene in scene_plan(case_id))
        + f"file '{visual / 'still-03.png'}'\n")
    run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-safe", "0", "-f", "concat", "-i", str(playlist),
         "-vf", "fps=10", "-frames:v", "270", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "25",
         "-threads", "1", "-pix_fmt", "yuv420p", "-map_metadata", "-1", "-fflags", "+bitexact",
         "-flags:v", "+bitexact", "-movflags", "+faststart", str(visual / "footage.mp4")])


def content_type(path):
    return {".pdf": "application/pdf", ".txt": "text/plain", ".csv": "text/csv", ".json": "application/json",
            ".wav": "audio/wav", ".rttm": "text/plain", ".png": "image/png", ".mp4": "video/mp4"}[Path(path).suffix]


def expectation(case_id, path):
    return {"path": path, "case_id": case_id, "record_type": "human_authored_acceptance_assertion",
            "assertion_origin": "authored acceptance specification; not worker or model output",
            "expected_outcome": "review_required" if CASES[case_id] == "ECHO" else "accepted_for_ingestion_and_review",
            "preserve_provenance": True, "case_scoped_graph": True, "model_detection_required": False,
            "perfect_asr_or_diarization_required": False, **SAFE}


def provenance_records(case_id, paths):
    """Authored register and expected results; no inference is performed here."""
    expected_path = f"expected-results/{case_id}.json"
    paths = sorted(paths)
    entries = [{"path": path, "case_id": case_id, "evidence_id": f"SYN-EVID-{CASES[case_id]}-{i+1:03d}",
                "modality": "expected_result" if path == expected_path else Path(path).parts[2],
                "content_type": content_type(path), "synthetic": True,
                "source": "offline_procedural_generator", "expected_result_path": expected_path}
               for i, path in enumerate(paths)]
    register = envelope(case_id, files=entries, hash_authority=MANIFEST,
        self_entry_policy="register and expected-result records reference their own paths; hashes live in the global manifest")
    expected = envelope(case_id, record_type="human_authored_acceptance_assertion",
        assertion_origin="human-authored task requirements expressed as acceptance assertions; not worker or model output",
        expected_outcome="review_required" if CASES[case_id] == "ECHO" else "accepted_for_ingestion_and_review",
        **SAFE, artifact_expectations=[expectation(case_id, path) for path in paths],
        speaker_roles_case_scoped=True, cross_case_visibility="not_automatically_visible")
    if CASES[case_id] in {"NF", "COP"}:
        expected["isolation_rule"] = {"shared_identifier": SHARED,
            "nightfall_investigators_can_automatically_see_copper": False,
            "blocked_scopes": ["records", "entities", "observations", "graph_nodes", "case_details"],
            "rule": "Opening Nightfall must not expose Copper solely because the vehicle token matches."}
    if CASES[case_id] == "ECHO":
        expected["candidate_connection"] = {"candidate_id": "SYN-LINK-ECHO-01", "status": "review_required",
                                            "automatic_identity_merge": False, "automatic_cross_case_linking": False}
    return register, expected


def add_register(directory, case_id):
    prefix = f"operation-nightfall/{case_id}/"
    paths = [prefix+p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()]
    paths += [prefix+"metadata/evidence-register.json", f"expected-results/{case_id}.json"]
    register, expected = provenance_records(case_id, paths)
    (directory / "metadata/evidence-register.json").write_bytes(encoded(register))
    return expected


def checked_path(root, relative):
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
        raise ValueError(f"unsafe relative path: {relative}")
    destination = root / path
    if not destination.resolve().is_relative_to(root):
        raise ValueError(f"path escapes output root: {relative}")
    for component in [destination, *destination.parents]:
        if component == root:
            break
        if component.is_symlink():
            raise ValueError(f"symlink output is prohibited: {component}")
    return destination


def toolchain():
    for name in ("espeak-ng", "ffmpeg", "ffprobe", "fc-match", "pdftotext"):
        if not shutil.which(name):
            raise ValueError(f"required offline tool unavailable: {name}; nothing was replaced")
    font = run(["fc-match", "-f", "%{file}", "monospace"]).decode()
    if not Path(font).is_file() or any(char in font for char in "':\\"):
        raise ValueError("no usable local monospace font")
    return font, {"ffmpeg": run(["ffmpeg", "-version"]).decode().splitlines()[0],
                  "espeak_ng": run(["espeak-ng", "--version"]).decode().strip(),
                  "font_sha256": digest(Path(font).read_bytes()), "font_name": Path(font).name}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", default=".")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    root = Path(args.output_root).absolute()
    if root.is_symlink() or root.resolve() in {Path(root.anchor), Path.home()}:
        parser.error("unsafe output root")
    root = root.resolve()
    manifest_path = checked_path(root, MANIFEST)
    old_paths = set()
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text())
        for entry in old["files"]:
            path = entry["path"]
            checked_path(root, path)
            if not (any(path.startswith(f"operation-nightfall/{case}/") for case in CASES)
                    or path in {f"expected-results/{case}.json" for case in CASES}):
                parser.error("existing manifest lists an unmanaged path")
            if (root / path).exists() and digest((root / path).read_bytes()) != entry["sha256"]:
                parser.error(f"existing managed file was modified; preserve/reconcile it first: {path}")
            old_paths.add(path)
    existing = list((root / "operation-nightfall").rglob("*")) if (root / "operation-nightfall").exists() else []
    for path in existing:
        checked_path(root, path.relative_to(root).as_posix())
    if (existing or manifest_path.exists()) and not args.force:
        parser.error("output already exists; use --force for managed replacement")
    font, versions = toolchain()
    root.mkdir(parents=True, exist_ok=True)
    # Stage within the requested repository/output directory, validate before replacing any file.
    with tempfile.TemporaryDirectory(prefix=".nightfall-build-", dir=root) as temp:
        stage = Path(temp)
        work = stage / "work"
        work.mkdir()
        entries = []
        for case_id in CASES:
            directory = stage / "operation-nightfall" / case_id
            for relative, data in authored_files(case_id, args.seed).items():
                path = directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            make_audio(case_id, directory, work)
            make_visual(case_id, directory, work, font)
            expected = stage / f"expected-results/{case_id}.json"
            expected.parent.mkdir(exist_ok=True)
            expected.write_bytes(encoded(add_register(directory, case_id)))
            for path in sorted([p for p in directory.rglob("*") if p.is_file()] + [expected]):
                relative = path.relative_to(stage).as_posix()
                entries.append(dict(path=relative, case_id=case_id,
                    modality="expected_result" if path == expected else path.relative_to(directory).parts[0],
                    content_type=content_type(path), bytes=path.stat().st_size,
                    sha256=digest(path.read_bytes()), generated_synthetic_data=True,
                    expected_result_path=f"expected-results/{case_id}.json"))
            print(f"built {case_id}: 120 calls, 90 transactions, 60 messages, 81s speech, 27s 720p video", flush=True)
        manifest = dict(schema_version=2, corpus_id="SYN-CORPUS-OPERATION-NIGHTFALL-V1", seed=args.seed,
                        case_ids=list(CASES), files=entries, toolchain=versions,
                        purpose="controlled fictional acceptance testing only", **SAFE)
        (stage / "manifests").mkdir()
        (stage / MANIFEST).write_bytes(encoded(manifest))
        from verify_operation_nightfall import verify
        verify(stage, quiet=True)
        new_paths = {entry["path"] for entry in entries}
        for relative in sorted(new_paths | {MANIFEST}):
            destination = checked_path(root, relative)
            if destination.exists() and relative not in old_paths | {MANIFEST}:
                parser.error(f"refusing to overwrite an unrecognized file: {relative}")
        unlisted = {p.relative_to(root).as_posix() for p in existing if p.is_file()} - old_paths
        if unlisted:
            parser.error(f"unregistered existing files require reconciliation: {sorted(unlisted)}")
        obsolete = sorted(old_paths - new_paths)
        # Keep replaced/obsolete managed originals in an ignored, local recovery directory.
        if old_paths:
            backup_parent = root / "artifacts"
            checked_path(root, "artifacts")
            backup_parent.mkdir(exist_ok=True)
            backup = Path(tempfile.mkdtemp(prefix="nightfall-previous-", dir=backup_parent))
            for relative in sorted(old_paths | {MANIFEST}):
                if (root / relative).exists():
                    target = backup / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(root / relative, target)
            for relative in obsolete:
                if (root / relative).exists():
                    (root / relative).unlink()
            print(f"previous managed files recoverable at {backup}; retired {len(obsolete)} obsolete artifacts", flush=True)
        for relative in sorted(new_paths) + [MANIFEST]:
            destination = checked_path(root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / relative, destination)
        total = sum(entry["bytes"] for entry in entries) + manifest_path.stat().st_size
        print(f"generated {len(entries)} artifacts + manifest, {total} bytes; seed={args.seed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
