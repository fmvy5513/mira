# GitLab review changes and rollout boundary

This release contains two distinct changes. Disabling delivery eligibility does not undo the general diff-completeness repair.

## General GitLab diff-completeness repair

All GitLab reviews retrieve the pinned, collected diff version instead of relying on the potentially collapsed legacy changes response. They reject incomplete versions and source revisions that change during retrieval. This deliberately changes normal review failure behavior: incomplete input produces a visible failure rather than a misleading partial review.

Verified binary, empty-file, pure-rename and mode-only metadata can coexist with reviewed text files. Metadata paths are explicitly skipped, never counted as reviewed source. Verification permits at most 20 metadata files and a 30-second verification budget. Unknown empty text fragments fail closed. These bounds apply even when delivery eligibility is disabled.

Before rollout, exercise an ordinary text MR, a formerly collapsed large diff, mixed text/binary and rename MRs, and malformed/incomplete versions. Monitor input failures and review latency. Roll back this general behavior by redeploying the previous reviewed image; changing `MIRA_GITLAB_DELIVERY_PROJECTS` alone is not a rollback of input acquisition or chunk-completeness changes.

## Opt-in delivery evidence

`MIRA_GITLAB_DELIVERY_PROJECTS` lists explicit `group/project` names. Only these projects receive delivery eligibility statuses and evidence packages, pinned full-review semantics, and final revision/discussion checks. An empty list disables these additional status/package side effects. Mira never approves or merges an MR.

Only a collected, complete version can start a delivery review. Current source and target are rechecked after discussion enumeration and again after evidence upload, before publishing the terminal status. Consumers must still re-read current GitLab revisions: separate API requests cannot provide an atomic publication guarantee. A manifest alone, without its matching successful status and current revisions, never authorizes release.

Deploy with the opt-in list empty first. Validate ordinary reviews before enabling only the authorized pilot project. Then exercise eligible review evidence and negative cases: changed source/target, incomplete input, unresolved findings, skipped metadata, failed chunks and interrupted publication. Record the deployed commit and exact tested revisions. Production release automation remains disabled until its separate integration and acceptance gates pass.
