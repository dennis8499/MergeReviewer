---
name: merge-reviewer
description: Review a merge between two Git branches or commits from inside a workspace, with merge-base and direct comparison modes, merge-commit checks, and a Traditional Chinese Markdown report. Use when a user asks to inspect what will be merged or whether an already merged change lost logic.
metadata:
  short-description: Review Git merges and branch differences
---

# Merge Reviewer

Review two committed Git versions without checking out either one. The skill is designed for a VS Code workspace that may contain more than one repository. It reports evidence-backed correctness, security, performance, compatibility, and merge-integration findings in Traditional Chinese.

## Required request

Collect these values from the user or the invocation:

- `專案名稱` (optional only when the workspace resolves to exactly one repository; a repository path is also accepted)
- `基礎分支` or commit
- `比較分支` or commit
- `比較模式`: `合併前審查` (default) or `直接比較`

Do not invent a missing base or head. If there are multiple repositories, ambiguous names, ambiguous remotes, or an unresolved ref, show the candidates and ask the user to choose.

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

The helper discovers `.git` directories and worktree `.git` files, resolves workspace folders, fetches the remote branch associated with each input branch, and then freezes both inputs to commit SHAs. It never pulls, checks out, resets, merges, or stages anything. A fetch failure is fatal; never silently review stale remote refs. Pure commit IDs and local branches without an upstream are not fetched. If a branch exists on several remotes, use an explicit remote ref such as `origin/release` or ask the user to choose.

Treat the helper output as the source of truth for `repo`, `base_sha`, `head_sha`, `diff_base`, `merge_base`, changed files, and merge commits. The comparison is object-based, so uncommitted working-tree changes must not enter the review. Preserve and report the helper's `working_tree_unchanged` result.

If the helper fails because a ref is invalid, history is shallow, or there are zero or multiple merge bases, stop and report the exact reason. Do not switch comparison modes implicitly.

## Review procedure

Read [references/review-rules.md](references/review-rules.md) before classifying findings. Inspect the complete diff and then the relevant context at `head_sha` using Git object commands; never use checkout to create a review view:

```powershell
git -C <repo> diff --no-ext-diff --binary --find-renames --find-copies <diff_base> <head_sha> --
git -C <repo> show <head_sha>:<path>
git -C <repo> grep -n <symbol-or-config-key> <head_sha> -- <path-or-directory>
```

For large changes, process every changed path in batches and record binary files, submodules, renames, and mode-only changes as review limitations. Do not truncate a diff without saying which files were not inspected.

Review the changed behavior and its callers, configuration, data contracts, tests, error paths, authorization/validation, and compatibility assumptions. A finding must include a concrete trigger, impact, and evidence in the committed source. Do not report style preferences or unsupported speculation.

For every merge commit listed by the helper:

1. Inspect the merge result against each parent, including `git show --cc <merge-sha>` when useful.
2. Look for one side's validation, authorization, error handling, configuration, or data transformation being lost during conflict resolution.
3. Confirm a suspected merge defect is still present in `head_sha`; do not report one that a later commit fixed.
4. For squash, rebase, or hand-copied changes without merge-parent evidence, review the behavior but do not claim that an identified defect was caused by manual merging.

The helper's default `merge` mode compares `merge_base(base, head)` to `head`. `direct` compares `base` to `head`. State the selected mode and both resolved SHAs in the report.

## Report and side effects

Create the report directory and write one Markdown report to:

```text
<repo>/review-reports/merge-review-<UTC-timestamp>-<base-short>-<head-short>.md
```

Use a fresh filename if a timestamp collision occurs. The report must be Traditional Chinese and include:

- project name and repository path;
- input refs and resolved full SHAs;
- comparison mode, merge base, fetch outcome, and whether the working tree stayed unchanged;
- review scope, changed-file summary, binary/submodule limitations, and merge commits inspected;
- findings ordered by P0, P1, P2, P3, each with title, file and line (or commit), trigger, evidence, impact, and a focused remediation suggestion;
- tests not executed by this static review, unless the user explicitly asked for tests and they were actually run.

Use the explicit result states from the reference: `沒有差異`, `未發現具體問題`, or `審查未完成`. The second state is not a guarantee that the code is correct. If the review is incomplete, explain the missing evidence or files.

Do not modify source files, configuration, branches, index, or history. Fetching refs and writing the requested report are the only allowed mutations. Do not auto-fix, merge, publish comments, or create commits.

Return a concise Traditional Chinese chat summary with the result state, P0–P3 counts, the most important findings, and a clickable link to the Markdown report.

## Invocation examples

```text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
$merge-reviewer 基礎分支=abc123 比較分支=def456 比較模式=直接比較
```

For installation outside this repository, copy or symlink `skills/merge-reviewer` into `$CODEX_HOME/skills/merge-reviewer` (or `~/.codex/skills/merge-reviewer` when `CODEX_HOME` is unset), then invoke `$merge-reviewer` or allow normal automatic skill discovery.
