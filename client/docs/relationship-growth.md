# Relationship growth

Live interactions retain distinct evidence-based effects: meaningful conversation increases familiarity and comfort; shared experience can also increase trust and closeness. All positive score changes use the existing seven-day growth budget. Repair may still reduce tension after that budget is exhausted. Stage confirmation records the explicit relationship fact without awarding score bonuses, including when the stage is lowered.

New closeness gains cannot exceed the lower of trust and comfort. This does not lower existing scores or equate familiarity with trust. Permission records and boundary facts remain independent of score growth.

New historical-import commands carry familiarity, trust, comfort, closeness and tension from the same assessment. Closeness from that assessment is limited by its trust and comfort evidence. Historical imports are evidence baselines, not weekly live interactions. They merge conservatively without reducing existing live state or assigning a relationship stage. Legacy two-axis commands remain decodable for compatibility; the current importer always sends the complete assessment. Existing corpus audit keys and user data are not reset, so this change does not automatically reassess previously imported histories.

The days of a history count like the days of live chats. Each assessed batch carries the calendar days (UTC+8) of its exchanges whose time is known; applying it merges them into the days the two talked and recomputes the story volume and the effective stage from that span (`history_day_updates`). Exchanges without a time and days after today add nothing, nothing is removed, and a reached volume is never lowered. Live chats keep adding only their own day. Histories assessed by earlier versions get their days once their whole re-read is done: an explicit checkpoint (`relationship_history_days`, written once per set of assessed batches) that letter deletion derives again on a rebuilt state.

The history re-read assesses one batch at a time and waits while a reply is being written (at most five minutes). A refusal that passes (busy, a server error, no answer in time) is tried again after 15 s, 40 s, 2 min and 5 min before the batch is marked failed; such a batch is taken up again whenever the re-read starts (startup, an import, a retry). Any other failure waits for the explicit retry.

Tests use synthetic interactions and SQLite ledgers. They do not constitute real-model or user-data reassessment acceptance.
