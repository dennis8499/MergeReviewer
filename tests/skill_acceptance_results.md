# Codex skill acceptance — 2026-10-12

The implementing Codex session executed the portable Group workflow against two isolated Git workspaces prepared by `skill_acceptance.py`. Codex read the frozen changes, baseline/merge-preview, callers, event consumer, package identities and shared contract; searched the captured sources; decided the findings; authored drafts; and published them through `review_report.py`. The fixture helper prepares source only and never generates review decisions.

| Case | Expected behavior | Observed Codex review |
| --- | --- | --- |
| Remove `totalCents`, expose only `amountCents` | Detect incompatibility in unchanged callers and event consumers | One P1 finding: the web display reads a missing field and Worker contract validation rejects the event before settlement. |
| Keep `totalCents` and add the equivalent `amountCents` alias | Preserve existing consumers without an invented regression | No findings; existing display, validation and settlement still receive the original value. |

Both reports follow `orders-api → web` and `orders-api → invoice-worker → shared-contracts`; the four involved Repos are recorded with fixed source refs. `unrelated-admin` is explicitly excluded after source inspection and contract searches. The API is the only changed Repo. Changed-path coverage and connected impact coverage pass validation; no candidate is silently included or omitted. Original HEAD, branches, index and source files remain unchanged by review preparation and publication.

Local generated artifacts (ignored by Git):

- [Regression report](../test-artifacts/skill-acceptance/regression/summary.md) and [validated result](../test-artifacts/skill-acceptance/regression/result.json).
- [Safe-change report](../test-artifacts/skill-acceptance/safe/summary.md) and [validated result](../test-artifacts/skill-acceptance/safe/result.json).

Reproduce the inputs with `python -X utf8 -B tests/skill_acceptance.py --case regression` or `--case safe`, then ask Codex to execute the returned skill request. Follow `references/workspace-review.md` for the frozen-source commands and draft contract. Publish or clean the returned context, then clean the fixture root with `review_session.py cleanup --context-dir <fixture_root>`.

This is actual static skill execution by the current Codex session, with known expected outcomes, not an independent blind benchmark. TypeScript application code and application tests were not executed. Findings use `scenario_source: code-derived`. These two cases establish workflow acceptance, not a general model recall/false-positive rate; helper regression tests separately exercise scope, snapshots, evidence validation, incompleteness and cleanup.

## Implementation validation

Final source/package checks ran locally on Windows with Python 3.14.7:

| Suite | Result |
| --- | --- |
| MergeReviewer `python -X utf8 -B -m unittest discover -s tests -p 'test_*.py'` | 121 tests: 120 passed, 1 skipped because this Windows account cannot create symbolic links. Includes 29 new workspace tests. |
| `python -X utf8 -B -m behave tests/features --tags='not @human-acceptance'` | 24 scenarios passed; the GitHub release-publishing human scenario was excluded. |
| GitlabWorkSpace `test/overlays/merge-reviewer/test_group_review.py` | 10 passed, including portable target dispatch, fixed MR rejection and existing legacy Group/MR behavior. |
| GitlabWorkSpace `test/release/workflow_kit_installer_test.py` | 11 passed. |
| GitlabWorkSpace `test/release/workflow_kit_profiles_test.py` | 7 passed. |
| GitlabWorkSpace `test/release/workflow_kit_transactions_test.py` | 20 passed, including reproducible archive digests and upgrade/rollback preservation. |
| Skill creator validator and `git diff --check` in both repositories | Passed. |

The final standalone `dist/merge-reviewer-0.10.0.zip` contains all 19 installable skill files and matches the fixed Workspace source archive byte-for-byte. SHA-256: `ed02ca66030420c776447c31725600be9a96d97ba70eb7844ff7017acabb3d2e`. The Workspace offline tar and release ZIP were rebuilt with this pin. These local checks do not claim execution of the Linux/Python 3.10 CI matrix or external release publication.
