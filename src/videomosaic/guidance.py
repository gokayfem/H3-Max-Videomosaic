"""Prepare first/last-frame experiments and retain measured improvements."""

import numpy as np
from PIL import Image

from .media import contact_sheet, decode
from .pipeline import analyze, load_analysis, load_assignment
from .plan import I2V, jobs_for


def prepare_guidance(workspace, count=16):
    if not 1 <= count <= 64:
        raise ValueError("Guided candidate count must be between 1 and 64")
    if workspace.path("jobs/guided.json").exists():
        raise ValueError("Guided jobs already exist; reuse that plan or start another workspace")
    sources, means, target, metadata = load_analysis(workspace)
    assignment = load_assignment(workspace, metadata, len(sources)).ravel()
    settings = workspace.settings
    target = target.reshape(settings.frames, -1, 3)
    luminance = target @ np.array([0.2126, 0.7152, 0.0722])
    change = luminance[-1] - luminance[0]
    pulse = np.maximum(luminance[0], luminance[-1]) - luminance[
        settings.frames // 2 : 3 * settings.frames // 4
    ].min(0)
    error = np.sqrt(((means[:, assignment] - target) ** 2).mean((0, 2)))
    eligible = (luminance.mean(0) > 35) & (luminance.mean(0) < 195) & (error > 10) & (error < 90)
    # A single-placement source cannot be replaced while retaining every original.
    eligible &= np.bincount(assignment, minlength=len(sources))[assignment] > 1
    groups = [
        ("steady", abs(change) < 6, error),
        ("brightening", change > 6, change),
        ("darkening", change < -6, -change),
        ("passing-shadow", pulse > 8, pulse),
    ]
    candidates = []
    for kind, condition, score in groups:
        cells = np.where(eligible & condition)[0]
        candidates.append(
            [(kind, int(cell), int(assignment[cell])) for cell in cells[np.argsort(-score[cells])]]
        )
    selected, used = [], set()
    # Round-robin keeps the lighting trajectories diverse even for small batches.
    while len(selected) < count and any(candidates):
        for group in candidates:
            while group and group[0][2] in used:
                group.pop(0)
            if group and len(selected) < count:
                item = group.pop(0)
                selected.append(item)
                used.add(item[2])
    jobs, previews = [], []
    for index, (kind, cell, source_index) in enumerate(selected):
        source = sources[source_index]
        frames, _ = decode(
            workspace.path(source["output"]), settings, (480, 480), square_source=True
        )
        ident = f"guided-{index:02d}"
        paths = []
        for frame_index, label in ((0, "first"), (settings.frames - 1, "last")):
            frame = frames[frame_index].astype(np.float32)
            delta = np.clip(target[frame_index, cell] - frame.mean((0, 1)), -65, 65) * 0.85
            anchor = np.clip(frame + delta, 0, 255).astype(np.uint8)
            relative = f"keyframes/{ident}-{label}.jpg"
            path = workspace.path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(anchor).save(path, quality=95)
            paths.append(relative)
            previews.append((f"{ident} {kind} {label}", anchor))
        motion = {
            "steady": "Maintain nearly constant lighting while the scene moves naturally.",
            "brightening": "A broad soft source of warm light gradually brightens the scene.",
            "darkening": "A broad soft shadow gradually moves over the scene, reducing illumination gently.",
            "passing-shadow": "During the middle of the shot a soft passing shadow briefly darkens the scene, then the light returns.",
        }[kind]
        prompt = (
            f"One continuous {settings.clip_seconds}-second cinematic shot of the same "
            f"{source.get('subject', 'scene')} shown in the supplied first and last images. "
            "Preserve the subject, objects, textures, composition and camera while adding gentle believable natural motion. "
            f"{motion} Begin with the first image and arrive smoothly at the last image. "
            "Locked camera, restrained natural motion, consistent geometry. No cuts, no dissolves between "
            "unrelated scenes, no sudden flashes, no text, no logos, no collage."
        )
        jobs.append(
            {
                "id": ident,
                "source_index": source_index,
                "target_cell": cell,
                "kind": kind,
                "base_fingerprint": metadata["fingerprint"],
                "endpoint": I2V,
                "output": f"guided/{ident}.mp4",
                "first_frame": paths[0],
                "last_frame": paths[1],
                "input": {
                    "prompt": prompt,
                    "duration": settings.clip_seconds,
                    "resolution": "480P",
                    "prompt_expansion_mode": "balanced",
                    "seed": (settings.seed + 3000 + index) % 2**31,
                    "enable_safety_checker": True,
                    "sync_mode": False,
                },
            }
        )
    if jobs:
        workspace.save("jobs/guided.json", jobs)
        contact_sheet(previews, workspace.path("reports/keyframe-pairs.jpg"), thumb=(240, 240))
    return {"guided_candidates": len(jobs), "network_requests": 0}


def improved_cells(old_mean, new_mean, target, cells, motion, baseline_motion):
    if motion <= max(0.08, baseline_motion * 0.12) or len(cells) <= 1:
        return np.array([], dtype=int)
    old_error = ((old_mean[:, None] - target[:, cells]) ** 2).mean((0, 2))
    new_error = ((new_mean[:, None] - target[:, cells]) ** 2).mean((0, 2))
    wins = cells[new_error < old_error * 0.93]
    return wins[:-1] if len(wins) == len(cells) else wins


def select_guided(workspace):
    if workspace.path("reports/guidance-selection.json").exists():
        return workspace.load("reports/guidance-selection.json")
    sources, means, target, metadata = load_analysis(workspace)
    if len(sources) != workspace.settings.tiles:
        raise ValueError("Guided selection requires an unmodified original source library")
    assignment = load_assignment(workspace, metadata, len(sources)).ravel().copy()
    settings = workspace.settings
    target = target.reshape(settings.frames, -1, 3)
    jobs = jobs_for(workspace, "guided")
    comparisons = []
    for job in jobs:
        if job.get("base_fingerprint") != metadata["fingerprint"]:
            raise ValueError("Guided jobs belong to different analyzed inputs")
        index = job["source_index"]
        if type(index) is not int or not 0 <= index < settings.tiles:
            raise ValueError("Invalid guided source index")
        frames, _ = decode(workspace.path(job["output"]), settings, (64, 64), square_source=True)
        new_mean = frames.mean((1, 2), dtype=np.float32)
        motion = float(
            np.abs(np.diff(frames[:: max(1, settings.fps // 6)].astype(np.float32), axis=0)).mean()
        )
        cells = np.where(assignment == index)[0]
        wins = improved_cells(
            means[:, index], new_mean, target, cells, motion, metadata["motion"][index]
        )
        if len(wins):
            assignment[wins] = len(sources)
            sources.append(
                {
                    "id": job["id"],
                    "output": job["output"],
                    "subject": sources[index].get("subject", "scene"),
                    "palette": sources[index].get("palette", "guided"),
                    "original_source": index,
                }
            )
        comparisons.append(
            {
                "id": job["id"],
                "accepted": bool(len(wins)),
                "improved_placements": len(wins),
                "motion": motion,
            }
        )
    report = {
        "tested": len(jobs),
        "accepted": sum(c["accepted"] for c in comparisons),
        "improved_placements": sum(c["improved_placements"] for c in comparisons),
        "comparisons": comparisons,
    }
    # Save the original manifests so a failed cache rebuild can be rolled back.
    original_sources = workspace.load("sources.json")
    original_assignment = workspace.load("assignment.json")
    workspace.save("reports/pre-guidance-sources.json", original_sources)
    workspace.save("reports/pre-guidance-assignment.json", original_assignment)
    try:
        workspace.save("sources.json", sources)
        analyze(workspace)
        fresh = workspace.load("cache/analysis.json")
        workspace.save(
            "assignment.json",
            {
                "fingerprint": fresh["fingerprint"],
                "indices": assignment.reshape(settings.rows, settings.columns).tolist(),
                "original_sources": settings.tiles,
                "source_ids": [s["id"] for s in sources],
            },
        )
        workspace.save("reports/guidance-selection.json", report)
    except Exception:
        workspace.save("sources.json", original_sources)
        workspace.save("assignment.json", original_assignment)
        raise
    return report
