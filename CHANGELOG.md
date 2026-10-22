# Change log

## 0.18.0 — 2026-10-22 (local candidate)

- Add request-ID start/step/finish/status/cancel orchestration over the existing helpers, with owned evidence outside source Repos and permission preflight before transport.
- Share in-flight precise fetches and completed receipts for 300 seconds without sliding expiry; keep 30-second failure cooldown, explicit refresh, private refs and credential/remote invalidation.
- Reuse verified immutable peer packs with two local packing slots and a 2-GiB LRU; copy independent evidence into every session.
- Capture coordinated peers directly at their pinned commits without redundant worktree/index scans; later local HEAD changes affect only new requests.
- Batch ordered reads, contract registration and searches with one manifest commit, durable prefix checkpoints, operation-ID replay and bounded 24-KiB output with complete artifacts.
- Prepare honest unreviewed drafts, retain repairable JSON/field errors, and save publication before cleanup. Retry returns the existing report.
- Render the workbench's five H2 sections directly for engineering-review drafts; preserve legacy CLI, v3 and exit 0/2/3 compatibility.
- Record helper phase durations separately. Local helper validation and model-quality acceptance are documented independently.

## 0.17.0 — 2026-10-21

- Unify the Traditional Chinese merge review report layout while preserving validated findings, evidence and editable confirmations.
- Record Git fetch attempt/success and requested/synced branch counts separately; branch entries do not represent HTTP request counts.
- Keep one capture owner per review request, share frozen evidence with delegates, and repair drafts in the retained context without automatic recapture after terminal failures.
- Treat a complete ancestor no-op merge as empty required coverage while retaining snapshots, raw patches and peer gaps.
- Seal one fixed base-to-head commit path without historical trees, and independently verify raw commit hashes and parent edges before granting ancestor exemptions in publication and retained-draft validation.
- Synchronize executable file mode in ancestor fixtures so Linux checkout observes the same committed state as Windows.
- Add inclusive line ranges for fixed reads and search-ID filtering for artifact views; preserve full-content hashes, original line numbers and the search ledger.
- Document complete contract JSON, inventory keys and raw artifact versus stdout wrapper shapes. Validation covers helper regressions; no new model-quality benchmark is claimed.

## 0.16.0 — 2026-10-20 (local candidate)

- Share a fail-closed Git network process-tree lease with GitLabWorkspace; fetch only explicit branches without submodule recursion or automatic maintenance.
- Add opt-in v2 summaries, raw batched blob reads, shared-content output and checksum-addressed search views while keeping complete fixed evidence on disk.
- Serialize manifest writers and reject stale generations; require final-version judgments for necessary merge-parent views without reviving net-zero or repaired historical changes.
- Preserve file symlink boundaries, accept UTF-8 BOM drafts/requests and canonicalize packaged text to LF while retaining binary bytes.
- Add isolated source-only quality canaries and latest-HEAD helper comparison preparation; model acceptance and performance claims require separate recorded runs.

## 0.15.0 — 2026-10-20

- Keep one Repo/base/head request and unified agent review using Codex Code Review criteria, without a runtime model preset.
- Pin all Repo SHAs before snapshot sealing; group explicit remote branch fetches once per remote and keep post-capture operations local.
- Publish one Traditional Chinese report with five common sections, additive engineering details, validated technical records and sensitive evidence omission.
- Preserve v3 drafts, all priorities, fixed citations, editable confirmations and historical reports.
- Use the GitHub JSON media type for Actions artifact downloads while preserving binary media negotiation for Release assets.

## 0.14.0 — 2026-10-16

- Seal starting versions, merge previews and applicable instructions in independent evidence repositories; later ref changes and source garbage collection do not change the captured review.
- Add optional, validated pending confirmations and editable human confirmation records while retaining confirmed findings and preventing an unconditional pass.
- Prefer an installed Output Style Skill for presentation and retain the original standalone style when absent.

## 0.13.0 — 2026-10-16

- Add compact capture summaries, ordered batch reads/searches and operation-scoped verification caches while retaining complete fixed evidence and v3 validation.
- Render each finding as problem, trigger, expected result, actual result, fixed line evidence and recommended change. Preserve complete refs in citations and omit the technical appendix.
- Require explicit line ranges for non-empty finding evidence; retain empty-file evidence and incomplete-review behavior.
- Package the native Skill for GitlabWorkSpace 0.24.4; retain the three branch-review inputs.

## 0.12.2 — 2026-10-15

- Apply the shared Concise style from the start of each task to progress messages, answers and newly created or updated documents; preserve full evidence, errors and requested explanations.

- Streamline the Skill entrypoint, load workflow references on demand and add a focused style reference; retain the v3 review/report contract.

## 0.12.1 — 2026-10-15

- Use the first 8 characters of the captured base/head branch tip commits in report filenames; retain UTC microsecond timestamps and collision protection.
- Show the selected Repo/path, input branches, resolved refs, short commits, merge direction and merge-base-to-head diff range after the conclusion; retain full SHAs in the technical appendix.
- Update report examples and verify the format through local/remote branch, divergent checkout, installed-package and BDD coverage.

## 0.12.0 — 2026-10-15

- Use native Codex Code Review criteria; separate evidence rules and concise Merge Report style.
- Require one Repo plus explicit base/head branches. Remove quick, local-change, tag/SHA, direct, multi-seed, MR and receipt entry points.
- Publish workspace context/result v3. Enable impact extension only for at least two declared Git roots; retain one selected Repo and captured candidate HEADs.
- Publish one Markdown Merge Report named `merge-review-{UTC timestamp}-{base commit}-{head commit}.md` directly in the selected Repo report directory; remove JSON report export while preserving supported findings and evidence gaps.
- Ship the Traditional Chinese usage guide, examples, troubleshooting and breaking-change migration instructions.
- Integrate with GitlabWorkSpace 0.24.0 through the native branch-review interface. Older Workspace packages pinned to MergeReviewer 0.11.0 require a full VSIX/workflow-kit upgrade rather than replacing the Skill in place.

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
