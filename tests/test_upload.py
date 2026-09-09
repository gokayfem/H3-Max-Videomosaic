from unittest.mock import Mock

import fal_client
import pytest
import requests
from test_generation import Reply

from videomosaic.storage import Workspace, sha256
from videomosaic.upload import publish


def test_upload_is_checkpointed_and_public_read_has_no_key(tmp_path, movies, monkeypatch):
    reference, _ = movies
    workspace = Workspace(tmp_path)
    workspace.path("movie.mp4").write_bytes(reference.read_bytes())
    sdk = Mock()
    sdk.upload_file.return_value = "https://v3b.fal.media/example.mp4"
    constructor = Mock(return_value=sdk)
    monkeypatch.setattr(fal_client, "SyncClient", constructor)
    calls = []

    def public_read(session, url, **kwargs):
        assert session.trust_env is False
        calls.append(kwargs)
        return Reply(status=206, blocks=[b"\x00\x00\x00\x20ftyp" + b"x" * 56])

    monkeypatch.setattr(requests.Session, "get", public_read)
    assert publish(workspace, "movie.mp4")["execute"] is False
    assert constructor.call_count == 0
    report = publish(workspace, "movie.mp4", execute=True, key="unit-test-key")
    assert report["public_read_verified"] is True
    publish(workspace, "movie.mp4", execute=True, key="unit-test-key")
    assert sdk.upload_file.call_count == 1
    assert all(
        call["headers"] == {"Range": "bytes=0-63"} and not call["allow_redirects"] for call in calls
    )


def test_upload_timeout_does_not_repeat_publication(tmp_path, movies, monkeypatch):
    reference, _ = movies
    workspace = Workspace(tmp_path)
    path = workspace.path("movie.mp4")
    path.write_bytes(reference.read_bytes())
    sdk = Mock()
    sdk.upload_file.side_effect = TimeoutError("uncertain")
    monkeypatch.setattr(fal_client, "SyncClient", Mock(return_value=sdk))
    with pytest.raises(ValueError, match="not confirmed"):
        publish(workspace, "movie.mp4", execute=True, key="unit-test-key")
    with pytest.raises(ValueError, match="uncertain"):
        publish(workspace, "movie.mp4", execute=True, key="unit-test-key")
    assert sdk.upload_file.call_count == 1
    assert workspace.load(f"state/upload-{sha256(path)}.json")["started"] is True
