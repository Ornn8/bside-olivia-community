# P02-11 PrivateWorld SQLite ledger

`SQLitePrivateWorldLedger` stores PrivateWorld data in an explicit, separate
SQLite file. `apply_once` atomically appends one typed event and its caller-
provided snapshot. Both `event_id` and `delivery_id` are unique, so a delivery
retry cannot apply the same relationship change twice.

The ledger does not reduce events, infer relationship changes, call a model, or
join the ordinary memory database. P02-12 owns the pure reducer. Public health
contains only status and row counts; it never exposes a path or private value.
