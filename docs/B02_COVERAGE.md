# B02 coverage matrix

以下是 B02 HTTP 测试与契约边界的对应关系；测试只使用本地 aiohttp TestServer/in-process route 和脱敏 fixture。

| 场景 | 覆盖测试 | 结果要求 |
|---|---|---|
| core health/version/capabilities | `test_core_health_is_versioned_and_reports_unavailable_optional_capabilities` | 200、`b02.v1`、required core healthy、native realtime unavailable |
| invalid health profile | `test_invalid_health_profile_is_a_stable_client_error` | 400 `INVALID_PROFILE` |
| empty letters/music | `test_empty_letter_and_music_paths_are_explicitly_empty` | 200 + empty list/source，不假造失败或媒体 URL |
| normal send/list/detail | `test_normal_send_list_and_detail_preserve_legacy_fields` | 200、保留 `letter_id`/`reply_text` 兼容字段 |
| missing field/type | `test_missing_fields_and_invalid_json_never_become_success` | 400、明确字段/类型错误 |
| malformed HTTP JSON/method | `test_handler_rejects_malformed_json_and_wrong_methods` | 400 `INVALID_JSON`、405 `METHOD_NOT_ALLOWED` |
| provider failure/retry | `test_handler_acknowledges_before_background_llm_failure` | send 200/PENDING；detail `FAILED` + retryable |
| pending restart recovery | `test_persisted_pending_reply_resumes_when_http_runtime_starts` | 重启后恢复任务并持久化终态 |
| failed retry supersession | `test_successful_retry_replaces_recent_failed_copy_in_current_mailbox` | FAILED 审计记录保留；current list 隐藏；旧 ID 返回 410 tombstone |
| legacy isolation/read-only | `test_legacy_scope_is_read_only_and_isolated_from_new_chat` | current/legacy 分离；legacy detail 不改读状态；禁止写入 |
| true unimplemented capability | `test_unimplemented_routes_and_native_capabilities_are_not_fake_successes` | 501 `NOT_IMPLEMENTED`；health 不伪装 native capability |
| schema/fixture privacy | `test_contract_and_fixture_artifacts_are_versioned_and_sanitized` | 版本、read-only schema、无原文/私密标识 |
| runtime log privacy | `test_request_and_reply_values_never_enter_runtime_logs` | body/query/reply/token 不进入日志 |

既有 B00 回归文件 `tests/test_baseline_hardening.py` 继续覆盖 CORS、后台 LLM 失败、HTTP 501/404、MIDI 终态、日志和安全扫描。
