# P02-10 PrivateWorld contract

`PrivateWorldPort` separates private relationship state from ordinary memory.
Its persistent snapshot and control view may contain bounded hidden values; the
character view omits every hidden numeric value and exposes only explicit,
finite permissions and relationship labels.

`NullPrivateWorldPort` is the disabled default. It returns an immutable empty
snapshot, does not persist or mutate files, and does not call a provider.

This slice defines contracts only. P02-11 owns the SQLite event ledger, P02-12
owns reduction, and P02-13 owns behavioral projection. No runtime pipeline or
model integration is included here.
