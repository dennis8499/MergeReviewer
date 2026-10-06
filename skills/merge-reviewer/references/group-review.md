# Group quick review

For quick review from a non-Git Group folder, resolve that Group explicitly and
run `git_review_context.py --group-root <Group> --quick`. Do not substitute the
installed Skill directory. This is distinct from legacy single-Repo quick
review: no remote discovery, fetch, branch comparison or merge preview.

The helper scans only direct-child Git roots (including worktrees), compares
each Repo's index and final working files separately to HEAD, and freezes
patches and evidence outside the target Repos. An unborn Repo compares to an
empty tree. Untracked non-ignored files and forced ignored staged files are
included. Unsafe roots, conflicts and dirty submodules produce an explicit
per-Repo error while other Repos continue.

Read manifest.json and both patches for every captured Repo. Read only frozen
files listed by evidence_files while reasoning about versions; never replace
index evidence with a newer working file. Compare context before reporting;
changed live state makes the submission recommendation incomplete. Binary and
submodule changes require explicit coverage limitations.

Write this independent result contract inside the context directory:

```json
{
  "schema": "merge-reviewer-group-result/v1",
  "summary": "白話摘要",
  "coverage": [{
    "repo": "service", "source": "index", "path": "app.py", "status": "reviewed",
    "evidence": [{"repo": "service", "source": "index", "path": "app.py",
      "ref": "<versions.index tree SHA>", "side": "result", "line_start": 1, "line_end": 3}]
  }],
  "findings": [],
  "limitations": []
}
```

Coverage includes every `(repo, source, path)` in changed_files, including old
rename paths. Sources are index and working-tree. A deletion/old path may cite
the matching base-side frozen record. metadata-only/not-reviewed requires a
reason and prevents a complete result. Findings use priority, title, impact,
trigger, expected, actual, recommendation and frozen evidence. Combine the same
issue's evidence from both sources; the validator also merges exact duplicate
behavior findings and retains all source citations. Never merge different
version-specific failures.

Run `review_report.py --context-dir <context> --result <draft>`; optional
`--include-json` retains the structured report. The report command removes the
owned system-temporary context after successful or failed publication. If a
review ends before publication, run `review_session.py cleanup --context-dir
<context>` first. The default output is
`<Group>/review-reports/<run-id>/summary.md` and one report per Repo. Use
submission recommendations, not merge recommendations. Show all Repo states,
P0–P3 counts, findings, source/version citations and uncovered paths. A failed
publication clears the current owned context; rebuild it when trying again.

This review does not replace Megin's independent review/verification/acceptance
gates and never stages, commits or fixes products.
