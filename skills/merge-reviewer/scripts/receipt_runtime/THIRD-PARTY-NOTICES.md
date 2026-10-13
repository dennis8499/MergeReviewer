# Bundled TOML parser

Tomli 2.4.1, copyright 2021 Taneli Hukkinen, is distributed under the MIT
license in `tomli/LICENSE`. These source files came from the Tomli copy bundled
with the installed pip distribution; their SHA-256 values are pinned in
`scripts/sync-receipt-runtime.py` and `source-manifest.json`.

Python 3.10 uses this bundled parser when `tomllib` is unavailable. Python 3.11
and later use the standard-library parser. Standalone receipt verification does
not require installing a third-party package.
