# P02-14 PrivateWorld local administration

Run `python -m private_world_admin --database <file> <command> --yes` to perform
an explicit local operation. Commands are `export --output <file>`, `reset`, and
`delete`. Omitting `--yes` changes nothing and returns `CONFIRMATION_REQUIRED`.

Export uses a same-directory temporary file plus atomic replacement. There is no
default export path, automatic repository write, log upload, Release attachment,
or product-interface exposure. Reset preserves an empty database; delete removes
the database and its SQLite sidecars. Console output contains stable codes only.
