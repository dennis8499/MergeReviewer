# Fixed MR tasks and portable reports

Use `git_review_context.py --mr-context <task.json>` for a GitlabWorkSpace MR.
The MergeReviewTask/v1 contains origin, projectId, mrIid, sourceProjectId,
targetProjectId, sourceBranch, targetBranch, sourceSha, targetSha, repoPath,
sourceRemoteUrl, targetRemoteUrl and mode. repoPath is the actual local Git root,
not a GitLab namespace. targetSha is the current target branch tip, not the
historical diff_refs.start_sha. Forks retain separate source/target identities.

The helper obtains the exact source and target objects, including from their
respective remotes when absent. Missing objects stop review without guessing a
branch or falling back to local HEAD. Legacy schema-4 comparison and evidence
rules remain in effect; task metadata is bound to that context.

Use the normal validated review-report workflow. For an MR-bound context the
renderer additionally embeds base64 MergeReviewReport/v1 metadata in Markdown
and automatically writes a JSON companion with report_metadata and report_body.
Both artifacts identify the GitLab origin, MR/project, source and target SHAs,
actual comparison base, context digest and normalized body SHA-256. Ordinary
single-Repo JSON output remains optional.

Paste the complete Markdown including its metadata or import its JSON companion
into GitlabWorkSpace. Editing the body invalidates its digest; regenerate or
reimport a valid report. Old plain text is an unverified general comment.
Workspace verifies identity, body digest and live source/target versions before
publication. A report may explicitly be incomplete or contain P0/P1 findings;
publication only checks version binding. Human approval/merge and GitLab rules
remain separate. The Skill never posts comments or approves/merges an MR.
