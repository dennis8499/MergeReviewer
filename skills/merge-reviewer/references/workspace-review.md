# Portable Repo and Group code review

This is the default workflow for new branch, Stage, and uncommitted review requests. Codex reviews semantics directly; no nested Codex CLI or external GitLab service is required. Read `review-rules.md` for actionable findings, P0–P3 priorities, plain-language summaries and regression attribution.

## Prepare the boundary and target

```powershell
python <skill>/scripts/git_review_context.py --workspace <root> --scope auto --target branch --project <Repo-or-path> --base <ref> --head <ref> --pretty
python <skill>/scripts/git_review_context.py --workspace <root> --scope auto --target staged --project <Repo-or-path> --pretty
python <skill>/scripts/git_review_context.py --workspace <root> --scope auto --target uncommitted --project <Repo-or-path> --pretty
```

Add `--workspace-file <file.code-workspace>` for the actual VS Code folders, or `--group-root <root>` for an explicit local Group. Repo IDs in the manifest are unique even when folder names repeat. Use exact paths to disambiguate a requested Repo. `--scope repo` keeps evidence inside the selected project. Auto selects Group for multiple visible Repos or a declared non-Git container holding Repos, including a container with only one Repo. A declared Git root or a workspace file listing only that Git root remains Repo. Discovery stays inside declared roots and stops at Git boundaries, including worktree `.git` files.

Choose one boundary: a workspace file cannot override an explicit Group root or a
Group MR/receipt root. Changed workspace-folder bytes or a changed candidate Repo
inventory after capture keep the result incomplete until the review is repeated.

`--target branch` defaults to merge mode, accepts `--mode direct` and local/remote refs or SHAs, and retains merge-parent and merge-preview evidence. `--quick` may choose the seed's remote default base. With multiple Repos, identify the branch-review seed explicitly. Related Repos use their own captured HEAD; their local edits are excluded.

Local targets have no base/head/remote/quick arguments. Stage uses the real index's contents, including split-index semantics, in alternate storage. Uncommitted uses final saved files, so a staged version overwritten by a later working edit is reviewed only with the staged target. It clears assume-unchanged flags and present skip-worktree flags only in the copied index; absent sparse-checkout paths keep their index entries. Unborn Repos compare with an empty tree. Generated untracked `review-reports/` are excluded. For local Group review without a selected Repo, all changed Repos are seeds. Unchanged Repos remain candidates until evidence establishes a relation.

All snapshots live in an owned system-temp context. Original HEAD, index, branches and files remain unchanged. The manifest contains `schema: merge-reviewer-workspace-context/v2`, `scope`, `target`, `repositories`, `seed_repos`, `required_coverage`, `changed_contracts`, `searches` and `context_gaps`. Each Repo has an ID, canonical path, captured sources, patches, changes and preparation limitations. Source entries give full commit/tree refs and Git blob identities for every path. Generated index/working/merge objects remain in the owned context; committed objects are read by immutable ID.

## Follow the actual impact chain

1. Read the manifest, every seed patch, relevant base content and merge-parent patches. Inspect all changed/old/merge-preview paths. Use `inventory` to find entrypoints, dependency manifests, API/event/data contracts, tests and applicable root/nested `AGENTS.md` rules.
2. Extract changed contracts and behaviors from code. Search their exact symbols, route/field names, message topics, package references, table/column names and config keys across candidates. Search plausible aliases or wrappers when the first query is insufficient; a single negative name search cannot establish semantic independence.
3. Read each relevant match from its recorded source. Follow callers toward entrypoints and consumers downstream, including adapters and later events. Repeatedly search newly discovered contracts until the chain closes. Search the seed's merge-preview source separately when integration changes the behavior. A related Repo's current source always comes from its selected mode, even if a same-named feature branch exists.
4. Record graph nodes and connections with evidence and a concrete reason. Deduplicate revisits by Repo/source/path/behavior; cycles are allowed. Link every node to a changed root. Record a disposition and search IDs for every candidate Repo. Unrelated candidates are listed as excluded, not expanded into full code audits.
5. If a dynamic destination, missing Repo/source, submodule, large/binary file or other required link cannot be reviewed, record an unresolved Repo or an unreviewed connected node and explain the gap. Keep supported findings, but the overall result is incomplete. Never equate the existence of frozen files or a successful validator with semantic completeness.

Use these commands instead of rereading mutable working files:

```powershell
python <skill>/scripts/review_workspace.py inventory --context-dir <context>
python <skill>/scripts/review_workspace.py search --context-dir <context> --contract C-001 --query <literal-contract-or-symbol>
python <skill>/scripts/review_workspace.py search --context-dir <context> --repo repo-000 --source merge-preview --contract C-001 --query <literal>
python <skill>/scripts/review_workspace.py read --context-dir <context> --repo repo-001 --source head --path src/client.py
```

Register each changed contract before search. Write one contract JSON with `id`, `root` (an exact required_coverage row), `kind` (`api|event|package|data|table|config|behavior`), `symbol`, `evidence` (one frozen line anchor) and `search_terms` (`query`, `reason`). The symbol and terms must occur in that anchor; an alternate term outside it needs its own frozen `evidence` alias/wrapper anchor. New downstream contracts add `parent_ids`, retaining their parent's changed root. Every changed Group path needs a contract or explicit incomplete coverage. Run `review_workspace.py register-contract --context-dir <context> --contract-file <contract.json>`, then add `--contract <ID>` to each Group search. Search is literal, includes all matches, records its contract and fixed source/ref and returns `S-0001` IDs. Repeat `--repo` to select several Repos; omit it to search all candidates. Read output has numbered lines, full source ref and SHA-256. Binary read failures require a coverage limitation. `inventory` lists source paths, modes and blob IDs. Do not use live grep to establish version-specific findings.

## Draft contract

Write the draft inside the owned context. This one-file staged example is the minimum complete shape; replace IDs, refs, paths, ranges and conclusions with actual reviewed evidence:

```json
{
  "schema": "merge-reviewer-workspace-result/v2",
  "summary": "已檢查所選變更與相關流程，未建立有證據的問題。",
  "coverage": [{
    "repo": "repo-000", "source": "index", "path": "app.py", "status": "reviewed",
    "impact_ids": ["I-001"],
    "evidence": [{"repo": "repo-000", "source": "index", "ref": "<full fixed tree SHA>", "path": "app.py", "line_start": 1, "line_end": 3}]
  }],
  "impact_coverage": [{
    "id": "I-001", "repo": "repo-000", "source": "index", "path": "app.py", "status": "reviewed",
    "reason": "入口的驗證與錯誤處理維持原有行為，已檢查呼叫端及測試。",
    "related_to": [], "contract_ids": ["C-001"],
    "evidence": [{"repo": "repo-000", "source": "index", "ref": "<full fixed tree SHA>", "path": "app.py", "line_start": 1, "line_end": 3}]
  }],
  "repository_coverage": [{"repo": "repo-000", "status": "seed", "reason": "使用者選定的審查起點。", "search_ids": []}],
  "findings": [],
  "next_steps": [{"owner": "開發", "action": "依審查結論處理。"}],
  "tests_executed": [],
  "tests_not_executed": ["僅執行靜態程式審查，未執行專案測試。"],
  "limitations": []
}
```

- `coverage`: exactly every `(repo, source, path)` in `required_coverage`, including old rename paths and merge preview. Status is `reviewed`, `metadata-only` or `not-reviewed`; the latter two need `reason` and force incompleteness. Reviewed entries need matching fixed evidence and `impact_ids` for their own root nodes. Deleted paths cite the actual comparison baseline: merge-base for merge-mode head changes, base for direct/local targets, or head for preview changes. Binary/submodule metadata is a limitation.
- `impact_coverage`: registered `contract_ids`, unique ID, Repo/source/path, `reviewed|not-reviewed`, evidence, `reason`, and `related_to` IDs. Connect callers/contracts/consumers; every node must be reachable from changed roots. An unresolved link inside a captured Repo can be a `not-reviewed` node with a reason and no fabricated evidence. Connections are undirected for reachability; explain the actual call/data direction in `reason`.
- `repository_coverage`: every captured Repo exactly once, with `seed|related|not-related|unresolved`, `reason` and `search_ids`. Each Repo needs searches for every changed/propagated contract anchored in other Repos. A non-seed needs completed searches against its main fixed source before claiming related or unrelated. Exclusions require at least two distinct relevant queries per contract, with reasons for unrelated matches as well as misses. Group quick needs separate index and working-tree searches. A related Repo needs a fixed contract search match. Every registered contract needs its own connected impact anchor. A failed capture remains unresolved. Missing external runtime consumers are recorded in the affected captured Repo's unreviewed impact node and reason; never invent a Repo ID.
- `findings`: sequential IDs (`F-001`...), P0–P3 order, `title`, `impact`, `trigger`, `expected`, `actual`, `recommendation`, `owner`, `scenario_source` and evidence. Each finding must cite a reviewed seed change in the correct source and may supplement it with unchanged consumer/contract evidence. Do not create fake changed coverage for unchanged files. Distinguish base-version defects and safe changes from regressions. Static scenarios are `code-derived`; only executed reproductions are `reproduced`.
- Evidence uses captured Repo ID, source name, exact source ref, relative path and matching line range. Source names are listed by the manifest: `head`, `base`, `merge-base`, `merge-preview`, `parent-<SHA>`, `index`, or `working-tree`. Never swap source refs merely because their file names match.
- `limitations` contains required evidence/impact gaps and forces an incomplete conclusion. Record checks that were not run separately in `tests_not_executed`; do not silently turn unresolved links into test omissions.

## Publish and stop

```powershell
python <skill>/scripts/review_report.py --context-dir <context> --result <context>/draft.json
```

Add `--include-json` only when requested. New reports live at `<Repo-or-Group>/review-reports/merge-review-<run-id>/summary.md`, with one `repo-XXX.md` per seed/related Repo and optional `result.json`. Summary severity counts are global; detail reports retain global finding IDs. Source versions, exclusions, searches, change coverage and impact coverage remain visible even without JSON output. Branch reports recommend merging; local reports recommend submitting.

Publication validates refs/blobs/line ranges, source-specific coverage, graph reachability, candidate dispositions, findings and version drift. Unreviewed required links keep `審查未完成`; incomplete evidence never becomes a passing recommendation. Publishing through `review_report.py` removes the owned context on success and failure. When stopping before publication, run `review_session.py cleanup --context-dir <context>`. Rebuild context after a failed publication.

All new Group calls use this v2 contract. `--group-root <Group> --quick` uses target `quick` internally and covers both index and working-tree against HEAD. New Group MR adapters provide a validated MergeReviewTask/v2 with explicit groupRoot and fixed source/target SHA; the binding is verified at capture and publication. Native Megin Repo/Group receipt v2 binds every delivered Repo as a seed and revalidates historical evidence during publication. Keep schema-4, workspace v1 and legacy Group v1 contexts separate for historical verification; none of their old evidence is promoted to a complete v2 cross-Repo review.
