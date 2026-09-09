import json

import pytest

from videomosaic.storage import Settings, Workspace, atomic_json, load_key


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "a/../../secret",
        "/tmp/file",
        "C:/file",
        "C:file",
        "\\\\server\\file",
        "a\\b",
        ".",
        "",
    ],
)
def test_paths_cannot_escape(tmp_path, path):
    with pytest.raises(ValueError):
        Workspace(tmp_path / "run").path(path)


def test_symlink_escape(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    try:
        (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires a Windows privilege")
    with pytest.raises(ValueError):
        Workspace(root).path("escape/secret")


def test_atomic_json_preserves_previous_on_failure(tmp_path):
    path = tmp_path / "state.json"
    atomic_json(path, {"request_id": "known"})
    with pytest.raises(ValueError):
        atomic_json(path, {"invalid": float("nan")})
    assert json.loads(path.read_text()) == {"request_id": "known"}
    assert list(tmp_path.iterdir()) == [path]


def test_lock_excludes_writers_and_releases_after_error(workspace):
    with pytest.raises(RuntimeError), workspace.lock():
        with pytest.raises(ValueError, match="locked"), workspace.lock():
            pass
        raise RuntimeError("intentional")
    with workspace.lock():
        assert workspace.path(".videomosaic.lock").exists()
    assert not workspace.path(".videomosaic.lock").exists()


def test_env_file_is_explicit_and_never_executed(tmp_path, monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text("# test\nexport FAL_KEY='literal-$(do-not-run)'\nUNRELATED=value\n")
    with pytest.raises(ValueError):
        load_key()
    assert load_key(path) == "literal-$(do-not-run)"
    monkeypatch.setenv("FAL_KEY", "environment-wins")
    assert load_key(path) == "environment-wins"


@pytest.mark.parametrize(
    "values",
    [
        {"tiles": 0},
        {"tiles": True},
        {"tiles": 257},
        {"fps": 0},
        {"clip_seconds": 4},
        {"tiles": 10, "rows": 2, "columns": 2},
    ],
)
def test_settings_reject_invalid_bounds(values):
    with pytest.raises(ValueError):
        Settings(**values)
