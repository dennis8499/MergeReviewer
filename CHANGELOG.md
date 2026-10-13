# Change log

## 0.11.0 - 2026-10-13

- Route new Group branch, quick, MR and Megin receipt reviews through fixed-source workspace context/result v2.
- Anchor changed and propagated contracts, bind every search to relevant evidence and require alternate queries before excluding candidates.
- Keep separate index and saved-file quick coverage, source freshness, graph cycles and unresolved consumer gaps.
- Revalidate MR/receipt bindings before publication, including portable reports; independently validate native Repo/Group receipt v2 without installing Megin.
- Retain historical context/receipt verification without promoting old evidence to new cross-Repo completion.
- Bundle the MIT-licensed Tomli parser for Python 3.10 receipt/scanner compatibility and verify its source hashes before isolated receipt validation.

## 0.10.0 - 2026-10-12

- Add portable Repo/Group review with explicit auto/repo/group boundaries and independent branch/staged/uncommitted targets.
- Freeze related Repos at their own workspace versions, retain exact index/working content and support unborn Repos without a remote.
- Add fixed-source inventory/read/search helpers, auditable impact graphs, candidate dispositions and source-specific change coverage.
- Validate reachable impact gaps, evidence versions, reviewed-change anchors and live version drift before publishing Group summaries and Repo details.
- Include legacy dual-source Group quick in standalone archives; retain schema-4 quick/ref, MR and optional Megin receipt contracts.

## 0.9.0 - 2026-10-12

- Validate complete native Megin v2 discovery, approval, scenario/TDD and review-history evidence with no Megin installation dependency.
- Retain standalone explicit-ref, local modification and native v1 receipt review.
- Use fixed ZIP metadata without compressor dependencies so release hashes are reproducible across checkout times and platforms.

## 0.8.0

- Add optional native Megin delivery receipt binding to fixed committed capture and validated Markdown/JSON reports, with independent historical proof validation and no Megin installation dependency.
- Preserve native single-Repo scope and compatibility for reviews without a receipt.

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
