# OPS required CI

Issue #11 moves the delivery gate from chat claims to GitHub checks. The workflow is intentionally one Windows job so every check runs serially and each pytest invocation receives its own ignored `.evidence/ci/<run>/<name>` basetemp. CI does not install `rtk`; the command mapping below is the contract for equivalent local and runner invocations.

## Required check

- Workflow: `Required CI`
- Job/check name: `required-ci`
- GitHub UI label: `Required CI / required-ci`
- Triggers: every pull request and every push to `main`
- Runner: `windows-2022`, Python `3.12.10`
- Permissions: `contents: read`
- No dependency cache and no artifact upload are used; the job never reads original media, model weights, private user data, or API keys.

The pytest steps write JUnit XML only inside the ignored run directory and fail
if a report has zero tests or any failures, errors, or skipped tests. Collection
also requires a positive `tests collected` count. Missing dependencies therefore
fail installation or the test command; they cannot become a green skip.

## Local-to-CI command mapping

| Local command | CI equivalent |
| --- | --- |
| `rtk python -m pytest ...` | `python -m pytest ...` |
| `rtk python -m pytest --collect-only ...` | `python -m pytest --collect-only ...` |
| `rtk python -m compileall ...` | `python -m compileall ...` |
| `rtk python baseline_hardening_scan.py ...` | `python baseline_hardening_scan.py ...` |
| `rtk python tools/verify_b02_scope.py` | `python tools/verify_b02_scope.py` |
| `rtk python tools/verify_B10A_scope.py` | `python tools/verify_B10A_scope.py` |
| `rtk python tools/verify_gov_scope.py` | `python tools/verify_gov_scope.py` |
| `rtk python tools/verify_b10b_scope.py` | `python tools/verify_b10b_scope.py` |
| `rtk python tools/verify_current_main_scope.py` | `python tools/verify_current_main_scope.py` |
| `rtk python tools/verify_b08_scope.py --current` | `python tools/verify_b08_scope.py --current` |
| `rtk python tools/verify_b08_scope.py --historical-only` | `python tools/verify_b08_scope.py --historical-only` |
| `rtk python tools/verify_b08_scope.py --composed` | `python tools/verify_b08_scope.py --composed` |
| `rtk python tools/verify_project_status.py` | `python tools/verify_project_status.py` |
| `rtk python tools/healthcheck.py --profile core` | `python tools/healthcheck.py --profile core` |
| `rtk python tools/healthcheck.py --profile asr` | `python tools/healthcheck.py --profile asr` |
| `rtk python tools/asr_healthcheck.py` | `python tools/asr_healthcheck.py` |
| `rtk python tools/live_healthcheck.py` | `python tools/live_healthcheck.py` |
| `rtk python tools/verify_b05_scope.py --historical-only` | `python tools/verify_b05_scope.py --historical-only` |
| `rtk python tools/verify_b05_scope.py --composed` | `python tools/verify_b05_scope.py --composed` |
| `rtk python tools/verify_b02_scope.py --composed-b05` | `python tools/verify_b02_scope.py --composed-b05` |
| `rtk python tools/verify_b04_scope.py --composed-b05` | `python tools/verify_b04_scope.py --composed-b05` |
| `rtk python tools/verify_B10A_scope.py --composed-b05` | `python tools/verify_B10A_scope.py --composed-b05` |
| `rtk python tools/verify_p01_scope.py --composed-b05` | `python tools/verify_p01_scope.py --composed-b05` |
| `rtk git diff --check <base> <head> --` | `git diff --check <base> <head> --` |

`verify_gov_scope.py` is the independent GOV child verifier. It also validates
the current B08 boundary before returning B08 paths as exclusions. Historical
tranche scopes may exclude the three governance documents and validated B08
paths only after the corresponding verifier passes; they must not add those
documents to their own historical allowlists. B08 `--composed` is the explicit
composition gate for GOV, B05, B06, B07, B02, B04, B10A, and P01.
The reviewer must also block any ARCH-01 violation: the repository assembles
validated upstreams through thin adapters and records upstream/version/commit,
license, replacement boundary, provenance, and uninstall path.

The scanner runs `all` and each of `comments`, `runtime-dependencies`, `secrets`, `sensitive-paths`, `large-files`, and `evidence-ignore`. Targeted pytest covers ASR, governance, HTTP, Live/B08, packaging, TTS, and baseline hardening; full pytest covers the complete `tests/` tree.

The current latest-main regression baseline is 202 targeted tests passed, 289 full
tests passed, and 289 tests collected, with zero failures, errors, or skips. These
counts document the observed inventory; the workflow does not hard-code them and
fails on collection/full-test errors or any skipped test. B10B lifecycle evidence
also requires a fail-closed missing-provider result, reversible rollback, and
zero fail/error/skip counts.

## Main protection recommendation

This payload is a controller recommendation, not proof that protection is enabled. Apply it only after the PR has exposed the real `required-ci` check, then verify with a fresh GitHub API GET. This branch does not call the branch-protection API and does not merge `main`.

Endpoint: `PUT /repos/Ornn8/bside-olivia-community/branches/main/protection`

```json
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["required-ci"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": true,
    "required_approving_review_count": 1,
    "require_last_push_approval": true,
    "bypass_pull_request_allowances": {
      "users": [],
      "teams": [],
      "apps": []
    }
  },
  "restrictions": {
    "users": [],
    "teams": [],
    "apps": []
  },
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": true,
  "lock_branch": false,
  "allow_fork_syncing": false
}
```

`CODEOWNERS` routes review to `@Ornn8`; it does not itself approve a PR or simulate branch protection. A real reviewer distinct from the author must satisfy the review requirement when the controller enables it.
