# P02-12 PrivateWorld reducer

`reduce_private_world` is a pure function from an immutable snapshot and typed
event to a new snapshot plus an explainable field delta. Boundary respect,
conflict, and repair use small fixed changes; score bounds are 0–100.

High-frequency messages, gifts, repeated phrases, one-off confessions, and
inactivity never upgrade or decay the relationship. A stage changes only after
an explicit `stage_confirmed` event with evidence identifiers. Equivalent
semantic events inside the fixed 24-hour window are no-ops.

This slice has no database, clock read, model call, or runtime wiring.
