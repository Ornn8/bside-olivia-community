# P02-13 PrivateWorld behavior projection

`project_private_world` is a pure, provider-free projection. It converts hidden
numeric state into the finite `PrivateBehaviorView` enums already accepted by
`ReplyContext`; raw values and the complete snapshot never enter its payload.

Only current nickname permissions are returned. `control_only` and `pending`
Local Continuation awareness both project to `continuation_known=false`, without
exposing their control labels. Home access stays outside the model payload and
only grants the assembly layer permission to acknowledge matching trusted
history; it does not instruct the character to mention, invent, or describe a
home scene.
