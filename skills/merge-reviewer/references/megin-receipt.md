# Optional Megin delivery receipt binding

Use `--megin-receipt <receipt.json>` when reviewing a completed native Megin local
delivery. Repo and Group receipt v2 work without installing Megin; the bundled
read-only receipt runtime validates historical records with their versioned
policy. It does not initialize, update or execute a Megin workflow.

```text
python <skill>/scripts/git_review_context.py --workspace <Group> --megin-receipt <receipt.json> --mode direct --no-fetch --pretty
```

The receipt's explicit workspace root and delivered Repo rows become fixed branch
seeds. All delivered seeds must match the exact base and feature/merge commits;
other declared Group Repos remain candidates at their own fixed HEAD. Group
context/result v2 still requires anchored changed contracts, downstream searches,
impact coverage and explicit Repo dispositions. A receipt never replaces this
semantic review or asserts remote publication.

To review one Repo from a multi-Repo delivery, explicitly use `--scope repo
--project <delivered-Repo>`. An ambiguous selection is rejected. The full receipt
is still verified, while `reviewed_repositories` records only the selected seed;
the remaining delivery rows are provenance, not claimed review coverage. A Repo
receipt used inside a declared Group keeps the other Group Repos as candidates.

Capture checks digest, workspace/work/plan identity, protected record hashes,
fixed inputs, ancestry and trees, approval, discovery, scenario/TDD proof,
independent review, human acceptance, passing checks and delivery gate output.
New Group evidence includes architecture v2 and actual-diff impact confirmation.
Later source changes may coexist with historical delivery review, but protected
records and pinned objects must remain intact.

Portable manifests retain `megin-review-binding/v2` with the receipt, its digest
and exact reviewed Repo/base/head rows. Publication revalidates the historical
proof and entire binding after rendering, before publishing files. Tampering or
ref mismatches block publication and trigger owned-context cleanup.

Repo receipt v1 remains supported with its historical validator, including native
contract/evidence v1 and v2. Portable reviews label that binding
`megin-historical-review-binding/v1`; it supplies provenance only. Overlay-specific
Group receipt v1 stays in the Workspace's isolated historical verifier and must
not be upgraded into a new Group completion claim. Existing contexts keep their
original schema validators; do not convert old evidence in place.
