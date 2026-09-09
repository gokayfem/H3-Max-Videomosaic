"""Deterministic mosaic cameras with file-backed proxies and streamed close views."""

import json
import math
from collections import OrderedDict

import cv2
import numpy as np

from .matching import correct
from .media import Encoder, capture, decode, load_array, save_array
from .pipeline import load_analysis, load_assignment


def ease(value):
    value = min(1.0, max(0.0, value))
    return value**3 * (value * (value * 6 - 15) + 10)


def camera(time, settings, style="showcase"):
    columns, rows = settings.columns, settings.rows
    if style == "wide":
        return 1.0, columns / 2, rows / 2
    # Coordinates preserve the demonstrated portrait framing at any grid size.
    ax, ay = columns * 39.5 / 64, rows * 20 / 36
    bx, by = columns * 35 / 64, rows * 27 / 36
    zoom_a, zoom_b = max(1, columns / 5.333333), max(1, columns / 6.4)
    if style == "reveal":
        poses = [(0, zoom_a, ax, ay), (settings.clip_seconds, 1, columns / 2, rows / 2)]
    elif style == "showcase":
        dx, dy = columns * 0.65 / 64, rows * 0.25 / 36
        poses = [
            (0, zoom_a, ax - dx, ay),
            (4, zoom_a, ax + dx, ay + dy),
            (12, 1, columns / 2, rows / 2),
            (17, 1, columns / 2, rows / 2),
            (21, zoom_b, bx - dx, by),
            (24, zoom_b, bx + dx, by - dy),
            (28, 1, columns / 2, rows / 2),
            (32, 1, columns / 2, rows / 2),
        ]
    else:
        raise ValueError("Unknown camera style")
    scale, cx, cy = poses[-1][1:]
    for start, end in zip(poses[:-1], poses[1:], strict=True):
        if time <= end[0]:
            amount = ease((time - start[0]) / (end[0] - start[0]))
            scale = math.exp(math.log(start[1]) * (1 - amount) + math.log(end[1]) * amount)
            cx = start[2] * (1 - amount) + end[2] * amount
            cy = start[3] * (1 - amount) + end[3] * amount
            break
    half_x, half_y = columns / (2 * scale), rows / (2 * scale)
    return (
        scale,
        float(np.clip(cx, half_x, columns - half_x)),
        float(np.clip(cy, half_y, rows - half_y)),
    )


def loop_indices(frame, settings):
    overlap = max(1, round(settings.fps * 0.5))
    period = settings.frames - overlap
    phase = frame % period
    first = phase + overlap
    if phase < period - overlap:
        return first, first, 0.0
    second = phase - (period - overlap)
    return first, second, (1 - math.cos(math.pi * second / overlap)) / 2


def blend(first, second, amount):
    return cv2.addWeighted(first, 1 - amount, second, amount, 0) if amount > 0 else first


class FrameStream:
    def __init__(self, path, settings):
        self.cap = capture(path)
        self.rate = self.cap.get(cv2.CAP_PROP_FPS)
        if not 0.1 <= self.rate <= 240:
            self.close()
            raise ValueError("Invalid source frame rate")
        self.fps = settings.fps
        self.index = -1
        self.rgb = None

    def get(self, frame):
        wanted = math.floor(frame * self.rate / self.fps)
        if wanted < self.index:
            if not self.cap.set(cv2.CAP_PROP_POS_FRAMES, wanted):
                raise ValueError("Source decoder cannot seek")
            self.index = wanted - 1
        while self.index < wanted:
            ok, bgr = self.cap.read()
            if not ok:
                raise ValueError("Source ended during rendering; rerun analyze")
            self.index += 1
            self.rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return self.rgb

    def close(self):
        self.cap.release()


class Library:
    """Keep at most 96 native decoder frames, instead of whole 480p movies."""

    def __init__(self, workspace, sources, metadata):
        self.workspace, self.sources = workspace, sources
        self.settings, self.metadata = workspace.settings, metadata
        self.proxies = {}
        self.streams = OrderedDict()
        self.native_used = set()

    def proxy(self, index, size):
        key = index, size
        if key not in self.proxies:
            source = self.sources[index]
            signature = self.metadata["signatures"][index]
            name = f"cache/{source['id']}-{signature}-{self.settings.fps}-{self.settings.frames}-{size}.npy"
            path = self.workspace.path(name)
            if not path.exists():
                frames, _ = decode(
                    self.workspace.path(source["output"]),
                    self.settings,
                    (size, size),
                    square_source=True,
                )
                save_array(path, frames)
            frames = load_array(path, mmap=True)
            if frames.dtype != np.uint8 or frames.shape != (self.settings.frames, size, size, 3):
                raise ValueError("Invalid proxy cache; remove cache files and rerun analyze")
            self.proxies[key] = frames
        return self.proxies[key]

    def native(self, index, frame, side):
        key = index, side
        if key not in self.streams:
            while len(self.streams) >= 96:
                self.streams.popitem(last=False)[1].close()
            self.streams[key] = FrameStream(
                self.workspace.path(self.sources[index]["output"]), self.settings
            )
            self.native_used.add(index)
        self.streams.move_to_end(key)
        return self.streams[key].get(frame)

    def get(self, index, first, second, amount, pixels):
        if pixels > 192:
            a = self.native(index, first, 0)
            b = self.native(index, second, 1) if amount else a
        else:
            frames = self.proxy(index, 64 if pixels <= 64 else 192)
            a, b = frames[first], frames[second]
        return blend(a, b, amount)

    def close(self):
        for stream in self.streams.values():
            stream.close()
        self.streams.clear()
        for frames in self.proxies.values():
            frames._mmap.close()
        self.proxies.clear()


class Engine:
    def __init__(self, workspace):
        self.settings = workspace.settings
        sources, self.means, self.target, metadata = load_analysis(workspace)
        self.assignment = load_assignment(workspace, metadata, len(sources))
        self.library = Library(workspace, sources, metadata)

    def frame(self, index, width, height, style):
        settings = self.settings
        scale, cx, cy = camera(index / settings.fps, settings, style)
        first, second, amount = (
            loop_indices(index, settings) if style == "showcase" else (index, index, 0)
        )
        means = self.means[first] * (1 - amount) + self.means[second] * amount
        target = self.target[first] * (1 - amount) + self.target[second] * amount
        detail = 0.68 + 0.23 * np.clip(math.log(scale) / math.log(10), 0, 1)
        cell_x, cell_y = width / settings.columns * scale, height / settings.rows * scale
        xmin, ymin = cx - settings.columns / (2 * scale), cy - settings.rows / (2 * scale)
        xmax, ymax = cx + settings.columns / (2 * scale), cy + settings.rows / (2 * scale)
        canvas = np.zeros((height, width, 3), np.uint8)
        raw, resized = {}, {}
        for row in range(max(0, math.floor(ymin)), min(settings.rows, math.ceil(ymax))):
            y0, y1 = round((row - ymin) * cell_y), round((row + 1 - ymin) * cell_y)
            for column in range(max(0, math.floor(xmin)), min(settings.columns, math.ceil(xmax))):
                x0, x1 = round((column - xmin) * cell_x), round((column + 1 - xmin) * cell_x)
                ax0, ax1, ay0, ay1 = max(x0, 0), min(x1, width), max(y0, 0), min(y1, height)
                if ax0 >= ax1 or ay0 >= ay1:
                    continue
                source = int(self.assignment[row, column])
                if source not in raw:
                    raw[source] = self.library.get(
                        source, first, second, amount, max(cell_x, cell_y)
                    )
                key = source, x1 - x0, y1 - y0
                if key not in resized:
                    interpolation = (
                        cv2.INTER_AREA
                        if raw[source].shape[0] >= max(cell_x, cell_y)
                        else cv2.INTER_LANCZOS4
                    )
                    resized[key] = cv2.resize(raw[source], key[1:], interpolation=interpolation)
                tile = correct(resized[key], means[source], target[row, column], detail=detail)
                canvas[ay0:ay1, ax0:ax1] = tile[ay0 - y0 : ay1 - y0, ax0 - x0 : ax1 - x0]
        return canvas


def render(workspace, *, style="showcase", width=1920, crf=17, overwrite=False):
    if style not in {"wide", "reveal", "showcase"}:
        raise ValueError("Unknown render style")
    if type(width) is not int or width % 2 or not 128 <= width <= 3840:
        raise ValueError("Width must be an even integer between 128 and 3840")
    if type(crf) is not int or not 0 <= crf <= 35:
        raise ValueError("CRF must be an integer between 0 and 35")
    settings = workspace.settings
    height = round(width * settings.rows / settings.columns / 2) * 2
    if not 2 <= height <= 3840:
        raise ValueError("This grid produces an unsupported output height")
    output = workspace.path(f"renders/{style}-{width}.mp4")
    duration = 32 if style == "showcase" else settings.clip_seconds
    encoder = Encoder(output, width, height, settings.fps, crf, overwrite)
    engine = None
    try:
        engine = Engine(workspace)
        for index in range(duration * settings.fps):
            encoder.write(engine.frame(index, width, height, style))
            if (index + 1) % (settings.fps * 4) == 0:
                print(
                    json.dumps({"rendered_seconds": (index + 1) / settings.fps, "of": duration}),
                    flush=True,
                )
        report = encoder.finish(
            {
                "frames": duration * settings.fps,
                "fps": settings.fps,
                "width": width,
                "height": height,
                "sheet": workspace.path(f"reports/{style}-{width}.jpg"),
            }
        )
        report.update(
            style=style,
            original_sources=settings.tiles,
            sources_used=len(np.unique(engine.assignment)),
            native_sources_decoded=len(engine.library.native_used),
            no_portrait_overlay=True,
        )
        workspace.save(f"reports/{style}-{width}.json", report)
        return report
    finally:
        if engine:
            engine.library.close()
        encoder.close()
