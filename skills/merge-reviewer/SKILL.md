---
name: merge-reviewer
description: Review Git merges and branch differences from inside a workspace, including one-line comparison of the current local branch with a remote default branch, merge-base and direct modes, optional saved working-tree snapshots, merge-commit checks, and a Traditional Chinese Markdown report.
metadata:
  short-description: Review Git merges and branch differences
---

# Merge Reviewer

Review two Git versions without checking out either one. The normal mode reviews committed refs and previews their isolated merge; quick mode compares the current local branch with a remote's advertised default branch and can optionally include a stable snapshot of saved working-tree files. The skill is designed for a VS Code workspace that may contain more than one repository. It records per-path review coverage and emits validated Traditional Chinese Markdown and JSON reports.

## Required request

Collect either the explicit values or the quick-review request:

- `專案名稱` (optional only when the workspace resolves to exactly one repository; a repository path is also accepted)
- `基礎分支` or commit
- `比較分支` or commit
- `比較模式`: `合併前審查` (default) or `直接比較`
- `快速審查` (optional): use the current local branch as the head and discover the remote default branch
- `遠端` (optional in quick mode): required when more than one remote exists
- `包含未提交變更` (optional in quick mode): include staged, unstaged, and non-ignored untracked files

Do not invent a missing base or head. If there are multiple repositories or ambiguous remotes, show the candidates and ask the user to choose. An unresolved ref is an immediate error: do not substitute a local branch, upstream, tag, or stale remote-tracking ref. An unqualified branch name (`main` or `feature/login`) and `refs/heads/...` resolve only in the local branch namespace; an unqualified name may still resolve a local tag when no same-named local branch exists. `origin/main` and `refs/remotes/origin/main` resolve only in the named remote namespace. Quick mode uses the current local branch's `HEAD`, does not require that branch to be pushed, and rejects detached `HEAD`.

## Deterministic Git preparation

Use the bundled helper before reading the diff:

```powershell
python <skill-dir>/scripts/git_review_context.py `
  --workspace <workspace-root> `
  --project <project-name-or-path> `
  --base <base-ref> `
  --head <head-ref> `
  --mode merge `
  --git-timeout 180 `
  --context-dir <new-directory-outside-repository> `
  --format json --pretty
```

Use `--mode direct` for direct comparison. Add `--workspace-file <file.code-workspace>` when the user identifies a specific VS Code workspace. Omit `--project` only when the helper finds exactly one repository. Always create a unique `--context-dir` outside the target repository so the result JSON and evidence bundle survive until validation and report creation.

For a one-line review of the current branch, use:

```powershell
python <skill-dir>/scripts/git_review_context.py `
  --workspace <workspace-root> `
  --quick `
  --git-timeout 180 `
  --context-dir <new-directory-outside-repository> `
  --format json --pretty
```

Add `--remote <name>` when the repository has multiple remotes and `--include-working-tree` to review saved local changes. `--no-fetch` is valid only when every input is local, a tag, a commit, or `HEAD`; a remote-qualified input always requires network verification and causes an immediate parameter error with `--no-fetch`. The snapshot includes force-staged ignored files and preserves the real index. Use a unique external `--context-dir` in every mode. Remove it only after the final JSON and Markdown reports have been written. Each Git command times out after 180 seconds by default; increase `--git-timeout` for unusually large repositories. The helper uses `git ls-remote --symref <remote> HEAD` to find the remote default branch. `--base <remote>/<branch>` is an explicit quick-mode override and cannot be combined with `--remote`.

The helper discovers `.git` directories and worktree `.git` files, resolves workspace folders, fetches only explicitly selected remote branches with an exact refspec, and then freezes the inputs to commit SHAs. It never pulls, checks out, resets, merges, or changes the user's index. A fetch failure is fatal; never silently review stale remote refs. Pure commit IDs, tags, and local branches—including local branches with an upstream—are not fetched. Use an explicit remote ref such as `origin/release` when the remote namespace is intended.

Treat the helper output as the source of truth for `repo`, `base_sha`, `head_sha`, `diff_base`, `merge_base`, `review_scope`, `review_right`, `context_complete`, `context_gaps`, `evidence_files`, `merge_preview`, changed files, and merge commits. Manifest schema version 4 defines `context_complete` as evidence preparation status; `review_complete` remains a compatibility alias for that same value and never asserts that the Skill has actually reviewed every path. In committed scope, the comparison is object-based and excludes working-tree changes. Staged, unstaged, deleted, and untracked files do not prevent a committed-ref review; do not ask the user to stage, commit, stash, or clean them. Read the diff and affected file contents from the resolved commit objects so local edits to the same paths cannot enter the review. These existing edits stay in place. In working-tree scope, review the helper's fixed tree snapshot and files under its context bundle; do not read a mutable file again from the repository. Preserve and report `working_tree_unchanged`.

If the helper fails because a ref is invalid, history is shallow, or merge mode has zero or multiple merge bases, stop and report the exact reason. Direct mode compares the two fixed commits without requiring a shared base; its manifest `merge_base` is `null` when no unique base exists. Do not switch comparison modes implicitly.

In `merge` mode, the helper also runs `git merge-tree --write-tree` using temporary Git object storage outside the repository. The resulting tree, diff, changed file blobs, and merge output are retained under the context bundle until the report is complete. A clean merge preview still requires semantic compatibility review of changes from both branches. A conflict or an unavailable Git capability makes `context_complete=false`; describe the evidence gap and return `審查未完成`. `direct` mode deliberately omits this preview.

## Review procedure

Read [references/review-rules.md](references/review-rules.md) before classifying findings. Inspect the complete diff and then relevant context from the recorded source for each path (the primary review tree, fixed working-tree snapshot, merge preview, base, or merge parent) using Git object commands; never use checkout to create a review view:

```powershell
git -C <repo> diff --no-ext-diff --no-textconv --binary --find-renames --find-copies <diff_base> <review_right> --
git -C <repo> show <review_right>:<path>
git -C <repo> grep -n <symbol-or-config-key> <review_right> -- <path-or-directory>
```

For `review_scope=working-tree`, use `working-tree.patch` and `working-tree-files/` for fixed changed-file content. Each exported file has an exact Git blob id and SHA-256 in `evidence_files`. For merged content, use `merge-preview.patch`, `merge-preview-files/`, and the preserved `merge-preview-objects/` object directory. For findings, cite `head`, `base`, `working-tree`, `merge-preview`, or `merge-parent` with the exact matching commit or tree SHA, repository-relative path, and valid line interval when known. Deleted file evidence must point to the old path in `base` or a merge parent.

For large changes, process every changed path in batches and record binary files, submodules, renames, and mode-only changes as review limitations. Do not truncate a diff without saying which files were not inspected.

If the helper reports `context_complete=false` (or the compatibility field `review_complete=false`) or any `dirty_submodule_paths`, keep the result `審查未完成` unless the missing context or nested submodule state is separately captured and reviewed. Do not describe a working-tree review as complete while nested uncommitted submodule content is outside the snapshot.

Review the changed behavior and its callers, configuration, data contracts, tests, error paths, authorization/validation, and compatibility assumptions. A finding must include a concrete trigger, impact, and evidence in the fixed source named by its citation. Also inspect relevant base-version context so pre-existing issues are not attributed to this change. Describe the issue in plain language first, then give a concrete example with operation/input, expected result, and actual result. Mark examples `code-derived` or `reproduced`; never imply static examples were executed. Do not report style preferences or unsupported speculation.

For every merge commit listed by the helper:

1. Inspect the merge result against each parent, including `git show --cc <merge-sha>` when useful.
2. Look for one side's validation, authorization, error handling, configuration, or data transformation being lost during conflict resolution.
3. Confirm a suspected merge defect is still present in the final reviewed tree (`review_right` for the review scope); do not report one that a later commit or saved working-tree change fixed.
4. For squash, rebase, or hand-copied changes without merge-parent evidence, review the behavior but do not claim that an identified defect was caused by manual merging.

The helper's default `merge` mode compares `merge_base(base, head)` to `review_right` and independently previews the merge of the resolved base/head commits. `direct` compares `base` to `review_right` and may have `merge_base=null`. Quick working-tree scope intentionally keeps `head_sha` for committed-history checks while `review_right` identifies its saved file snapshot. State each source separately in the structured result.

## Report and side effects

Create the report directory and write one Markdown report to:

```text
<repo>/review-reports/merge-review-<UTC-timestamp>-<base-short>-<head-short>.md
<repo>/review-reports/merge-review-<UTC-timestamp>-<base-short>-<head-short>.json
```

Use a fresh shared filename if either report file already exists. Never hand-write or modify one output independently; both formats must be produced by `scripts/review_report.py` from the same validated JSON result. The report must be Traditional Chinese and follow the readable structure in [references/review-rules.md](references/review-rules.md):

- `審查結論` first, with the merge recommendation (`可以合併`, `修正後再合併`, `暫緩合併`, or `需補做審查`, decided by the rules in the reference), the result state with its plain-language explanation, one plain-language sentence, P0–P3 counts with action labels, one to three next steps with the responsible role, and any evidence gap that changes the conclusion;
- `問題總覽` as a short Markdown table with a fixed finding ID, action-oriented priority label (for example `P1 合併前必修`), problem, user/data/service impact, and suggested handler (`開發`, `維運設定`, or `QA`);
- `問題詳情` ordered by P0, P1, P2, P3, with a plain-language impact statement, an evidence-based operation scenario showing input, expected result, and actual result, a plain-language `建議處理`, and technical evidence for engineers (file/line or commit, trigger, evidence, impact scope, and the engineering fix suggestion);
- `範圍與限制` with a plain-language description of what was and was not checked, changed-file summary, binary/submodule limitations, merge commits inspected, and tests not executed by this static review unless the user explicitly asked for tests and they were actually run;
- `技術審查紀錄（工程師參考）` with project/repository path, input refs and resolved full SHAs, comparison mode and scope, remote selection, merge base, fetch outcome, working-tree status, and the evidence sources used.

### Structured result and validation

After inspecting the complete context, write `review-result-draft.json` under the unique context directory. Its schema is version 1:

```json
{
  "schema_version": 1,
  "summary": "一到兩句白話審查摘要。",
  "coverage": [
    {
      "path": "src/orders.py",
      "status": "reviewed",
      "evidence": [
        {
          "source": "head",
          "ref": "<full source commit or tree SHA>",
          "path": "src/orders.py",
          "line_start": 18,
          "line_end": 24
        }
      ]
    }
  ],
  "findings": [],
  "next_steps": [],
  "tests_executed": [],
  "tests_not_executed": ["未執行自動測試；本次為靜態審查。"],
  "limitations": []
}
```

List every `changed_files` path and `old_path`, plus every merge-preview changed path and `old_path`, in `coverage`. A path changed in both scopes needs citations for both the primary review version and merge preview. `status` is `reviewed`, `metadata-only`, or `not-reviewed`. Reviewed paths require citations matching each applicable fixed source; the other states require a plain-language `reason` and force the final result to `審查未完成`. Coverage describes what the Skill actually inspected; `context_complete` describes whether the helper prepared all requested evidence.

Each finding uses `id`, `priority` (`P0`–`P3`), `title`, `impact`, `trigger`, `expected`, `actual`, `recommendation`, `scenario_source` (`code-derived` or `reproduced`), `owner` (`開發`, `維運設定`, or `QA`), and at least one `evidence` item on a reviewed changed path. Optional technical fields are `evidence_summary` and `engineering_fix`. IDs must be sequential (`F-001` onward), and findings must be ordered P0 to P3. Evidence sources are `head`, `base`, `working-tree`, `merge-preview`, and `merge-parent`; use the matching full SHA, repository-relative path, and valid line interval. The validator checks each citation against its fixed Git object or saved snapshot, checks that snapshot file hashes match, and refuses references outside the manifest's commits and trees.

Write the draft into the external context bundle and run:

```powershell
python <skill-dir>/scripts/review_report.py `
  --context-dir <context-directory> `
  --result <context-directory>/review-result-draft.json `
  --report-dir <repo>/review-reports `
  --git-timeout 180
```

The validator requires a schema-4 context and complete path coverage. It computes the final state, severity counts, and merge recommendation from context gaps, preview status, coverage, and findings. It rejects missing paths, invalid evidence line ranges, duplicate findings, impossible priorities, and invalid roles before creating files. Only this script writes the matching Markdown and JSON reports. If validation fails, correct the draft or restore the evidence bundle and rerun it; do not describe the review as complete.

The conclusion, overview, impact statement, operation scenario, and `建議處理` must be plain language: no class, method, variable, file names, SHAs, or English jargon (use the term table in the reference). Keep those terms only in the technical sections.

Use the explicit result states from the reference: `發現具體問題`, `沒有差異`, `未發現具體問題`, or `審查未完成`. Use `審查未完成` whenever a fetch, ref, merge-base, file-read, context, or nested-checkout gap prevents a complete review, even when some findings were confirmed. Use `未發現具體問題` only after the complete feasible scope was checked and no evidence-backed finding was established. Keep the overview count and finding IDs consistent with the details. Do not invent examples for `沒有差異`, `未發現具體問題`, or an unverified finding.

Do not modify source files, configuration, branches, index, or history. Fetching refs, creating the external context bundle, and writing the requested report pair are the only allowed mutations. The helper uses external alternate index/object directories and cleans its private temporary data after saving evidence. Preserve the context bundle until both reports are created; then remove the full external context directory. Do not auto-fix, perform a repository merge, publish comments, or create commits.

Return a concise Traditional Chinese chat summary in plain language, in this order:

1. merge recommendation, result state, and one-sentence plain-language conclusion;
2. P0–P3 counts with action labels, including zero counts;
3. up to three highest-priority finding IDs, each with the impact and one short situation example, without class, method, or file names;
4. next steps with the responsible role;
5. a note pointing to the full report when more findings exist;
6. clickable links to both the Markdown report and matching JSON result.

When the result is `沒有差異` or `未發現具體問題`, say that no evidence-backed issue was established and do not invent a situation example. When the result is `審查未完成`, explain the missing evidence first; if findings exist, summarize only those that are supported and say that the review is incomplete. Keep the summary counts and finding IDs consistent with the Markdown report.

When the helper fails, tell the user in plain language what went wrong, then pass on the helper's `建議：` line (printed after the error line on stderr) as the next step. Do not silently work around the failure.

## Invocation examples

```text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
$merge-reviewer 基礎分支=abc123 比較分支=def456 比較模式=直接比較
$merge-reviewer 快速審查
$merge-reviewer 快速審查 包含未提交變更
$merge-reviewer 快速審查 遠端=upstream
```

For installation outside this repository, copy or symlink `skills/merge-reviewer` into `$CODEX_HOME/skills/merge-reviewer` (or `~/.codex/skills/merge-reviewer` when `CODEX_HOME` is unset), then invoke `$merge-reviewer` or allow normal automatic skill discovery.
