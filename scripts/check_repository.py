"""Inspect staged Git blobs; print names, never matched secret values."""

import re
import subprocess
from pathlib import Path, PurePosixPath


def violations(name, data):
    path = PurePosixPath(name)
    errors = []
    if path.name.lower().startswith(".env") and path.name != ".env.example":
        errors.append("credential file")
    if path.suffix.lower() in {".mp4", ".mov", ".webm", ".npy", ".npz", ".pem", ".key", ".part"}:
        errors.append("private or generated artifact")
    if any(part in {"runs", "state", "cache", ".venv", "__pycache__"} for part in path.parts):
        errors.append("generated directory")
    if len(data) > 2 * 1024 * 1024:
        errors.append("unexpected file larger than 2 MiB")
    if path.suffix.lower() in {".jpg", ".png"}:
        return errors
    text = data.decode("utf-8", errors="replace")
    patterns = [
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{30,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"data:image/[^;]+;base64,[A-Za-z0-9+/]{100,}",
        r"(?m)^[ \t]*(?:export[ \t]+)?FAL_KEY[ \t]*=[ \t]*['\"]?[^\s#'\"]{12,}",
    ]
    if any(re.search(pattern, text) for pattern in patterns):
        errors.append("possible embedded secret or image data")
    if re.search(r"[A-Za-z]:[/\\]Users[/\\]", text):
        errors.append("machine-specific user path")
    return errors


def main():
    root = Path(__file__).resolve().parents[1]
    git = ["git", "-c", f"safe.directory={root.as_posix()}"]
    result = subprocess.run(
        [*git, "ls-files", "--stage", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    entries = [entry for entry in result.stdout.decode().split("\0") if entry]
    if not entries:
        raise SystemExit("No tracked files; stage the intended source files before scanning")
    failures = []
    for entry in entries:
        metadata, name = entry.split("\t", 1)
        mode, object_id, stage = metadata.split()
        if mode not in {"100644", "100755"} or stage != "0":
            failures.append(f"{name}: unexpected Git file mode or unmerged stage")
            continue
        data = subprocess.run(
            [*git, "cat-file", "blob", object_id], cwd=root, check=True, capture_output=True
        ).stdout
        for problem in violations(name, data):
            failures.append(f"{name}: {problem}")
    if failures:
        print("\n".join(failures))
        return 1
    print(
        f"Checked {len(entries)} staged files: no blocked artifacts or common secret patterns found."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
