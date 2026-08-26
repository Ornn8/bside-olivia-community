# P02-14 PrivateWorld local administration

Run `python -m private_world_admin --database <file> <command> --yes` to perform
an explicit local operation. Commands are `export --output <file>`, `reset`, and
`delete`. Omitting `--yes` changes nothing and returns `CONFIRMATION_REQUIRED`.

Export uses a same-directory temporary file plus atomic replacement. There is no
default export path, automatic repository write, log upload, Release attachment,
or product-interface exposure. Reset preserves an empty database; delete removes
the database and its SQLite sidecars. Console output contains stable codes only.

The internal `reset_current_user(request_id=..., reason=..., confirmed=True)`
primitive operates only on the normalized current-user ledger selected below the
same local state root. It records the request fingerprint in that ledger: an
identical retry is `DUPLICATE`, while a reused request id with a different payload
is rejected as `PRIVATE_WORLD_ADMIN_REQUEST_CONFLICT`. It has no Settings UI yet.
