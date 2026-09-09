"""Bounded video decoding, portable FFmpeg discovery, and output validation."""

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw


def capture(path):
    cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"Cannot decode video: {Path(path).name}")
    return cap


def decode(path, settings, size=None, square_source=False):
    cap = capture(path)
    try:
        rate = cap.get(cv2.CAP_PROP_FPS)
        width, height = (
            int(cap.get(prop)) for prop in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT)
        )
        if not 0.1 <= rate <= 240 or not (1 <= width <= 8192 and 1 <= height <= 8192):
            raise ValueError("Unsupported video metadata")
        if square_source and (width, height) != (480, 480):
            raise ValueError("Source movies must be 480×480 pixels")
        wanted = np.floor(np.arange(settings.frames) * rate / settings.fps).astype(int)
        target_width, target_height = size or (width, height)
        result = np.empty((settings.frames, target_height, target_width, 3), np.uint8)
        current = -1
        for index, native_index in enumerate(wanted):
            while current < native_index:
                ok, bgr = cap.read()
                if not ok:
                    raise ValueError(
                        f"Video is shorter than {settings.clip_seconds} seconds: {Path(path).name}"
                    )
                current += 1
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            if size:
                rgb = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
            result[index] = rgb
        return result, {"width": width, "height": height, "fps": rate}
    finally:
        cap.release()


def save_array(path, array):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".array-", suffix=".npy", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.save(handle, array, allow_pickle=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_array(path, mmap=False):
    return np.load(path, allow_pickle=False, mmap_mode="r" if mmap else None)


def contact_sheet(items, path, columns=4, thumb=(480, 270)):
    if not items:
        return
    width, height = thumb
    sheet = Image.new(
        "RGB", (columns * width, math.ceil(len(items) / columns) * (height + 28)), (20, 20, 20)
    )
    draw = ImageDraw.Draw(sheet)
    for index, (label, frame) in enumerate(items):
        picture = Image.fromarray(frame)
        picture.thumbnail(thumb)
        x, y = index % columns * width, index // columns * (height + 28)
        sheet.paste(picture, (x + (width - picture.width) // 2, y + (height - picture.height) // 2))
        draw.text((x + 8, y + height + 6), label, fill="white")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, quality=90)


def ffmpeg_path():
    candidate = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    if not candidate or not Path(candidate).is_file():
        raise ValueError("Install FFmpeg on PATH or set FFMPEG_PATH to its executable")
    return str(Path(candidate).resolve())


class Encoder:
    """Write to a sibling temporary file; retain an existing export on failure."""

    def __init__(self, output, width, height, fps, crf=17, overwrite=False):
        executable = ffmpeg_path()
        self.output = Path(output)
        if self.output.exists() and not overwrite:
            raise ValueError("Output exists; pass --overwrite to replace it")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".render-", suffix=".mp4", dir=self.output.parent)
        os.close(fd)
        self.temporary = Path(name)
        self.log = tempfile.TemporaryFile()
        command = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            str(fps),
            "-i",
            "pipe:0",
            "-an",
            "-vf",
            "scale=out_color_matrix=bt709:out_range=tv",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "8",
            "-g",
            str(2 * fps),
            "-movflags",
            "+faststart",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            str(self.temporary),
        ]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=self.log)
        except BaseException:
            self.log.close()
            self.temporary.unlink(missing_ok=True)
            raise

    def write(self, frame):
        self.process.stdin.write(frame.tobytes())

    def finish(self, expected):
        self.process.stdin.close()
        if self.process.wait():
            raise ValueError("FFmpeg could not encode the video; check its libx264 support")
        report = verify_video(self.temporary, **expected)
        os.replace(self.temporary, self.output)
        report["file"] = self.output.name
        return report

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        self.log.close()
        self.temporary.unlink(missing_ok=True)


def verify_video(path, frames=None, fps=None, width=None, height=None, sheet=None):
    cap = capture(path)
    items, differences, black = [], [], []
    previous = None
    count = 0
    try:
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if actual_fps <= 0:
            raise ValueError("Video frame rate is invalid")
        step = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) // 8)
        while True:
            ok, bgr = cap.read()
            if not ok:
                break
            small = cv2.resize(bgr, (160, 90), interpolation=cv2.INTER_AREA).astype(np.float32)
            if small.mean() < 2:
                black.append(count)
            if previous is not None:
                differences.append(float(np.abs(small - previous).mean()))
            previous = small
            if sheet and count % step == 0 and len(items) < 9:
                items.append(
                    (
                        f"{count / actual_fps:.2f}s",
                        cv2.cvtColor(cv2.resize(bgr, (640, 360)), cv2.COLOR_BGR2RGB),
                    )
                )
            count += 1
    finally:
        cap.release()
    if count < 2 or black:
        raise ValueError("Output is empty, truncated, or contains black frames")
    for expected, actual, label in (
        (frames, count, "frames"),
        (fps, actual_fps, "fps"),
        (width, actual_width, "width"),
        (height, actual_height, "height"),
    ):
        if expected is not None and abs(expected - actual) > 0.01:
            raise ValueError(f"Unexpected output {label}: {actual}")
    if not any(delta > 0.01 for delta in differences):
        raise ValueError("Output has no measurable motion")
    if sheet:
        contact_sheet(items, sheet)
    return {
        "file": Path(path).name,
        "width": actual_width,
        "height": actual_height,
        "fps": actual_fps,
        "frames": count,
        "duration": count / actual_fps,
        "bytes": Path(path).stat().st_size,
        "black_frames": len(black),
        "changing_frame_pairs": sum(d > 0.01 for d in differences),
    }
