# Security

This is a local CLI for your own fal account and trusted media. It is not a
multi-tenant service or a sandbox for hostile files.

- Generation and publication default to dry runs; both require `--execute`.
- Uncertain submissions are never automatically retried.
- Keys come from the environment or an explicit `.env`. The pipeline does not
  save them in plans, state, videos, or logs.
- Authenticated generation requests use a fixed HTTPS queue host without
  redirects. Public MP4 downloads receive no key and are restricted to fal media
  hosts, port 443, and 512 MiB per file.
- Workspace paths reject traversal, absolute paths, Windows drive/UNC paths, and
  symlinks resolving outside the selected workspace.
- NumPy loads disable pickle. FFmpeg uses an argument list, without a shell.
- Ignore rules exclude credentials, runs, media, caches, and receipts. Only two
  deliberate public preview JPEGs are tracked.
- CI has read-only repository permissions and pinned action revisions. Tests use
  fake transports and require no fal key.

Use trusted directories and media. Path checks cannot defend against another
process racing filesystem changes. Decoding uses native OpenCV/FFmpeg libraries;
keep them and Python dependencies updated. Avoid elevated privileges. An uploaded
CDN URL grants access to its file, so select that file deliberately.

Before committing, inspect staged files and run `python scripts/check_repository.py`.
A pattern scan catches common mistakes but cannot prove every secret is absent.
If a key leaks, revoke and replace it in fal. Deleting the latest Git copy does not
remove it from history.

Use GitHub's private vulnerability reporting if enabled. Otherwise, contact the
repository owner privately before publishing exploit details. Do not put live
keys, private media URLs, or account receipts in public issues.
