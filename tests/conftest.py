"""All tests are offline: a missed HTTP mock must fail before any request."""

import subprocess

import httpx
import numpy as np
import pytest
import requests

from videomosaic.media import ffmpeg_path
from videomosaic.plan import prepare
from videomosaic.storage import Settings, Workspace


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must never make real network requests")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(httpx.Client, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)


@pytest.fixture
def workspace(tmp_path):
    run = Workspace(tmp_path / "run")
    run.initialize(Settings(tiles=4, columns=16, rows=9, fps=6))
    prepare(run)
    return run


@pytest.fixture(scope="session")
def movies(tmp_path_factory):
    try:
        ffmpeg = ffmpeg_path()
    except ValueError:
        pytest.skip("FFmpeg is needed for media integration tests")
    root = tmp_path_factory.mktemp("movies")
    clips = root / "clips"
    clips.mkdir()

    def write(path, width, height, color):
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            "6",
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "1",
            str(path),
        ]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        yy, xx = np.mgrid[:height, :width]
        for frame in range(30):
            base = np.empty((height, width, 3), np.uint8)
            base[:] = color
            circle = (xx - (width * 0.3 + frame * width / 90)) ** 2 + (yy - height * 0.5) ** 2 < (
                height * 0.25
            ) ** 2
            base[circle] = np.clip(np.array(color) + 50 + frame, 0, 255)
            process.stdin.write(base.tobytes())
        process.stdin.close()
        assert process.wait() == 0, process.stderr.read().decode()
        process.stderr.close()

    for index, color in enumerate([(55, 75, 100), (180, 130, 80), (45, 160, 110), (190, 150, 160)]):
        write(clips / f"source-{index}.mp4", 480, 480, color)
    reference = root / "reference.mp4"
    write(reference, 320, 180, (110, 100, 90))
    return reference, clips


@pytest.fixture
def local_workspace(tmp_path, movies):
    from videomosaic.pipeline import import_footage

    reference, clips = movies
    run = Workspace(tmp_path / "local")
    import_footage(run, reference, clips, Settings(tiles=4, columns=16, rows=9, fps=6))
    return run
