# onyx-folder-sync

Keeps local or mounted folders (including SMB or NFS shares) in sync with an
[Onyx](https://github.com/onyx-dot-app/onyx) knowledge base. It walks the folders
you configure, extracts the text of each supported file and pushes it to Onyx
through the Onyx Ingestion API. Later runs push only what changed and remove
documents whose files are gone.

It is not a fork of Onyx and needs no change to your Onyx installation. It is a
scan, not a watcher: it looks at the folders when you run it. It does no OCR and
reads no images, audio, video or archives. It does not map file permissions: each
Onyx entry is public or restricted as a whole, through Onyx's own access settings.

This is an unofficial community tool. It is not affiliated with or endorsed by the
Onyx project.

## Requirements

Tested with Onyx v5.1.0. The tool needs a personal access token of an admin user
with the manage-connectors permission. It creates one connector entry per root,
pushes documents with `POST /api/onyx-api/ingestion` and removes them again when
files disappear.

## Quick start with Docker

The image is `ghcr.io/froefam/onyx-folder-sync`. The container runs as a non-root
user (uid 1000) and reads its config from `/config/config.yml`.

1. Copy `config.example.yml` to a local `config.yml` and edit it. Set `onyx.url`,
   keep `state_path: /state/onyx-folder-sync.db` and point each root `path` at a
   folder below `/data`.
2. Put the token in a file (for example `onyx_token`) or pass it as `ONYX_TOKEN`.
   The config file itself is rejected if it contains a token.
3. Mount the folder to index read-only under `/data/...`. Mount a volume at
   `/state`: it holds the SQLite state database and must be on a local disk, not
   a network share. The user inside the container needs write access to it (a
   Docker named volume works as is; for a bind mount, make the directory writable
   for uid 1000).

First look at what would happen. A dry run walks and diffs, extracts nothing and
writes nothing, and it works without a token:

```
docker run --rm \
  -v "$PWD/config.yml:/config/config.yml:ro" \
  -v "$PWD/onyx_token:/run/secrets/onyx_token:ro" \
  -e ONYX_TOKEN_FILE=/run/secrets/onyx_token \
  -v /volume1/OneDrive:/data/onedrive:ro \
  -v onyx-folder-sync-state:/state \
  ghcr.io/froefam/onyx-folder-sync sync --dry-run
```

Then run a real pass with the same options and `sync` instead of `sync --dry-run`.
The first pass indexes at roughly 12 KB of text per second on the test host, so a
large folder takes hours.

To keep it running, use a compose file with a loop:

```yaml
services:
  onyx-folder-sync:
    image: ghcr.io/froefam/onyx-folder-sync:latest
    restart: unless-stopped
    command: ["sync", "--loop", "--interval", "6h"]
    environment:
      ONYX_TOKEN_FILE: /run/secrets/onyx_token
    volumes:
      - ./config.yml:/config/config.yml:ro
      - ./onyx_token:/run/secrets/onyx_token:ro
      - /volume1/OneDrive:/data/onedrive:ro
      - state:/state
volumes:
  state:
```

### Run from source with uv

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```
git clone https://github.com/FroeFam/onyx-folder-sync
cd onyx-folder-sync
uv sync
export ONYX_TOKEN_FILE=/path/to/onyx_token
uv run onyx-folder-sync --config config.yml sync --dry-run
```

`--config` can be replaced by the `ONYX_FOLDER_SYNC_CONFIG` environment variable.

## Configuration reference

YAML. See `config.example.yml` for a commented file. Unknown keys are rejected.

| Key | Default | Meaning |
|---|---|---|
| `onyx.url` | required | Base URL of Onyx. |
| `onyx.request_pause_seconds` | 1 | Pause between two pushes. |
| `state_path` | required | SQLite state file. A path on a network filesystem is refused. |
| `defaults.max_file_mb` | 20 | Larger files are skipped (`too-large`). |
| `defaults.max_text_chars` | 2000000 | Longer extracted text is skipped (`text-too-large`). |
| `defaults.max_delete_fraction` | 0.25 | Delete guard threshold, see Safety. |
| `defaults.settle_minutes` | 5 | Files modified more recently are left for the next run (`settling`). |
| `defaults.extract_timeout_seconds` | 120 | Time limit for extracting one file. |
| `defaults.extract_memory_mb` | 2048 | Address space limit for the extraction process (Linux and macOS). This is address space, not resident memory: in the Docker image html extraction failed at 1024 and worked at 2048. Run `extract-report` on your files to check. |
| `defaults.extensions` | built-in list | Replaces the built-in list when set. |
| `roots[].name` | required | Unique name (compared case-insensitively). |
| `roots[].path` | required | Folder to index. Roots must not be equal, nested or overlapping. |
| `roots[].exclude` | none | Glob patterns in gitignore syntax. |
| `roots[].link_template` | none | Link stored with each document. `{relpath}` is the relative path, URL-encoded per segment. |

Token: `ONYX_TOKEN`, or `ONYX_TOKEN_FILE` naming a file that holds it. Never the
config file.

Default extensions: pdf, docx, pptx, xlsx, html, htm, eml, epub, txt, md, csv, tsv.

Config-like types that Onyx also accepts (conf, yml, yaml, json, xml, sql, log) are
off by default because they commonly hold credentials. Add them with
`defaults.extensions`, which replaces the list, so repeat the defaults you want.

### What is left out, in order

1. Built-in excludes: hidden entries (names starting with `.`), `.git`,
   `node_modules`, `@eaDir`, `#recycle`, `$RECYCLE.BIN`, `System Volume Information`,
   `Thumbs.db`, `.DS_Store` and Office lock files (`~$*`).
2. The root's `exclude` patterns.
3. `.onyxignore` files (gitignore syntax) in any folder. An `.onyxignore` that
   cannot be read makes its folder unknown for that run, so nothing below it is
   deleted.
4. The extension list.
5. The file size cap.
6. After extraction: empty text is `no-text`, text over `max_text_chars` is
   `text-too-large`.

Symlinks and junctions are not followed. Windows cloud placeholder files are never
opened and are reported as `placeholder`.

## Commands and exit codes

Global options: `--config <path>` (or `ONYX_FOLDER_SYNC_CONFIG`) and `--version`.

| Command | What it does |
|---|---|
| `sync` | One pass, then exit. |
| `sync --loop --interval 6h` | Repeat. The interval takes seconds or `s`, `m`, `h`, `d` suffixes. Default 6h. |
| `sync --dry-run` | Walk and diff only. Prints what would be pushed and deleted. Cannot be combined with `--loop`. |
| `sync --repush` | Ignore stored hashes and push everything again. |
| `sync --allow-deletes N` | Let a blocked delete guard through, for exactly N pending deletes. |
| `extract-report` | Extract all eligible files, push nothing, print counts per type (ok, no-text, failed, too-large) and the total text size. |
| `status` | Counts per root, the last run and any blocked delete state. Reads state only. |
| `forget <root> [--state-only]` | Remove the root's Onyx entry with its documents, and its state. `--state-only` drops only the local state. |
| `rename-root <old> <new>` | Rename a root in state. Document ids do not change. Change the name in the config too. |
| `reconcile <root> [--delete-excluded] [--allow-deletes N]` | Repair after documents were removed on the Onyx side or after state was restored. |

| Exit code | Meaning |
|---|---|
| 0 | Success, or Onyx unreachable at the start of the run (one log line, nothing done). |
| 1 | The run finished with document failures, or an Onyx request failed. |
| 2 | Configuration or state problem, including a missing root. |
| 3 | The delete guard blocked deletes. |
| 4 | Onyx rejected the token. |
| 5 | Another run holds the lock. |

With `--loop`, the loop continues after exit codes 0, 1 and 3 and stops on the
others.

## How it works

Change detection: a file whose size or modification time differs from state is
extracted again. The extracted payload (text, title, link, metadata) is hashed;
when the hash equals the stored one, nothing is pushed. State records a document as
pushed only after Onyx confirmed it. Files modified within the last
`settle_minutes` are skipped until a later run.

Document ids are `fc_` plus 32 hex characters of a SHA-256 over a random root id
(created once, kept in state) and the relative path. Renaming a root keeps its ids.
A rename or move of a file is a delete plus a new document.

Each root gets its own Onyx connector entry named `folder: <root> [<instance id>]`,
created by the tool with source `ingestion_api`. The instance id is generated once
per state file. Pointing a root at an existing entry is not supported.

A sync pass: take the lock, check that Onyx answers and accepts the token, check
that config, state and entries agree, walk every root (metadata only), diff against
state, extract new and changed files in a child process with a timeout, push one
document per request, then delete documents whose files are confirmed gone and print
a report. A file that had a document and now hits a deliberate skip (too large, no
text, newly excluded, extension removed) has its document deleted, under the same
guard.

## Safety behaviour

- A file is deleted from Onyx only after it was missing in two consecutive walks.
- Delete guard: up to 10 deletes per root per run are always allowed. A run that
  would delete more than `max_delete_fraction` of a root's documents performs no
  deletes, still pushes, and exits with code 3. Review with `sync --dry-run`, then
  run `sync --allow-deletes N` with exactly the pending number.
- A folder that could not be read during the walk is unknown for that run: nothing
  beneath it is deleted.
- A root that is missing or unmounted, or that has no eligible files while state
  holds documents, stops the run with exit code 2 before anything is pushed or
  deleted.
- Never switch on pruning for the entry in Onyx. Onyx pruning deletes every document
  the entry's own connector does not list, which includes all pushed documents. The
  tool refuses to run if the entry has pruning on, if its source is not
  `ingestion_api` or if its name carries a foreign instance id.
- The tool deletes only documents whose id starts with `fc_` and that belong to its
  own entry. If the file extraction fails or times out, the old document stays and
  the file is retried on the next run.
- State loss cannot be repaired by `reconcile`: ids derive from the root id kept in
  state. If the state file is gone, remove the old entry in the Onyx admin page and
  run `sync` again. `reconcile` is for documents removed on the Onyx side or an
  older restored state file that still holds the root.
- A second run while one is active exits with code 5 and changes nothing.
- The token is never written to logs or state.

## Limits and known issues

- An edit that keeps both file size and modification time is invisible to change
  detection. Run `sync --repush` about once a month.
- Folder names are visible in `metadata.path` to everyone who can see the entry.
- On Windows extraction has a timeout but no memory limit.
- The entry shows "Scheduled" forever in the Onyx admin page. This is cosmetic.
- Extraction errors keep the old document and are retried on every run.
- Pushing is sequential and Onyx indexes inside the request, so the first run on a
  large folder takes a long time.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests run against a fake Onyx server in `tests/fake_onyx/`; no real Onyx is needed.

## License

MIT
