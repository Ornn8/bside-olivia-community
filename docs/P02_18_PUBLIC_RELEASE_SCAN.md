# P02-18 Public Persona Release Scan

`baseline_hardening_scan.py --mode persona-release` checks tracked persona and
content assets before public distribution. Findings contain only a relative
path and a stable label; matched text and private values are never printed.

The scan rejects private reference/state/communication paths, control-view and
Local Continuation instances, private nickname grants, blocked rights records,
overlong copied source text, and unreadable release assets. `--mode all`
includes this boundary.

`linli_character/persona_v2.json` is an auditable source registry, not a
release payload. It may retain explicitly quarantined
`allowed_public_release=false` rows so the loader can fail closed. Those rows
must not be copied into the release payload created by P02-19. All other
persona assets are checked as release candidates and blocked rows fail the
scan.

Rollback is removal of the `persona-release` mode and its call from `all`; it
does not modify runtime Persona assembly, state, or media behavior.
