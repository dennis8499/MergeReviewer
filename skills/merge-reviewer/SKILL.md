---
name: merge-reviewer
description: Review one Repo between an explicit base branch and comparison branch using Codex Code Review. In a declared multi-Repo workspace, trace only the changed Repo's affected contracts and consumers, then publish a concise Traditional Chinese Merge Report.
metadata:
  short-description: Review one Repo's branches and related impact
---

# Merge Reviewer

Use Codex's native Code Review judgment for the core review. This skill supplies immutable Git evidence, conditional cross-Repo context and a report workflow; it does not replace native bug criteria or priority definitions. Apply relevant root/nested `AGENTS.md` review guidance. Report formatting and helper success cannot establish semantic correctness.

## Request and boundary

Require all three business inputs:

- `專案名稱`: exactly one Repo name or path (`--project`).
- `基礎分支`: a local or explicitly remote-qualified branch (`--base`).
- `比較分支`: a local or explicitly remote-qualified branch (`--head`).

Ask for missing inputs or an ambiguous Repo, showing discovered candidates. Do not infer a branch. Tags, commit IDs, `HEAD`, Stage, uncommitted, quick, direct comparison, multi-seed review, MR tasks and delivery receipts are unsupported. Explain the supported branch request instead of silently converting a removed mode.

Use the actual VS Code `.code-workspace` when provided. Otherwise use the current task's declared workspace root; never climb to unrelated parent/sibling folders. Workspace and report options configure the environment, not additional review targets. See [usage.md](references/usage.md) when the user needs installation, examples, report interpretation or troubleshooting.

## Prepare fixed evidence

~~~powershell
python "<skill-dir>/scripts/git_review_context.py" --workspace "<declared-root>" --project "<Repo-name-or-path>" --base "<base-branch>" --head "<comparison-branch>" --pretty
~~~

Add `--workspace-file <file.code-workspace>` for the declared VS Code boundary. Use `--no-fetch` only with local branches. The comparison is `merge-base(base, head) → head`, with merge-parent and merge-preview evidence. Remote-qualified branches are synchronized exactly; failed synchronization never falls back to stale refs. Original files, HEAD and index stay unchanged.

The public context/result schemas are v3. Treat `manifest.json` as source truth: `selected_repo` identifies the only seed; `extension_enabled` is true only when the declared boundary contains at least two distinct Git roots. Related Repos contribute their captured HEAD, not the seed's branch names or local edits.

## Review

Read [review-rules.md](references/review-rules.md) for the relationship to native review and evidence handling, and [workspace-review.md](references/workspace-review.md) for fixed reads and the v3 result contract.

Read complete patches and relevant fixed context, including base versions and applicable `AGENTS.md`. Let native Code Review determine whether an issue is actionable and its P0–P3 priority. Preserve supported findings even when other evidence is incomplete.

When `extension_enabled=false`, review the selected Repo normally; do not register cross-Repo contracts or require impact graphs. When true, follow the selected changes' contracts, callers and consumers across captured candidates. Bind searches and graph edges to fixed evidence, keep unrelated candidates excluded with reasons, and mark unresolved required links incomplete. Never audit unrelated Repos as separate review targets.

## Publish

Read [report-style.md](references/report-style.md) only for report wording. Write a Codex-authored `draft.json` inside the owned context using actual v3 coverage. The helper cannot create semantic review conclusions for you.

~~~powershell
python "<skill-dir>/scripts/review_report.py" --context-dir "<context>" --result "<context>/draft.json"
~~~

The publisher validates evidence and coverage, computes completion/counts, and writes exactly one `<selected-Repo>/review-reports/merge-review-<timestamp>-<base-commit>-<head-commit>.md` titled `Merge Report: <Repo>`. Use the UTC publication timestamp with microseconds (`YYYYMMDDTHHMMSSffffffZ`) and the captured full branch-tip SHAs, not the merge base or current checkout. Publish Markdown only and return its path on stdout. Preserve existing reports on filename collisions. Combine related-Repo evidence into this report.

Return a concise Traditional Chinese result with supported issues, material gaps/next steps and a clickable report link. Say explicitly when no evidence-backed issue was found; incomplete review is not a pass. Do not claim project tests ran unless actually executed.

## Cleanup and side effects

Fetching explicitly selected remote branches, creating owned system-temp evidence and writing reports are permitted side effects. Do not change source/configuration, checkout, merge into a real branch, reset, stage, commit, push or publish comments.

Publication cleans owned context on success and failure. If review stops before publication, clean only that owned session:

~~~powershell
python "<skill-dir>/scripts/review_session.py" cleanup --context-dir "<context>"
~~~

After publication/validation fails, prepare fresh evidence and repeat the review. Report exact preparation failures and the helper's suggested next step.

## Example

~~~text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
~~~
