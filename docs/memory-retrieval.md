# Original-text retrieval

Canonical letters remain the durable source. An independent SQLite index preserves full user and assistant text, timestamps and source IDs, with overlapping original-text windows for retrieval. Chinese bigrams and Latin tokens support local FTS5 matching; semantic Mem0 hits locate original windows through source IDs. Reciprocal-rank fusion merges candidates, then overlapping/identical text is removed. At most five records enter the existing untrusted-memory renderer. Summary-only records remain available where no original survives.

New exchange indexing precedes paid fact extraction. The existing outbox also backfills completed canonical letters, including terminal deliveries, in batches of twenty successful indexes per scan without invoking extraction. Existing historical Archive references continue through their separate read-only path; reimported histories additionally populate the new index. Persona evidence and relationship state are not copied into this index.

Current memory and Archive share a default budget of 1500 locally estimated tokens, in addition to the existing character ceiling. The estimator charges half a token per ASCII character and UTF-8 bytes for non-ASCII characters. It is deliberately conservative and is not a provider tokenizer or a billing measurement. Rendering never cuts a current-memory fact merely to fit a budget. Recent source IDs are excluded before source selection.

Evidence search caches up to 64 queries for 30 seconds, partitioned by user, query, limit, exclusions and index modification time. Pending provider writes bypass cache. Writes, manual additions, deletes and clears invalidate the in-process cache; failed provider searches are not cached.

Lifecycle controls guard background indexing. Source deletion and memory clear remove indexed originals and leave source tombstones so backfill cannot silently restore them. This does not erase the user's mailbox. No original text or queries are added to diagnostic output.

No new network service, generative query rewriting, LLM compression or cross-encoder model is installed. Cross-encoder reranking remains a later optional addition requiring local latency/recall measurement. Automated tests cover short Chinese queries, source recovery from semantic hits, isolation, original preservation, overlap removal, budgets, cache reuse, exclusion, forgetting and no-extraction backfill. Real user recall and billed-token savings require separate acceptance.
