import numpy as np
import pytest

from videomosaic.cli import main
from videomosaic.guidance import improved_cells, prepare_guidance
from videomosaic.matching import correct, match_sources
from videomosaic.media import decode, verify_video
from videomosaic.pipeline import analyze, load_analysis, match
from videomosaic.render import FrameStream, camera, loop_indices, render
from videomosaic.storage import Settings
from videomosaic.upload import publish


def test_temporal_matching_is_deterministic_and_keeps_every_source():
    rng = np.random.default_rng(45)
    sources = rng.uniform(30, 200, (12, 8, 3)).astype(np.float32)
    targets = rng.uniform(30, 200, (12, 4, 6, 3)).astype(np.float32)
    first = match_sources(sources, targets)
    assert first.shape == (4, 6)
    assert set(first.ravel()) == set(range(8))
    np.testing.assert_array_equal(first, match_sources(sources, targets))
    # Temporal order changes the best match even when time-averaged colors agree.
    ramp = np.linspace(40, 190, 12)[:, None] * np.ones((1, 3))
    reversed_sources = np.stack([ramp, ramp[::-1]], axis=1).astype(np.float32)
    target = np.broadcast_to(ramp[:, None, None], (12, 2, 2, 3)).copy()
    assignment = match_sources(reversed_sources, target)
    assert np.count_nonzero(assignment == 0) >= 2


def test_correction_preserves_detail_and_improves_color_error():
    rng = np.random.default_rng(9)
    tile = rng.integers(60, 130, (64, 64, 3), dtype=np.uint8)
    mean = tile.mean((0, 1))
    target = np.array([160, 130, 110])
    corrected = correct(tile, mean, target)
    assert np.linalg.norm(corrected.mean((0, 1)) - target) < np.linalg.norm(mean - target) * 0.1
    assert corrected.dtype == np.uint8 and corrected.std() > 5
    assert np.corrcoef(tile[:, :, 0].ravel(), corrected[:, :, 0].ravel())[0, 1] > 0.99


def test_guided_selector_requires_motion_and_retains_original():
    old = np.full((30, 3), 100, dtype=np.float32)
    new = np.full((30, 3), 150, dtype=np.float32)
    target = np.full((30, 4, 3), 150, dtype=np.float32)
    cells = np.arange(4)
    assert len(improved_cells(old, new, target, cells, 1, 1)) == 3
    assert len(improved_cells(old, new, target, cells, 0, 1)) == 0
    assert len(improved_cells(new, old, target, cells, 1, 1)) == 0


@pytest.mark.parametrize("style", ["wide", "reveal", "showcase"])
def test_camera_stays_inside_grid(style):
    settings = Settings()
    for time in np.linspace(0, 32, 321):
        scale, x, y = camera(time, settings, style)
        assert scale >= 1
        assert x - settings.columns / (2 * scale) >= -1e-8
        assert x + settings.columns / (2 * scale) <= settings.columns + 1e-8
        assert y - settings.rows / (2 * scale) >= -1e-8
        assert y + settings.rows / (2 * scale) <= settings.rows + 1e-8
    assert camera(32, settings, style) == (1, 32, 18)


def test_loop_join_has_no_out_of_bounds_indices():
    settings = Settings()
    for frame in range(768):
        first, second, amount = loop_indices(frame, settings)
        assert 0 <= first < 120 and 0 <= second < 120 and 0 <= amount <= 1
    before = loop_indices(107, settings)
    after = loop_indices(108, settings)
    assert before[2] > 0.98 and after == (12, 12, 0)


def test_full_local_pipeline(local_workspace, monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    assert analyze(local_workspace)["analyzed"] == 4
    assert match(local_workspace) == {"cells": 144, "sources_used": 4}
    result = render(local_workspace, style="wide", width=320)
    assert result["frames"] == 30 and result["changing_frame_pairs"] > 20
    assert result["black_frames"] == 0 and result["no_portrait_overlay"]
    with pytest.raises(ValueError, match="exists"):
        render(local_workspace, style="wide", width=320)
    tour = render(local_workspace, style="showcase", width=320)
    assert tour["duration"] == 32 and tour["frames"] == 192
    assert prepare_guidance(local_workspace)["network_requests"] == 0
    preview = publish(local_workspace, "renders/showcase-320.mp4")
    assert preview["execute"] is False and preview["bytes"] > 1000
    assert (
        main(
            [
                "verify",
                "--workspace",
                str(local_workspace.root),
                "--file",
                "renders/showcase-320.mp4",
            ]
        )
        == 0
    )
    report = verify_video(
        local_workspace.path("renders/wide-320.mp4"), width=320, height=180, fps=6, frames=30
    )
    assert report["duration"] == 5
    local_workspace.path("reference/portrait.mp4").write_bytes(b"changed")
    with pytest.raises(ValueError, match="stale"):
        load_analysis(local_workspace)


def test_streamed_frames_match_decode_after_backward_seeks(movies):
    _, clips = movies
    settings = Settings(tiles=4, fps=6)
    path = sorted(clips.glob("*.mp4"))[0]
    expected, _ = decode(path, settings, square_source=True)
    stream = FrameStream(path, settings)
    try:
        for frame in [0, 1, 20, 29, 2, 2, 17, 0, 29]:
            np.testing.assert_array_equal(stream.get(frame), expected[frame])
    finally:
        stream.close()


def test_cli_dry_run_and_safe_error(workspace, monkeypatch, capsys):
    monkeypatch.delenv("FAL_KEY", raising=False)
    assert main(["generate", "--workspace", str(workspace.root), "--group", "tiles"]) == 0
    assert '"execute": false' in capsys.readouterr().out
    assert (
        main(["generate", "--workspace", str(workspace.root), "--group", "tiles", "--execute"]) == 1
    )
    assert "Set FAL_KEY" in capsys.readouterr().err
    assert not workspace.path(".videomosaic.lock").exists()
