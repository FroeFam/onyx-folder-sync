# onyx-folder-sync

Keeps local or mounted folders in sync with an [Onyx](https://github.com/onyx-dot-app/onyx)
knowledge base. It walks the folders you configure, extracts the text of each
supported file, and pushes it to Onyx through the Onyx Ingestion API. Later runs
push only what changed and remove documents whose files are gone.

This is an unofficial community tool. It is not affiliated with or endorsed by
the Onyx project.

## Status

Early development. Nothing here is usable yet.

## Why a separate tool

Onyx connectors are compiled into Onyx itself: there is no plugin mechanism for
adding one from outside, and Onyx ships no connector for a plain folder. This
tool runs next to Onyx and uses only its public API, so it needs no fork and no
change to your Onyx installation.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## License

MIT
