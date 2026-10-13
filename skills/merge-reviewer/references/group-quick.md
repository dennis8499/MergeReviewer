# Group quick review

Run `git_review_context.py --group-root <Group> --quick` from the declared Group.
This uses workspace context/result v2 and the full impact workflow in
[workspace-review.md](workspace-review.md), with internal target `quick`.
It requires no remote or branch comparison.
`--workspace <Group> --quick` and `--scope auto|group --quick` select the same
dual-source workflow when the declared workspace is a Group and no target or
base/head was supplied. An explicit `--target branch` retains branch quick
comparison; a Git-root workspace keeps the single-Repo quick behavior.

Each Repo has independent fixed `index` and `working-tree` sources compared
against HEAD (empty tree for unborn Repos). Read both patches and cover every
required `(repo, source, path)`. A working-file correction cannot hide a staged
regression. Keep source-specific contract anchors, evidence and findings; combine
duplicate findings only when their trigger and failure match, retaining all
affected sources.

Register changed contracts and follow upstream/downstream consumers across all
declared Group Repos, including unchanged ones. Search every changed/propagated
contract in each candidate's index and working-tree separately. Exclusions need
two distinct relevant terms per contract/source and concrete reasons. Failed
captures and unresolved links remain incomplete. Cycles are allowed and visited
nodes are deduplicated. Applicable source/alias evidence must be fixed.
For a related candidate, each source with a contract match needs its own reviewed
impact node associated with that contract. A reviewed working-file node cannot
stand in for the index; an omitted or unreviewed source keeps the result incomplete.

Optional `--supporting-sources <JSON>` retains `{repo, commit, paths}` requests.
`repo` identifies one captured Repo by ID, unique name or canonical path; `commit`
is a full available immutable SHA. Sources are named `supporting-<SHA>` and can
supplement unchanged contract/consumer evidence. They do not replace candidate
index/working searches or create changed-file coverage.

Publish with `review_report.py`, optional `--include-json`. Output goes to
`<Group>/review-reports/merge-review-<run-id>/summary.md` plus involved Repo details.
Source drift and missing coverage prevent complete submission recommendations.
Publication always clears the owned context; interrupted reviews use
`review_session.py cleanup --context-dir <context>`. Historical Group v1 contexts
remain readable with their historical validator and cannot establish v2 coverage.
