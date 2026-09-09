from unittest.mock import Mock

import pytest
import requests

from videomosaic.generation import QueueClient, attach_request, generate, run_job, safe_url
from videomosaic.plan import jobs_for, prepare, validate_job
from videomosaic.storage import Settings, Workspace


class Reply:
    def __init__(self, *, status=200, body=None, blocks=None):
        self.status_code = status
        self.body = body or {}
        self.blocks = blocks or []
        self.closed = False

    def json(self):
        return self.body

    def iter_content(self, size):
        yield from self.blocks

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://queue.fal.run/a",
        "https://queue.fal.run.evil.test/a",
        "https://queue.fal.run@evil.test/a",
        "https://queue.fal.run:444/a",
        "https://queue.fal.run/a#fragment",
        "https://localhost/a",
        "https://127.0.0.1/a",
        "https://evil.test/a",
    ],
)
def test_authenticated_destinations_are_pinned(url):
    with pytest.raises(ValueError):
        safe_url(url, authenticated=True)


def test_key_only_sent_to_queue_and_redirects_disabled(monkeypatch):
    client = QueueClient("unit-test-key")
    assert client.session.trust_env is False
    transport = Mock(return_value=Reply())
    monkeypatch.setattr(client.session, "get", transport)
    client.get("https://queue.fal.run/minimax/job", authenticated=True).close()
    client.get("https://v3b.fal.media/files/movie.mp4").close()
    first, second = transport.call_args_list
    assert first.kwargs["headers"] == {"Authorization": "Key unit-test-key"}
    assert second.kwargs["headers"] == {}
    assert not first.kwargs["allow_redirects"] and not second.kwargs["allow_redirects"]
    client.close()


def test_queue_redirect_is_not_followed(monkeypatch):
    client = QueueClient("unit-test-key")
    reply = Reply(status=302)
    monkeypatch.setattr(client.session, "get", Mock(return_value=reply))
    with pytest.raises(ValueError, match="HTTP 302"):
        client.get("https://queue.fal.run/job", authenticated=True)
    assert reply.closed
    assert client.session.get.call_count == 1
    client.close()


def test_uncertain_post_is_never_repeated(workspace):
    job = jobs_for(workspace, "tiles")[0]
    client = Mock()

    def uncertain(*args):
        assert workspace.load(f"state/{job['id']}.json")["status"] == "submitting"
        raise requests.Timeout("transport failed")

    client.submit.side_effect = uncertain
    with pytest.raises(ValueError, match="not confirmed"):
        run_job(workspace, job, client)
    with pytest.raises(ValueError, match="uncertain"):
        run_job(workspace, job, client)
    assert client.submit.call_count == 1


def test_download_failure_resumes_same_request(workspace):
    job = jobs_for(workspace, "tiles")[0]
    client = Mock()
    client.submit.return_value = {"request_id": "known-id", "status_url": "https://evil.test/steal"}
    client.json.side_effect = [
        {"status": "COMPLETED"},
        {"video": {"url": "https://v3b.fal.media/movie.mp4"}},
    ]
    client.download.side_effect = ValueError("interrupted download")
    with pytest.raises(ValueError, match="interrupted"):
        run_job(workspace, job, client)

    def download(url, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fixture-movie")
        return output.stat().st_size

    client.download.side_effect = download
    assert run_job(workspace, job, client) == "complete"
    assert run_job(workspace, job, client) == "cached"
    workspace.path(job["output"]).write_bytes(b"corrupted")
    assert run_job(workspace, job, client) == "complete"
    assert client.submit.call_count == 1
    assert client.json.call_count == 2
    assert all(
        call.args[0].startswith("https://queue.fal.run/") for call in client.json.call_args_list
    )
    job["input"]["prompt"] += " changed"
    with pytest.raises(ValueError, match="inputs changed"):
        run_job(workspace, job, client)


@pytest.mark.parametrize(
    "blocks,limit",
    [([b"<!doctype html>" * 100], 10000), ([b"\x00\x00\x00\x20ftyp" + b"x" * 2000], 100)],
)
def test_bad_or_oversized_download_keeps_existing_file(tmp_path, monkeypatch, blocks, limit):
    output = tmp_path / "movie.mp4"
    output.write_bytes(b"original")
    client = QueueClient("unit-test-key")
    monkeypatch.setattr(client, "get", Mock(return_value=Reply(blocks=blocks)))
    with pytest.raises(ValueError):
        client.download("https://v3b.fal.media/movie.mp4", output, limit)
    assert output.read_bytes() == b"original"
    assert not output.with_suffix(".mp4.part").exists()
    client.close()


def test_attach_request_is_local_and_dry_run_needs_no_key(workspace):
    job = jobs_for(workspace, "tiles")[0]
    report = generate(workspace, "tiles")
    assert report["jobs"] == 4 and report["execute"] is False
    attach_request(workspace, "tiles", job["id"], "existing-request")
    assert workspace.load(f"state/{job['id']}.json")["request_id"] == "existing-request"
    with pytest.raises(ValueError, match="different request"):
        attach_request(workspace, "tiles", job["id"], "another-request")


def test_catalog_is_256_unique_480p_movies(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.initialize(Settings())
    prepare(workspace)
    jobs = jobs_for(workspace, "tiles")
    assert len(jobs) == 256
    assert len({j["input"]["prompt"] for j in jobs}) == 256
    assert {j["input"]["resolution"] for j in jobs} == {"480P"}
    assert all(j["input"]["enable_safety_checker"] for j in jobs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("enable_safety_checker", False),
        ("resolution", "1080P"),
        ("aspect_ratio", "16:9"),
        ("image_url", "https://evil.test/image.png"),
        ("duration", 6),
    ],
)
def test_job_validation(workspace, field, value):
    job = jobs_for(workspace, "tiles")[0]
    job["input"][field] = value
    with pytest.raises(ValueError):
        validate_job(workspace, job)
