# Operations and recovery

## Setup

Python 3.12 is tested in CI on Linux and Windows. Install FFmpeg with `libx264`
support. If it is not on PATH, set `FFMPEG_PATH` to its executable. FFmpeg is
called directly, without a shell.

```sh
python -m pip install --upgrade pip
python -m pip install -c constraints.txt -e ".[dev,upload]"
videomosaic --help
```

Only `generate --execute` and `upload --execute` contact fal. Other commands are
local. To try new prompts with fewer requests:

```sh
videomosaic init --workspace runs/pilot --tiles 16
videomosaic generate --workspace runs/pilot --group tiles
```

The preview reports request count and requested video seconds, not a price quote.
Check the endpoint's current pricing before executing. The default base plan is
257 jobs: 256 source movies and a separate portrait. Guided variants are extra.

Original sources must be square 480×480, constant-frame-rate MP4s with enough
frames for the configured duration. The portrait may use a larger canvas. Audio
is ignored; exports are silent. Five seconds and 24 fps reproduce the demonstrated
cadence. Raising output fps does not invent additional source motion.

## Credentials and run files

Set `FAL_KEY` in the environment or pass an explicit `--env-file .env`. An
environment value wins. The parser only reads `FAL_KEY`; it does not evaluate
shell syntax, interpolate variables, or search parent folders. Never put the key
in a prompt, job plan, command-line argument, or GitHub issue.

Keep workspaces under the ignored `runs/` directory. Add your own ignore rule if
you choose another location. Local plans and receipts may contain private prompts,
request IDs, and media URLs even though they do not contain the authentication key.

## Resume generation

Rerun the same command against the same workspace. Completed files are checked
against their SHA-256. Pending request IDs are polled; missing or damaged files
are downloaded from the same result, without submitting another generation.

The client makes **one submission attempt** per job. A timeout can occur after
fal accepts a paid request, so the client saves a marker before the POST and never
automatically retries an uncertain submission. Read-only requests use bounded
retries. If a receipt was lost, find the matching request in fal's request history
and attach it to the unchanged job:

```sh
videomosaic attach-request --workspace runs/portrait --group tiles --job tile-000 --request-id REQUEST_ID_FROM_FAL
videomosaic generate --workspace runs/portrait --group tiles --env-file .env --execute
```

Confirm that the request belongs to this endpoint and these inputs. If fal
confirms no request was accepted, you may explicitly remove only that job's
`state/<job-id>.json` before retrying. Removing state without checking can incur
duplicate charges. Changed submitted inputs are rejected; use a new workspace
for a different experiment.

Concurrency defaults to 6 and is limited to 16. A failed worker stops the batch
from scheduling more jobs; active workers finish or save their state. A poll has
a ten-minute deadline. Rerunning resumes it.

## Locks and interrupted local work

One writer may operate on a workspace at a time. `.videomosaic.lock` contains its
process ID. Normal completion, handled errors, and Ctrl+C remove it. After forced
termination, verify the recorded process has stopped before manually removing
that specific lock. Also inspect its command because process IDs can be reused.

JSON state, caches, downloads, and encoded videos use temporary files and atomic
replacement. A failed export keeps an existing video intact. Use `--overwrite`
only when replacing it deliberately. Fingerprints reject stale analysis and
assignments after inputs change; rerun `analyze` and `match`.

Guided selection backs up the original manifests to
`reports/pre-guidance-sources.json` and `reports/pre-guidance-assignment.json`.
After forced termination during its multi-file cache rebuild, restore these as
`sources.json` and `assignment.json`, remove a partially completed
`reports/guidance-selection.json` if present, then run `analyze` and
`select-guided`. Rebuilding caches does not require regenerating paid variants.

## Rendering resources and verification

The 64×36 grid reuses the source library across 2,304 cells. Distant views use
file-backed 64px proxies; medium views lazily create 192px proxies. Close views
stream original MP4 frames. At most 96 native decoder streams are open, each
using one decoder thread.

A 256-source, five-second, 24 fps run needs about 0.38 GB for all 64px proxies and
up to 3.4 GB for 192px proxies, plus source MP4s and exports. Allow at least 10 GB
of free disk. RAM and encoding time depend on output size, OS file caching, and
hardware. No local GPU is required. Longer clips and larger grids increase usage.

`wide` and `reveal` use the source duration. `showcase` is a 32-second camera tour
with half-second crossfades at loop joins, repeating the generated action.

Each successful render is decoded completely to check frame count, dimensions,
fps, nonblack frames, and measurable motion. Reports and contact sheets live in
`reports/`. Watch the video as well: those checks cannot judge all visual artifacts
or whether a new reference is recognizable.

## Public upload

`upload --execute` sends the chosen MP4 to fal storage, returns its shareable CDN
URL, and checks the public MP4 header. It does not post to social accounts.

The same file hash reuses its saved URL. If a public read fails, rerun to verify
the URL without uploading again. An upload started without a confirmed URL stays
uncertain; inspect fal storage before explicitly clearing its
`state/upload-<sha256>.json`. Storage retention follows fal's policies. Keep a
local master.
