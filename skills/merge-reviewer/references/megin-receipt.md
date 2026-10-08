# Optional single-Repo Megin receipt binding

Use `--megin-receipt <receipt.json>` with `git_review_context.py` when the user
requests a review of a completed Megin local delivery. The receipt schema is
`megin-repo-delivery-receipt/v1`; Group delivery receipts remain a Workspace
extension concern. This helper uses Python stdlib and Git and works without
Megin installed.

```text
python <skill-dir>/scripts/git_review_context.py --workspace <Repo> --base <base-SHA> --head <feature-or-merge-SHA> --mode direct --no-fetch --megin-receipt <receipt.json> --format json --pretty
```

Only fixed committed scope is accepted. Head must be the receipt's exact feature
or merge commit; `--include-working-tree` is rejected. Existing capture and report
commands retain their behavior when the option is omitted.

Capture validates the receipt digest, Repo root, Work ID, plan version,
base/feature/merge ancestry, accepted tree, fixed committed input bytes, record
hashes, passing checks, independent review claims, human acceptance and the
original delivery gate output. Later product or Skills changes may coexist with
a historical review. Protected plans and raw evidence must remain intact, and
the pinned Git objects must still be available.

The context includes `megin_binding` with schema `megin-repo-review-binding/v1`,
Work ID, plan version, receipt SHA-256, head SHA and the receipt. Report publication
revalidates that binding and its historical proof. Markdown records the delivery
identity and digest; optional JSON retains the complete binding. Changed records
or a mismatched head block publication and trigger owned-context cleanup.

Use normal per-path review coverage and fixed Git version evidence. The receipt
binds a local delivery and does not replace review, prove test relevance, assert
remote publication or claim an MR was merged.
