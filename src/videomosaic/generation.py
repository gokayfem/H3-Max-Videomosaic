"""Resumable fal queue jobs with no automatic retry of paid submissions."""

import base64
import concurrent.futures
import hashlib
import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import requests

from .plan import ENDPOINTS, jobs_for
from .storage import identifier, sha256

QUEUE = "https://queue.fal.run"
TRANSIENT = {429, 500, 502, 503, 504}


def safe_url(url, authenticated=False):
    """An untrusted receipt must never choose where the API key is sent."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError("Invalid fal URL")
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    allowed = (
        host == "queue.fal.run"
        if authenticated
        else (host == "fal.media" or host.endswith(".fal.media"))
    )
    if not (parsed.scheme == "https" and allowed and parsed.port in (None, 443)):
        raise ValueError("URL is outside the allowed fal host")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Credentials and fragments are not allowed in fal URLs")
    return url


def utc_now():
    return datetime.now(UTC).isoformat()


def input_hash(workspace, job):
    value = {"endpoint": job["endpoint"], "input": job["input"], "output": job["output"]}
    for key in ("first_frame", "last_frame"):
        if key in job:
            value[key] = sha256(workspace.path(job[key]))
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class QueueClient:
    def __init__(self, key):
        self.key = key
        self.session = requests.Session()
        # Do not inherit .netrc authentication or environment proxy credentials.
        self.session.trust_env = False

    def close(self):
        self.session.close()

    def submit(self, endpoint, arguments):
        if endpoint not in ENDPOINTS:
            raise ValueError("Unsupported generation endpoint")
        # Exactly one POST attempt. Even a timeout can mean the job was accepted.
        response = self.session.post(
            f"{QUEUE}/{endpoint}",
            json=arguments,
            headers={"Authorization": f"Key {self.key}"},
            timeout=(10, 90),
            allow_redirects=False,
        )
        try:
            if not 200 <= response.status_code < 300:
                raise ValueError(
                    f"Submission returned HTTP {response.status_code}; inspect the saved job before recovery"
                )
            return response.json()
        finally:
            response.close()

    def get(self, url, *, authenticated=False):
        safe_url(url, authenticated)
        for attempt in range(4):
            try:
                response = self.session.get(
                    url,
                    headers={"Authorization": f"Key {self.key}"} if authenticated else {},
                    timeout=(10, 90),
                    allow_redirects=False,
                    stream=True,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 3:
                    raise ValueError("fal read failed; the saved request can be resumed") from None
            else:
                if 200 <= response.status_code < 300:
                    return response
                code = response.status_code
                response.close()
                if code not in TRANSIENT or attempt == 3:
                    raise ValueError(f"fal read returned HTTP {code}")
            time.sleep(0.5 * 2**attempt)
        raise ValueError("fal read failed")

    def json(self, url):
        with self.get(url, authenticated=True) as response:
            return response.json()

    def download(self, url, output, max_bytes=512 * 1024 * 1024):
        """Bound disk usage, stream bytes, and replace only after MP4 validation."""
        part = output.with_suffix(".mp4.part")
        if part.is_symlink():
            raise ValueError("A download temporary path cannot be a symlink")
        output.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            with self.get(url) as response, part.open("wb") as handle:
                for block in response.iter_content(1024 * 1024):
                    total += len(block)
                    if total > max_bytes:
                        raise ValueError("Video exceeded the download size limit")
                    handle.write(block)
            with part.open("rb") as handle:
                header = handle.read(64)
            if total < 1000 or b"ftyp" not in header[:32]:
                raise ValueError("Downloaded content is not an MP4")
            os.replace(part, output)
        finally:
            part.unlink(missing_ok=True)
        return total


def request_urls(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise ValueError("Invalid request ID")
    root = f"{QUEUE}/minimax/h3-max-turbo/requests/{request_id}"
    return {"request_id": request_id, "status_url": root + "/status", "response_url": root}


def run_job(workspace, job, client, timeout_seconds=600):
    state_name = f"state/{identifier(job['id'])}.json"
    fingerprint = input_hash(workspace, job)
    state = (
        workspace.load(state_name)
        if workspace.path(state_name).exists()
        else {"id": job["id"], "input_hash": fingerprint}
    )
    if state.get("input_hash") != fingerprint:
        raise ValueError("Job inputs changed; use a new workspace or restore the original plan")
    output = workspace.path(job["output"])
    if (
        state.get("status") == "complete"
        and output.is_file()
        and sha256(output) == state.get("sha256")
    ):
        return "cached"
    if not state.get("request_id"):
        if state.get("submit_started_at"):
            raise ValueError(
                "Submission outcome is uncertain; use attach-request after checking fal history"
            )
        arguments = dict(job["input"])
        for field, api_name in (("first_frame", "image_url"), ("last_frame", "end_image_url")):
            if field in job:
                path = workspace.path(job[field])
                mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
                arguments[api_name] = (
                    f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
                )
        state.update(status="submitting", submit_started_at=utc_now())
        workspace.save(state_name, state)
        try:
            receipt = client.submit(job["endpoint"], arguments)
            # Canonical URLs are derived from the ID; returned tracking URLs never
            # gain control of the destination of an authenticated request.
            state.update(request_urls(receipt["request_id"]))
        except Exception:
            state["status"] = "submission_uncertain"
            workspace.save(state_name, state)
            raise ValueError(
                "Submission was not confirmed; it will not be retried automatically"
            ) from None
        state["status"] = "submitted"
        workspace.save(state_name, state)
    else:
        state.update(request_urls(state["request_id"]))
    deadline = time.monotonic() + timeout_seconds
    while not state.get("video_url"):
        status = client.json(state["status_url"]).get("status")
        if status == "COMPLETED":
            result = client.json(state["response_url"])
            url = safe_url(result.get("video", {}).get("url"))
            inference = (result.get("timings") or {}).get("inference")
            state.update(video_url=url, status="generated", completed_at=utc_now())
            if isinstance(inference, (float, int)) and 0 <= inference < 86400:
                state["inference_seconds"] = float(inference)
            workspace.save(state_name, state)
            break
        if status not in {"IN_QUEUE", "IN_PROGRESS"}:
            raise ValueError(
                "Unexpected queue status; the saved request can be inspected and resumed"
            )
        if time.monotonic() >= deadline:
            raise ValueError(
                "Polling timed out; rerun the same command to resume the saved request"
            )
        time.sleep(0.75)
    state["bytes"] = client.download(state["video_url"], output)
    state.update(status="complete", sha256=sha256(output), finished_at=utc_now())
    workspace.save(state_name, state)
    return "complete"


def generate(workspace, group, *, execute=False, key=None, concurrency=6, limit=None):
    if not 1 <= concurrency <= 16:
        raise ValueError("Concurrency must be between 1 and 16")
    jobs = jobs_for(workspace, group)
    if limit is not None:
        if limit < 1:
            raise ValueError("Limit must be positive")
        jobs = jobs[:limit]
    summary = {
        "group": group,
        "jobs": len(jobs),
        "requested_video_seconds": sum(j["input"]["duration"] for j in jobs),
        "execute": execute,
        "complete": 0,
        "cached": 0,
        "failed": 0,
    }
    if not execute:
        return summary
    if not key:
        raise ValueError("FAL_KEY is required for execution")
    stop = threading.Event()
    guard = threading.Lock()
    iterator = iter(jobs)
    errors = []

    def worker():
        client = QueueClient(key)
        try:
            while not stop.is_set():
                with guard:
                    job = next(iterator, None)
                if job is None:
                    return
                try:
                    outcome = run_job(workspace, job, client)
                except Exception:
                    stop.set()
                    with guard:
                        summary["failed"] += 1
                        errors.append(job["id"])
                    # Errors intentionally omit response bodies, URLs, keys, and
                    # third-party exception strings. State remains resumable.
                else:
                    with guard:
                        summary[outcome] += 1
                    print(json.dumps({"job": job["id"], "status": outcome}), flush=True)
        finally:
            client.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(concurrency, len(jobs))) as pool:
        list(pool.map(lambda _: worker(), range(min(concurrency, len(jobs)))))
    summary["failed_jobs"] = errors
    workspace.save(f"reports/{group}-generation.json", summary)
    if errors:
        raise ValueError(
            "Generation paused after a job error; inspect local state and docs/operations.md"
        )
    return summary


def attach_request(workspace, group, job_id, request_id):
    job = next((j for j in jobs_for(workspace, group) if j["id"] == identifier(job_id)), None)
    if job is None:
        raise ValueError("Job is not in this group")
    name = f"state/{job_id}.json"
    old = workspace.load(name) if workspace.path(name).exists() else {}
    if old.get("request_id") and old["request_id"] != request_id:
        raise ValueError("This job already has a different request ID")
    if old.get("input_hash", input_hash(workspace, job)) != input_hash(workspace, job):
        raise ValueError("Restore the original job inputs before attaching a request")
    old.update(
        id=job_id,
        input_hash=input_hash(workspace, job),
        status="submitted",
        **request_urls(request_id),
    )
    workspace.save(name, old)
    return {"job": job_id, "status": "attached; no request submitted"}
