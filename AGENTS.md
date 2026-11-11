# Guide for AI agents

onyx-folder-sync is a one-way sync of local or mounted folders into an Onyx
knowledge base through the Onyx Ingestion API. It pushes extracted text, and
it deletes documents from Onyx when their files are gone. The deletes are the
dangerous part: a wrong delete removes a user's document from their search
index. Most rules below exist to prevent that.

Read `README.md` for what the tool does from a user's point of view.

## Hard rules

1. **Never run against a real Onyx or a real share.** Use the fake server in
   `tests/fake_onyx/` and fixture trees from `tests/fixtures.py`. Real
   integration runs are done by the repository owner only.
2. **No secrets in the repo.** The API token comes from `ONYX_TOKEN` or
   `ONYX_TOKEN_FILE`. The config loader rejects any key with "token" in its
   name. Never log the token or request headers.
3. **Do not weaken or remove a test in `tests/invariants/`.** If one looks
   wrong, stop and report it to the owner with the reason. Adding invariant
   tests is welcome.
4. **`models.py`, `interfaces.py` and `schema.sql` are the shared contract.**
   Change them only when the task needs it, and say so in the PR text.
5. **Do not merge to `main`, tag or release** unless the owner asks.
6. **ASCII only** in code, config, scripts and comments.

## Commands

The project uses `uv` (version range in `pyproject.toml`). All four gates
must pass before a commit:

```
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
```

- `mypy` runs in strict mode over `src` and `tests`.
- `pytest` turns every warning into an error (`filterwarnings = error`).
- The full suite takes about two minutes. Run one file while working:
  `uv run pytest -q tests/unit/test_walk.py`.
- CI runs the same gates on Linux for Python 3.12, 3.13 and 3.14. Some tests
  are skipped per platform (symlinks and the memory limit on Windows,
  case-insensitive paths on Linux), so a green local run on one platform does
  not prove the other.

## Layout

```
src/onyx_folder_sync/
  models.py        constants, config models, records, exit codes, skip reasons
  interfaces.py    errors, Protocols (StateStore, OnyxApi, ...), SyncDeps
  schema.sql       SQLite schema of the state database
  config.py        YAML loading, root overlap check, token lookup
  filters.py       built-in excludes, config globs, .onyxignore
  walk.py          directory walk; reports eligible, skipped, errored dirs
  state.py         SQLite state store (WAL, synchronous FULL)
  lock.py          one run at a time per state file
  extract/         text extraction in a separate worker process
  onyx_client.py   HTTP client for the Onyx API
  ids.py           document id: "fc_" + 32 hex of SHA-256(root id, rel path)
  guard.py         delete guard arithmetic
  engine.py        run_sync: preflight, walk, diff, push, guarded deletes
  reconcile.py     compare state with what Onyx holds
  roots.py         forget and rename-root
  cli.py           typer app; main(argv) returns the exit code
tests/
  fake_onyx/       in-process fake Onyx HTTP server with fault injection
  fixtures.py      TreeBuilder, write_config, failing_scandir
  unit/            one module at a time
  integration/     CLI commands end to end against the fake server
  invariants/      safety invariants I1 to I11 and fault-injection scenarios
  samples/         small sample files per format (binary, do not reformat)
```

## How a sync run works

1. Preflight: Onyx reachable, token valid, each root has a verified connector
   entry. Onyx unreachable at preflight is exit 0 (nothing was done).
2. Walk **all** roots before any write. A root that is unavailable or has
   zero eligible files stops the whole run with exit 2.
3. Diff the walk against state: missing counts, deliberate skips.
4. Per eligible file: settle check, extract, hash, push when changed. A
   "push pending" row with an empty payload hash is written before each push.
5. Deletes last, all under the delete guard.

## Safety rules the code must keep

- **Unknown is never absent.** A directory that could not be read marks its
  whole subtree unknown; nothing under it counts as missing.
- **Two consecutive walks.** A document is deleted for absence only after its
  file was missing in two consecutive complete walks. A file that comes back
  resets the count, in state and in the rows held in memory.
- **Deliberate skips** (excluded, wrong extension, too large, no text, text
  too large) delete the document in the same run, under the guard. Every
  other skip reason keeps the document.
- **Delete guard.** 10 deletes per root are free, above that
  `max_delete_fraction` applies. One blocked root blocks every delete of the
  run (exit 3). `--allow-deletes N` works only when N equals the total number
  of pending deletes.
- **Ownership.** The tool deletes only ids it derived itself for a row in
  state. Use `may_be_in_onyx(row)`, not `row.has_document`, to decide whether
  Onyx can hold a document: a push that was never confirmed counts too.
- **Pushed means 2xx.** `onyx_id` is set only after Onyx answered 2xx.
- **Dry run writes nothing**: no state, no lock file, no write requests.

When you change `engine.py`, check each of these against your change and add
a test for the case you touched. Two bugs found in review were both of the
form "one code path read a stale or weaker condition than its neighbours".

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, or Onyx unreachable at preflight |
| 1 | Some documents failed to push or delete |
| 2 | Config or state problem, root unavailable or empty |
| 3 | Delete guard blocked the deletes |
| 4 | Token rejected |
| 5 | Another run holds the lock |

Extraction errors count as skipped, not failed, so one corrupt file does not
make every run exit 1.

## Things that trip people up

- `typer` bundles its own click as `typer._click`. `cli.main` matches its
  exceptions by shape, not by importing `click`.
- `extract_memory_mb` is an address space limit (Linux and macOS). The
  default is 2048, because the extraction libraries reserve more than 1 GB.
- Document ids derive from the root id stored in state. A lost state file
  cannot be repaired by `reconcile`; the README describes the recovery.
- `reconcile` is the one command that deletes `fc_` ids missing from state,
  inside the root's own verified entry and under the guard.
- Extensions outside the built-in list have no extractor and give an
  extraction error per file.
- `tests/samples/*` are stored without line ending conversion
  (`.gitattributes`). Regenerate them with `tests/make_samples.py`.
- Relative paths in state use forward slashes and are NFC-normalized.

## Pull requests

- Branch from `main`; one PR per change. CI classifies the SemVer label and
  runs an automated review. Read its inline comments and fix or answer each.
- Keep commit messages plain: what changed and why.
