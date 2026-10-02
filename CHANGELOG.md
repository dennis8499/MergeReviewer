# Change log

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
