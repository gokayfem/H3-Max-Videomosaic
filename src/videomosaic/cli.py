"""The public command line; costly and public operations require --execute."""

import argparse
import json
import sys
from contextlib import nullcontext

from . import __version__
from .generation import attach_request, generate
from .guidance import prepare_guidance, select_guided
from .media import verify_video
from .pipeline import analyze, import_footage, match
from .plan import prepare
from .render import render
from .storage import Settings, Workspace, load_key
from .upload import publish


def parser():
    root = argparse.ArgumentParser(
        description="Moving portraits built from 480p H3 Max Turbo movies"
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for name, help_text in {
        "init": "Create a reviewable plan, without network requests",
        "import": "Copy existing footage into a new workspace",
        "generate": "Preview or execute a fal job group",
        "attach-request": "Recover a queue receipt without submitting a job",
        "analyze": "Measure target and source color trajectories",
        "match": "Assign a fixed movie to every cell",
        "guide": "Prepare local first/last-frame experiments",
        "select-guided": "Keep guided variants that improve measured color error",
        "render": "Render and verify a local mosaic MP4",
        "verify": "Decode an entire MP4 and check dimensions and motion",
        "upload": "Preview or publish a selected MP4 to fal CDN",
    }.items():
        command = commands.add_parser(name, help=help_text)
        command.add_argument(
            "--workspace", default="runs/portrait", help="Run directory (default: runs/portrait)"
        )
        if name in {"init", "import"}:
            command.add_argument("--columns", type=int, default=64)
            command.add_argument("--rows", type=int, default=36)
            command.add_argument("--fps", type=int, default=24)
            command.add_argument("--clip-seconds", type=int, default=5)
            command.add_argument("--seed", type=int, default=937)
        if name == "init":
            command.add_argument("--tiles", type=int, default=256)
        if name == "import":
            command.add_argument("--reference", required=True)
            command.add_argument("--clips", required=True)
        if name in {"generate", "attach-request"}:
            command.add_argument("--group", choices=["portrait", "tiles", "guided"], required=True)
        if name in {"generate", "upload"}:
            command.add_argument(
                "--execute",
                action="store_true",
                help="Authorize the selected paid generation or public upload",
            )
            command.add_argument(
                "--env-file", help="Explicit local .env path; parent folders are never searched"
            )
        if name == "generate":
            command.add_argument("--concurrency", type=int, default=6)
            command.add_argument(
                "--limit", type=int, help="Only process the first N jobs in this group"
            )
        if name == "attach-request":
            command.add_argument("--job", required=True)
            command.add_argument("--request-id", required=True)
        if name == "guide":
            command.add_argument("--count", type=int, default=16)
        if name == "render":
            command.add_argument(
                "--style", choices=["wide", "reveal", "showcase"], default="showcase"
            )
            command.add_argument("--width", type=int, default=1920)
            command.add_argument("--crf", type=int, default=17)
            command.add_argument("--overwrite", action="store_true")
        if name in {"verify", "upload"}:
            command.add_argument(
                "--file", required=True, help="MP4 path relative to this workspace"
            )
    return root


def dispatch(args, workspace):
    name = args.command
    if name in {"init", "import"}:
        from pathlib import Path

        tiles = (
            args.tiles if name == "init" else len(list(Path(args.clips).expanduser().glob("*.mp4")))
        )
        settings = Settings(
            tiles=tiles,
            columns=args.columns,
            rows=args.rows,
            fps=args.fps,
            clip_seconds=args.clip_seconds,
            seed=args.seed,
        )
        if name == "init":
            workspace.initialize(settings)
            return prepare(workspace)
        return import_footage(workspace, args.reference, args.clips, settings)
    if name == "generate":
        options = {"concurrency": args.concurrency, "limit": args.limit}
        preview = generate(workspace, args.group, **options)
        return (
            generate(workspace, args.group, execute=True, key=load_key(args.env_file), **options)
            if args.execute
            else preview
        )
    if name == "attach-request":
        return attach_request(workspace, args.group, args.job, args.request_id)
    if name == "analyze":
        return analyze(workspace)
    if name == "match":
        return match(workspace)
    if name == "guide":
        return prepare_guidance(workspace, args.count)
    if name == "select-guided":
        return select_guided(workspace)
    if name == "render":
        return render(
            workspace, style=args.style, width=args.width, crf=args.crf, overwrite=args.overwrite
        )
    if name == "verify":
        return verify_video(workspace.path(args.file))
    if name == "upload":
        preview = publish(workspace, args.file)
        return (
            publish(workspace, args.file, execute=True, key=load_key(args.env_file))
            if args.execute
            else preview
        )
    raise ValueError("Unknown command")


def main(argv=None):
    args = parser().parse_args(argv)
    workspace = Workspace(args.workspace)
    read_only = args.command == "verify" or (
        args.command in {"generate", "upload"} and not args.execute
    )
    try:
        with nullcontext() if read_only else workspace.lock():
            report = dispatch(args, workspace)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except (ValueError, FileNotFoundError) as exc:
        # Library/network exceptions are handled at their boundary. Never dump
        # API responses, environment variables, or argument dictionaries here.
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted. Saved request IDs can be resumed; see docs/operations.md.",
            file=sys.stderr,
        )
        return 130
    except Exception:
        print(
            "Operation failed. Check workspace inputs, free disk space, and docs/operations.md.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
