# Contributing

Keep changes focused and preserve offline defaults. Tests must not call paid
endpoints or require a fal key. Use fake transports and synthetic footage.

```sh
python -m pip install --upgrade pip
python -m pip install -c constraints.txt -e ".[dev,upload]"
ruff check .
ruff format --check .
pytest
python -m build
python scripts/check_repository.py
```

Install FFmpeg before testing. Media tests skip locally if it is absent; CI
installs it explicitly. Changes to matching, cameras, or color correction also
need visual inspection: check the contact sheet, then watch close and full views.

Update the endpoint schema link and verification date when changing model inputs.
Preserve credential routing, request recovery, and publication defaults with
regression tests. Keep generated state and machine paths out of source.

`constraints.txt` records tested dependency versions. Refresh it deliberately
after tests and `pip-audit`; do not commit editable-install paths.
