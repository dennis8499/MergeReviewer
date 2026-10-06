# Change log

## 0.7.0

- Standalone release contains only the single-repository reviewer. Group review and GitLab MR support now arrive as a separately installed Workspace extension.
- Add an optional `workspace_extension.py` adapter hook; native argument parsing and report publishing remain unchanged when no extension is installed.

## 0.6.0

- All review modes keep evidence contexts in marked system-temporary directories. Report generation cleans the current owned context on success and failure; interrupted reviews have a verified cleanup command.
- Fixed MR Markdown keeps portable version metadata while JSON becomes optional by default.
- GitLab Workspace can pass fixed MR tasks inline as base64 JSON instead of saving a task file in the Group.
- Python helper entrypoints suppress runtime bytecode in installed Skill directories.

## 0.5.0

- Group quick review captures direct-child Repos, including worktrees and unborn repositories,
  with independent fixed index and working-tree evidence. It uses local HEAD without fetching,
  continues after individual failures, and writes atomic Group/per-Repo reports.
- Findings shared by both snapshots retain both version sources. Capture or later content drift,
  binary coverage and failed repositories remain explicit review limitations.
- `MergeReviewTask/v1` binds the actual Repo, GitLab/fork identities and exact source/current target
  SHAs. Missing commits are fetched by exact SHA without branch fallback. MR Markdown and JSON
  reports contain `MergeReviewReport/v1` metadata and a normalized body SHA-256.
- Existing single-Repo, ref and MR context/report semantics remain available.
