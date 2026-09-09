import shutil

import numpy as np

from videomosaic.guidance import prepare_guidance, select_guided
from videomosaic.media import save_array
from videomosaic.pipeline import analyze, load_analysis, load_assignment
from videomosaic.plan import jobs_for


def test_guided_replacement_keeps_originals_and_is_idempotent(local_workspace):
    workspace = local_workspace
    analyze(workspace)
    sources, means, _, metadata = load_analysis(workspace)
    settings = workspace.settings
    # Controlled color trajectory: source 1 is an exact target-color match.
    target = np.broadcast_to(
        means[:, 1, None, None], (settings.frames, settings.rows, settings.columns, 3)
    ).copy()
    save_array(workspace.path("cache/target.npy"), target)
    indices = np.zeros((settings.rows, settings.columns), dtype=int)
    indices[0, :3] = [1, 2, 3]
    workspace.save(
        "assignment.json",
        {
            "fingerprint": metadata["fingerprint"],
            "indices": indices.tolist(),
            "original_sources": 4,
            "source_ids": [s["id"] for s in sources],
        },
    )
    assert prepare_guidance(workspace, count=1)["guided_candidates"] == 1
    job = jobs_for(workspace, "guided")[0]
    assert job["source_index"] == 0
    assert "image_url" not in job["input"] and "end_image_url" not in job["input"]
    destination = workspace.path(job["output"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(workspace.path(sources[1]["output"]), destination)
    report = select_guided(workspace)
    assert report["accepted"] == 1 and report["improved_placements"] > 1
    final_sources, _, _, final_metadata = load_analysis(workspace)
    final_indices = load_assignment(workspace, final_metadata, len(final_sources))
    assert set(final_indices.ravel()) == set(range(5))
    assert select_guided(workspace) == report
