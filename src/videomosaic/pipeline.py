"""Import, analyze, and match a workspace without contacting fal."""

import hashlib
import json
import shutil
from dataclasses import asdict
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter1d

from .matching import match_sources
from .media import contact_sheet, decode, load_array, save_array
from .storage import Settings, identifier, sha256


def sources_for(workspace):
    sources = workspace.load("sources.json")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 512:
        raise ValueError("Invalid source manifest")
    ids, outputs = set(), set()
    for source in sources:
        ident = identifier(source["id"])
        path = workspace.path(source["output"])
        if ident in ids or path in outputs or path.suffix != ".mp4" or not path.is_file():
            raise ValueError("Source files must exist and have unique IDs and paths")
        ids.add(ident)
        outputs.add(path)
    return sources


def fingerprint(workspace, sources):
    data = {
        "settings": asdict(workspace.settings),
        "sources": [(s["id"], s["output"], sha256(workspace.path(s["output"]))) for s in sources],
        "reference": sha256(workspace.path("reference/portrait.mp4")),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def source_signature(workspace, source):
    return sha256(workspace.path(source["output"]))[:20]


def import_footage(workspace, reference, clips, settings=None):
    reference = Path(reference).expanduser().resolve()
    clips = Path(clips).expanduser().resolve()
    inputs = sorted(clips.glob("*.mp4"))
    if not reference.is_file() or not 1 <= len(inputs) <= 256:
        raise ValueError("Supply a portrait file and a folder of 1–256 source MP4s")
    settings = settings or Settings(tiles=len(inputs))
    if settings.tiles != len(inputs):
        raise ValueError("Settings source count must match the input folder")
    # Check duration and canvas before creating the destination run.
    decode(reference, settings, (16, 9))
    for path in inputs:
        decode(path, settings, (8, 8), square_source=True)
    workspace.initialize(settings)
    sources = []
    for index, path in enumerate(inputs):
        ident = f"tile-{index:03d}"
        output = f"clips/{ident}.mp4"
        destination = workspace.path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        sources.append(
            {"id": ident, "output": output, "subject": "imported scene", "palette": "original"}
        )
    destination = workspace.path("reference/portrait.mp4")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(reference, destination)
    workspace.save("sources.json", sources)
    return {"imported": len(sources), "network_requests": 0}


def analyze(workspace):
    settings = workspace.settings
    sources = sources_for(workspace)
    target, _ = decode(
        workspace.path("reference/portrait.mp4"),
        settings,
        (settings.columns * 4, settings.rows * 4),
    )
    target = (
        target.reshape(settings.frames, settings.rows, 4, settings.columns, 4, 3)
        .mean((2, 4))
        .astype(np.float32)
    )
    target = gaussian_filter1d(target, 0.48, axis=0, mode="nearest")
    means, motion, previews, signatures = [], [], [], []
    for index, source in enumerate(sources):
        signature = source_signature(workspace, source)
        frames, _ = decode(workspace.path(source["output"]), settings, (64, 64), square_source=True)
        save_array(
            workspace.path(
                f"cache/{source['id']}-{signature}-{settings.fps}-{settings.frames}-64.npy"
            ),
            frames,
        )
        means.append(frames.mean((1, 2), dtype=np.float32))
        motion.append(
            float(
                np.abs(
                    np.diff(frames[:: max(1, settings.fps // 6)].astype(np.float32), axis=0)
                ).mean()
            )
        )
        signatures.append(signature)
        previews.append((source["id"], frames[settings.frames // 2]))
        if (index + 1) % 32 == 0:
            print(json.dumps({"analyzed": index + 1, "of": len(sources)}), flush=True)
    save_array(workspace.path("cache/means.npy"), np.stack(means, axis=1))
    save_array(workspace.path("cache/target.npy"), target)
    workspace.save(
        "cache/analysis.json",
        {
            "fingerprint": fingerprint(workspace, sources),
            "signatures": signatures,
            "source_ids": [s["id"] for s in sources],
            "motion": motion,
        },
    )
    contact_sheet(
        previews,
        workspace.path("reports/source-contact.jpg"),
        columns=min(16, len(sources)),
        thumb=(112, 112),
    )
    return {"analyzed": len(sources), "minimum_motion": min(motion)}


def load_analysis(workspace):
    sources = sources_for(workspace)
    metadata = workspace.load("cache/analysis.json")
    if metadata["fingerprint"] != fingerprint(workspace, sources):
        raise ValueError("Analysis is stale; rerun analyze and match")
    settings = workspace.settings
    means = load_array(workspace.path("cache/means.npy"))
    target = load_array(workspace.path("cache/target.npy"))
    if means.shape != (settings.frames, len(sources), 3) or target.shape != (
        settings.frames,
        settings.rows,
        settings.columns,
        3,
    ):
        raise ValueError("Analysis array shapes are invalid")
    if not np.isfinite(means).all() or not np.isfinite(target).all():
        raise ValueError("Analysis contains non-finite colors")
    return sources, means, target, metadata


def match(workspace):
    sources, means, target, metadata = load_analysis(workspace)
    assignment = match_sources(means, target, workspace.settings.seed)
    workspace.save(
        "assignment.json",
        {
            "fingerprint": metadata["fingerprint"],
            "indices": assignment.tolist(),
            "original_sources": workspace.settings.tiles,
            "source_ids": [s["id"] for s in sources],
        },
    )
    return {"cells": assignment.size, "sources_used": len(np.unique(assignment))}


def load_assignment(workspace, metadata, source_count):
    record = workspace.load("assignment.json")
    if record["fingerprint"] != metadata["fingerprint"]:
        raise ValueError("Assignment is stale; rerun match")
    raw = np.array(record["indices"])
    if raw.dtype.kind not in "iu" or raw.shape != (
        workspace.settings.rows,
        workspace.settings.columns,
    ):
        raise ValueError("Invalid assignment shape or type")
    if raw.min() < 0 or raw.max() >= source_count:
        raise ValueError("Assignment source index is out of bounds")
    if not set(range(workspace.settings.tiles)).issubset(set(raw.ravel())):
        raise ValueError("Assignment must retain every original source")
    return raw.astype(np.int32)
