"""Deterministic, reviewable generation plans; no network access."""

from importlib.resources import files

from .storage import identifier, read_json

T2V = "minimax/h3-max-turbo/text-to-video"
I2V = "minimax/h3-max-turbo/image-to-video"
ENDPOINTS = {T2V, I2V}


def prepare(workspace):
    catalog = read_json(files("videomosaic").joinpath("catalog.json"))
    settings = workspace.settings
    # Spread small pilot batches across all palettes, preserving the 256-job order.
    jobs = [catalog["tiles"][index * 256 // settings.tiles] for index in range(settings.tiles)]
    for index, job in enumerate(jobs):
        job["input"]["duration"] = settings.clip_seconds
        job["input"]["seed"] = (settings.seed * 1000 + 100 + index) % 2**31
    hero = catalog["portrait"]
    hero["input"]["duration"] = settings.clip_seconds
    hero["input"]["seed"] = (settings.seed * 1000) % 2**31
    if settings.clip_seconds != 5:
        duration = settings.clip_seconds
        for job in [*jobs, hero]:
            job["input"]["prompt"] = (
                job["input"]["prompt"]
                .replace("Five-second", f"{duration}-second")
                .replace("five-second", f"{duration}-second")
            )
        hero["input"]["prompt"] = (
            hero["input"]["prompt"]
            .replace("0 to 2 seconds", f"0 to {duration * 0.4:g} seconds")
            .replace("Around 3 seconds", f"Around {duration * 0.6:g} seconds")
            .replace("by 4 seconds", f"by {duration * 0.8:g} seconds")
        )
    workspace.save("jobs/tiles.json", jobs)
    workspace.save("jobs/portrait.json", [hero])
    workspace.save(
        "sources.json", [{k: j[k] for k in ("id", "output", "subject", "palette")} for j in jobs]
    )
    return {"tiles": len(jobs), "portrait": 1, "tile_resolution": "480P"}


def validate_job(workspace, job):
    identifier(job["id"])
    if job.get("endpoint") not in ENDPOINTS:
        raise ValueError("Only the two documented H3 Max Turbo endpoints are supported")
    output = workspace.path(job["output"])
    if output.suffix != ".mp4":
        raise ValueError("Job output must be an MP4")
    data = job["input"]
    allowed = {
        "prompt",
        "duration",
        "resolution",
        "prompt_expansion_mode",
        "seed",
        "enable_safety_checker",
        "sync_mode",
    }
    if job["endpoint"] == T2V:
        allowed.add("aspect_ratio")
        ratio = "16:9" if job["id"] == "portrait" else "1:1"
        if data.get("aspect_ratio") != ratio:
            raise ValueError("Use a 16:9 portrait and square source movies")
    elif "first_frame" not in job:
        raise ValueError("Image-to-video jobs need a first_frame file")
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Job input contains unsupported fields")
    if type(data.get("seed")) is not int or not 0 <= data["seed"] < 2**31:
        raise ValueError("Seed must be a nonnegative 32-bit integer")
    if not isinstance(data.get("prompt"), str) or not data["prompt"].strip():
        raise ValueError("A nonempty prompt is required")
    if data.get("enable_safety_checker") is not True or data.get("sync_mode") is not False:
        raise ValueError("The pipeline requires safety checking and CDN output")
    if type(data.get("duration")) is not int or not 5 <= data["duration"] <= 15:
        raise ValueError("H3 Max Turbo duration must be 5–15 seconds")
    if data["duration"] != workspace.settings.clip_seconds:
        raise ValueError("Job duration must match workspace clip_seconds")
    if data.get("resolution") not in {"480P", "768P", "1080P"}:
        raise ValueError("Unsupported resolution")
    if job["id"] != "portrait" and data["resolution"] != "480P":
        raise ValueError("Small source movies must use 480P")
    if data.get("prompt_expansion_mode") not in {"balanced", "quality"}:
        raise ValueError("Unsupported prompt expansion mode")
    for name in ("first_frame", "last_frame"):
        if name in job:
            path = workspace.path(job[name])
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png"} or not path.is_file():
                raise ValueError("A keyframe must be an existing JPG or PNG in this workspace")
    if "image_url" in data or "end_image_url" in data:
        raise ValueError("Use first_frame/last_frame workspace paths for keyframe jobs")
    return output


def jobs_for(workspace, group):
    if group not in {"portrait", "tiles", "guided"}:
        raise ValueError("Unknown job group")
    jobs = workspace.load(f"jobs/{group}.json")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("The job group is empty")
    ids, outputs = set(), set()
    for job in jobs:
        output = validate_job(workspace, job)
        if job["id"] in ids or output in outputs:
            raise ValueError("Duplicate job ID or output path")
        ids.add(job["id"])
        outputs.add(output)
    return jobs
