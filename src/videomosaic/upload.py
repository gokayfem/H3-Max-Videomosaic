"""Explicit, checkpointed publication of a chosen MP4 to fal CDN."""

import requests

from .generation import safe_url
from .media import verify_video
from .storage import sha256


def publish(workspace, relative, *, execute=False, key=None):
    path = workspace.path(relative)
    if path.suffix.lower() != ".mp4" or not path.is_file():
        raise ValueError("Choose an existing MP4 inside the workspace")
    digest = sha256(path)
    report = {"file": relative, "sha256": digest, "bytes": path.stat().st_size, "execute": execute}
    if not execute:
        return report
    if not key:
        raise ValueError("FAL_KEY is required for upload")
    verify_video(path)
    name = f"state/upload-{digest}.json"
    state = workspace.load(name) if workspace.path(name).exists() else {}
    if not state.get("url"):
        if state.get("started"):
            raise ValueError(
                "Upload outcome is uncertain; inspect fal storage before explicitly clearing this upload state"
            )
        try:
            from fal_client import SyncClient
        except ImportError:
            raise ValueError('Install the upload extra: pip install -e ".[upload]"') from None
        workspace.save(name, {**report, "started": True})
        try:
            url = safe_url(SyncClient(key=key).upload_file(path))
        except Exception:
            raise ValueError(
                "Upload was not confirmed; its state is saved and will not be retried automatically"
            ) from None
        state = {**report, "started": True, "url": url}
        workspace.save(name, state)
    url = safe_url(state["url"])
    # A public read has no Authorization header and never follows redirects.
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(
                url,
                headers={"Range": "bytes=0-63"},
                stream=True,
                timeout=(10, 30),
                allow_redirects=False,
            ) as response:
                if response.status_code not in {200, 206}:
                    raise ValueError(
                        "Uploaded video is not publicly readable yet; rerun upload to verify the saved URL"
                    )
                prefix = next(response.iter_content(64), b"")
                if b"ftyp" not in prefix[:32]:
                    raise ValueError("Public upload does not have an MP4 header")
    except requests.RequestException:
        raise ValueError(
            "Public upload check failed; rerun upload to verify the saved URL"
        ) from None
    state["public_read_verified"] = True
    workspace.save(name, state)
    return state
