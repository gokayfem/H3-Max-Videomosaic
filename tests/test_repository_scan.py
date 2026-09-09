import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "repository_scan", Path(__file__).resolve().parents[1] / "scripts/check_repository.py"
)
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


def test_scan_blocks_sensitive_artifacts_and_synthetic_key():
    assert scan.violations(".env", b"")
    assert scan.violations("runs/private.json", b"{}")
    assert scan.violations("media/movie.mp4", b"")
    value = ("FAL_" + "KEY='" + "synthetic" * 8 + "'\n").encode()
    assert scan.violations("accidental.txt", value)
    assert scan.violations("accidental.txt", ("ghp_" + "s" * 40).encode())


def test_scan_allows_empty_template_and_documented_key_name():
    assert not scan.violations(".env.example", b"FAL_KEY=\n# Optional configuration\n")
    assert not scan.violations("README.md", b"Set FAL_KEY in the environment.")
