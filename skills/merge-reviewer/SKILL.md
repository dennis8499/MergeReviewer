---
name: merge-reviewer
description: Review Git merges and branch differences from inside a workspace, including one-line comparison of the current local branch with a remote default branch, merge-base and direct modes, optional saved working-tree snapshots, merge-commit checks, and a Traditional Chinese Markdown report.
metadata:
  short-description: Review Git merges and branch differences
---

# Merge Reviewer

Review two Git versions without checking out either one. The normal mode reviews committed refs; quick mode compares the current local branch with a remote's advertised default branch and can optionally include a stable snapshot of saved working-tree files. The skill is designed for a VS Code workspace that may contain more than one repository. It reports evidence-backed correctness, security, performance, compatibility, and merge-integration findings in Traditional Chinese, with plain-language explanations and concrete examples that make each confirmed issue easy to understand.

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
  --format json --pretty
```

Use `--mode direct` for direct comparison. Add `--workspace-file <file.code-workspace>` when the user identifies a specific VS Code workspace. Omit `--project` only when the helper finds exactly one repository. Use `--context-dir <temporary-directory>` only when a raw patch bundle is useful; do not put temporary material in the target repository.

For a one-line review of the current branch, use:

```powershell
python <skill-dir>/scripts/git_review_context.py `
  --workspace <workspace-root> `
  --quick `
  --format json --pretty
```

Add `--remote <name>` when the repository has multiple remotes and `--include-working-tree` to review saved local changes. `--no-fetch` is valid only when every input is local, a tag, a commit, or `HEAD`; a remote-qualified input always requires network verification and causes an immediate parameter error with `--no-fetch`. When including working-tree files, pass a fresh `--context-dir` outside the repository when the caller controls the temporary path; otherwise the helper creates a durable external context bundle automatically and returns its path. Remove that bundle after the review report is complete. The helper uses `git ls-remote --symref <remote> HEAD` to find the remote default branch. `--base <remote>/<branch>` is an explicit quick-mode override and cannot be combined with `--remote`.

The helper discovers `.git` directories and worktree `.git` files, resolves workspace folders, fetches only explicitly selected remote branches with an exact refspec, and then freezes the inputs to commit SHAs. It never pulls, checks out, resets, merges, or changes the user's index. A fetch failure is fatal; never silently review stale remote refs. Pure commit IDs, tags, and local branches—including local branches with an upstream—are not fetched. Use an explicit remote ref such as `origin/release` when the remote namespace is intended.

Treat the helper output as the source of truth for `repo`, `base_sha`, `head_sha`, `diff_base`, `merge_base`, `review_scope`, `review_right`, `snapshot_read_info`, changed files, and merge commits. In committed scope, the comparison is object-based and excludes working-tree changes. In working-tree scope, review the helper's fixed tree snapshot and the files under its context bundle; do not read a mutable file again from the repository. Preserve and report the helper's `working_tree_unchanged` result.

If the helper fails because a ref is invalid, history is shallow, or there are zero or multiple merge bases, stop and report the exact reason. Do not switch comparison modes implicitly.

## Review procedure

Read [references/review-rules.md](references/review-rules.md) before classifying findings. Inspect the complete diff and then the relevant context at `head_sha` using Git object commands; never use checkout to create a review view:

```powershell
git -C <repo> diff --no-ext-diff --binary --find-renames --find-copies <diff_base> <review_right> --
git -C <repo> show <review_right>:<path>
git -C <repo> grep -n <symbol-or-config-key> <review_right> -- <path-or-directory>
```

For `review_scope=working-tree`, use the generated `working-tree.patch` and `working-tree-files/` context bundle for changed-file content. The temporary Git object directory is removed after the helper finishes; the bundle is the durable review evidence.

For large changes, process every changed path in batches and record binary files, submodules, renames, and mode-only changes as review limitations. Do not truncate a diff without saying which files were not inspected.

If the helper reports `review_complete=false` or any `dirty_submodule_paths`, keep the result `審查未完成` unless the nested submodule state is separately captured and reviewed. Do not describe a working-tree review as complete while nested uncommitted submodule content is outside the snapshot.

Review the changed behavior and its callers, configuration, data contracts, tests, error paths, authorization/validation, and compatibility assumptions. A finding must include a concrete trigger, impact, and evidence in the committed source. Describe the issue in plain language first, then give a concrete example with the operation or input, expected result, and actual result. Mark examples as derived from code or illustrative when they were not actually executed. Do not report style preferences or unsupported speculation.

For every merge commit listed by the helper:

1. Inspect the merge result against each parent, including `git show --cc <merge-sha>` when useful.
2. Look for one side's validation, authorization, error handling, configuration, or data transformation being lost during conflict resolution.
3. Confirm a suspected merge defect is still present in `head_sha`; do not report one that a later commit fixed.
4. For squash, rebase, or hand-copied changes without merge-parent evidence, review the behavior but do not claim that an identified defect was caused by manual merging.

The helper's default `merge` mode compares `merge_base(base, head)` to `review_right`. `direct` compares `base` to `review_right` for committed inputs. Quick working-tree scope intentionally stays in merge mode and keeps real `head_sha` for merge-history checks. State the selected mode, review scope, and both resolved SHAs/tree identifiers in the report.

## Report and side effects

Create the report directory and write one Markdown report to:

```text
<repo>/review-reports/merge-review-<UTC-timestamp>-<base-short>-<head-short>.md
```

Use a fresh filename if a timestamp collision occurs. The report must be Traditional Chinese and follow the readable structure in [references/review-rules.md](references/review-rules.md):

- `審查結論` first, with one plain-language sentence, P0–P3 counts, the highest-priority finding IDs, and any evidence gap that changes the conclusion;
- `問題總覽` as a short Markdown table with a fixed finding ID, Chinese priority label, problem, and user/data/service impact;
- `問題詳情` ordered by P0, P1, P2, P3, with a plain-language impact statement, an evidence-based operation scenario showing input, expected result, and actual result, a focused remediation direction, and technical evidence (file/line or commit, trigger, evidence, and impact scope);
- `範圍與限制` with changed-file summary, binary/submodule limitations, merge commits inspected, and tests not executed by this static review unless the user explicitly asked for tests and they were actually run;
- `技術審查紀錄` with project/repository path, input refs and resolved full SHAs, comparison mode and scope, remote selection, merge base, fetch outcome, working-tree status, and the evidence sources used.

Use the explicit result states from the reference: `發現具體問題`, `沒有差異`, `未發現具體問題`, or `審查未完成`. Use `審查未完成` whenever a fetch, ref, merge-base, file-read, context, or nested-checkout gap prevents a complete review, even when some findings were confirmed. Use `未發現具體問題` only after the complete feasible scope was checked and no evidence-backed finding was established. Keep the overview count and finding IDs consistent with the details. Do not invent examples for `沒有差異`, `未發現具體問題`, or an unverified finding.

Do not modify source files, configuration, branches, index, or history. Fetching refs and writing the requested report are the only allowed mutations. A working-tree review uses an alternate index and object directory outside the repository, then removes it after generating the context bundle. Do not auto-fix, merge, publish comments, or create commits.

Return a concise Traditional Chinese chat summary in this order:

1. result state and one-sentence plain-language conclusion;
2. P0–P3 counts, including zero counts;
3. up to three highest-priority finding IDs, each with the impact and one short situation example;
4. a note pointing to the full report when more findings exist;
5. a clickable link to the Markdown report.

When the result is `沒有差異` or `未發現具體問題`, say that no evidence-backed issue was established and do not invent a situation example. When the result is `審查未完成`, explain the missing evidence first; if findings exist, summarize only those that are supported and say that the review is incomplete. Keep the summary counts and finding IDs consistent with the Markdown report.

## Invocation examples

```text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
$merge-reviewer 基礎分支=abc123 比較分支=def456 比較模式=直接比較
$merge-reviewer 快速審查
$merge-reviewer 快速審查 包含未提交變更
$merge-reviewer 快速審查 遠端=upstream
```

For installation outside this repository, copy or symlink `skills/merge-reviewer` into `$CODEX_HOME/skills/merge-reviewer` (or `~/.codex/skills/merge-reviewer` when `CODEX_HOME` is unset), then invoke `$merge-reviewer` or allow normal automatic skill discovery.
