"""Validated workspace paths and crash-safe local state."""

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath


@dataclass(frozen=True)
class Settings:
    tiles: int = 256
    columns: int = 64
    rows: int = 36
    fps: int = 24
    clip_seconds: int = 5
    seed: int = 937

    def __post_init__(self):
        bounds = {
            "tiles": (1, 256),
            "columns": (2, 128),
            "rows": (2, 128),
            "fps": (1, 60),
            "clip_seconds": (5, 15),
            "seed": (0, 2**31 - 1),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer between {low} and {high}")
        if self.tiles > self.columns * self.rows:
            raise ValueError("The grid must have at least one cell per source movie")

    @property
    def frames(self):
        return self.fps * self.clip_seconds


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_json(path, value):
    """Never expose half-written queue receipts or manifests."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise ValueError(
            "Job IDs must be short lowercase letters, numbers, hyphens, or underscores"
        )
    return value


class Workspace:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    def path(self, relative):
        """Reject traversal, drive paths, UNC paths, and symlinks leaving this run."""
        if not isinstance(relative, (str, Path)) or not str(relative):
            raise ValueError("Expected a workspace-relative path")
        raw = str(relative)
        windows = PureWindowsPath(raw)
        if Path(raw).is_absolute() or windows.drive or windows.root or "\\" in raw:
            raise ValueError("Use a relative path with forward slashes")
        if ".." in Path(raw).parts:
            raise ValueError("Parent traversal is not allowed")
        result = (self.root / raw).resolve()
        if result == self.root or not result.is_relative_to(self.root):
            raise ValueError("Path must remain inside the workspace")
        return result

    @property
    def settings(self):
        return Settings(**read_json(self.path("config.json")))

    def save(self, relative, value):
        atomic_json(self.path(relative), value)

    def load(self, relative):
        return read_json(self.path(relative))

    def initialize(self, settings):
        if self.path("config.json").exists():
            raise ValueError("This workspace already exists; choose another path")
        self.root.mkdir(parents=True, exist_ok=True)
        self.save("config.json", asdict(settings))

    @contextmanager
    def lock(self):
        """One writer per run. A stale lock requires explicit local recovery."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(".videomosaic.lock")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ValueError(
                "Workspace is locked; see docs/operations.md before removing a stale lock"
            ) from exc
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(str(os.getpid()))
            yield
        finally:
            path.unlink(missing_ok=True)


def load_key(env_file=None):
    """Read only FAL_KEY, never execute .env content or search parent folders."""
    key = os.environ.get("FAL_KEY", "").strip()
    if not key and env_file is not None and Path(env_file).is_file():
        for raw in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip().removeprefix("export ")
            if line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "FAL_KEY":
                key = value.strip()
                if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                    key = key[1:-1]
    if not key or any(c in key for c in "\r\n"):
        raise ValueError("Set FAL_KEY in the environment or supply --env-file .env")
    return key
