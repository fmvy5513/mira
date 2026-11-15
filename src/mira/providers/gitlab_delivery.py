"""Fail-closed eligibility facts for GitLab delivery review statuses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from urllib.parse import quote

from mira.exceptions import ProviderError
from mira.models import PRInfo, ReviewResult, Severity
from mira.providers.base import DeliveryReviewSession


def eligibility(
    result: ReviewResult,
    changed_paths: list[str],
    revision_current: bool,
    unresolved_thread_ids: list[str],
) -> dict:
    reasons = []
    if not revision_current:
        reasons.append("REVISION_CHANGED")
    if not changed_paths or set(changed_paths) - set(result.reviewed_paths):
        reasons.append("REVIEW_COVERAGE_INCOMPLETE")
    if getattr(result, "incremental_since", ""):
        reasons.append("FULL_REVIEW_REQUIRED")
    if (
        result.skipped_reason
        or result.skipped_paths
        or result.failed_paths
        or result.failed_chunks
        or result.security_failures
        or result.dependency_failures
    ):
        reasons.append("REVIEW_INCOMPLETE")
    blocking = sum(comment.severity >= Severity.WARNING for comment in result.comments)
    if blocking or result.key_issues:
        reasons.append("BLOCKING_FINDINGS")
    if unresolved_thread_ids:
        reasons.append("UNRESOLVED_BOT_THREADS")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "changed_paths": sorted(set(changed_paths)),
        "reviewed_paths": sorted(set(result.reviewed_paths)),
        "blocking_findings": blocking,
        "unresolved_thread_ids": sorted(unresolved_thread_ids),
    }


@dataclass(frozen=True, kw_only=True)
class GitLabDeliverySession(DeliveryReviewSession):
    context: str
    start_status_id: int
    pipeline_id: int
    version_id: int
    diff_refs: dict[str, str]
    changes: list[dict]
    changed_paths: list[str]

    def __post_init__(self) -> None:
        if any(
            not re.fullmatch(r"[0-9a-f]{40}", value)
            for value in (self.source_sha, self.target_base_sha)
        ):
            raise ProviderError("Invalid delivery revision identity")
        if any(
            type(value) is not int or value <= 0
            for value in (self.start_status_id, self.pipeline_id, self.version_id)
        ):
            raise ProviderError("Invalid delivery session identity")
        if not self.context or not self.changed_paths or not self.changes:
            raise ProviderError("Incomplete delivery session")

    def review_inputs(self) -> dict:
        return {"diff_refs": self.diff_refs, "changes": self.changes}


class DeliveryReviewMixin:
    """Opt-in GitLab exact-input review producer; never an MR approval producer."""

    def requires_full_delivery_review(self, pr_info: PRInfo) -> bool:
        allowed = {
            value.strip()
            for value in os.environ.get("MIRA_GITLAB_DELIVERY_PROJECTS", "").split(",")
            if value.strip()
        }
        return f"{pr_info.owner}/{pr_info.repo}" in allowed

    def _delivery_key(self, pr_info: PRInfo) -> tuple:
        return pr_info.owner, pr_info.repo, pr_info.number, pr_info.head_sha

    async def start_delivery_review(self, pr_info: PRInfo) -> GitLabDeliverySession | None:
        if not self.requires_full_delivery_review(pr_info):
            return None
        if not re.fullmatch(r"[0-9a-f]{40}", pr_info.head_sha):
            raise ProviderError("Delivery review requires an exact source SHA")
        pipelines = (
            await self._request("GET", f"{self._mr(pr_info)}/pipelines?per_page=100")
        ).json()
        eligible_pipelines = [
            pipeline
            for pipeline in pipelines
            if pipeline.get("sha") == pr_info.head_sha
            and pipeline.get("source") in {"merge_request_event", "push", "web", "pipeline"}
        ]
        if not eligible_pipelines:
            raise ProviderError("An application source pipeline is required before delivery review")
        pipeline_id = max(eligible_pipelines, key=lambda entry: entry["id"])["id"]
        context = f"panel/mira-eligibility/{pr_info.number}/{uuid.uuid4().hex}"
        versions = (await self._request("GET", f"{self._mr(pr_info)}/versions?per_page=100")).json()
        version = next(
            (entry for entry in versions if entry.get("head_commit_sha") == pr_info.head_sha), None
        )
        if version is None:
            raise ProviderError("The exact input diff version is unavailable")
        data = (await self._request("GET", f"{self._mr(pr_info)}/versions/{version['id']}")).json()
        diffs = data.get("diffs")
        if (
            data.get("head_commit_sha") != pr_info.head_sha
            or not isinstance(diffs, list)
            or data.get("overflow")
            or data.get("state") == "overflow"
            or any(d.get("too_large") or d.get("collapsed") for d in diffs)
            or not str(data.get("real_size", "")).isdigit()
            or int(data["real_size"]) != len(diffs)
        ):
            raise ProviderError("The input diff version is incomplete")
        target = (
            await self._request(
                "GET",
                f"{self._project(pr_info)}/repository/branches/{quote(pr_info.base_branch, safe='')}",
            )
        ).json()
        target_sha = target.get("commit", {}).get("id")
        if not target_sha or data.get("base_commit_sha") != target_sha:
            raise ProviderError(
                "The candidate must be integrated with current target before review"
            )
        changed_paths = sorted({entry.get("new_path") or entry.get("old_path") for entry in diffs})
        if not changed_paths or any(
            not isinstance(path, str) or not path for path in changed_paths
        ):
            raise ProviderError("The input diff paths are incomplete")
        response = (
            await self._request(
                "POST",
                f"{self._project(pr_info)}/statuses/{pr_info.head_sha}",
                data={
                    "state": "pending",
                    "name": context,
                    "pipeline_id": pipeline_id,
                    "description": "panel-review-v1;start=pending;review in progress",
                    "target_url": pr_info.url,
                },
            )
        ).json()
        try:
            status_id = response["id"]
            if type(status_id) is not int or status_id <= 0:
                raise ProviderError("Invalid delivery start status identity")
            handle = GitLabDeliverySession(
                context=context,
                start_status_id=status_id,
                pipeline_id=pipeline_id,
                source_sha=pr_info.head_sha,
                target_base_sha=target_sha,
                version_id=version["id"],
                diff_refs={
                    "head_sha": pr_info.head_sha,
                    "base_sha": data.get("base_commit_sha"),
                    "start_sha": data.get("start_commit_sha"),
                },
                changes=diffs,
                changed_paths=changed_paths,
            )
        except Exception:
            await self._request(
                "POST",
                f"{self._project(pr_info)}/statuses/{pr_info.head_sha}",
                data={
                    "state": "failed",
                    "name": context,
                    "pipeline_id": pipeline_id,
                    "description": "Delivery review startup failed",
                    "target_url": pr_info.url,
                },
            )
            raise
        if not hasattr(self, "_delivery_inputs"):
            self._delivery_inputs = {}
        self._delivery_inputs[self._delivery_key(pr_info)] = handle
        return handle

    async def finish_delivery_review(
        self, pr_info: PRInfo, handle: DeliveryReviewSession | None, result: ReviewResult | None
    ) -> None:
        if handle is None:
            return
        if not isinstance(handle, GitLabDeliverySession):
            raise ProviderError("A GitLab delivery session is required")
        current = (await self._request("GET", self._mr(pr_info))).json()
        target = (
            await self._request(
                "GET",
                f"{self._project(pr_info)}/repository/branches/{quote(pr_info.base_branch, safe='')}",
            )
        ).json()
        current_revision = (
            current.get("sha") == handle.source_sha
            and current.get("target_branch") == pr_info.base_branch
            and target.get("commit", {}).get("id") == handle.target_base_sha
        )
        # Unknown discussion state fails closed; comments being resolved is not
        # independent proof that a code fix is correct. A full current review is still required.
        discussions = []
        for page in range(1, 101):
            batch = (
                await self._request(
                    "GET", f"{self._mr(pr_info)}/discussions?per_page=100&page={page}"
                )
            ).json()
            discussions.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ProviderError("Discussion verification exceeds its bounded limit")
        identity = (await self._request("GET", f"{self._api}/user")).json()
        unresolved = [
            str(discussion["id"])
            for discussion in discussions
            if any(
                note.get("author", {}).get("id") == identity.get("id")
                and note.get("resolvable")
                and not note.get("resolved")
                for note in discussion.get("notes", [])
            )
        ]
        facts = (
            eligibility(result, handle.changed_paths, current_revision, unresolved)
            if result is not None
            else {"eligible": False, "reasons": ["REVIEW_FAILED"]}
        )
        payload = {
            "version": 1,
            "mr_iid": pr_info.number,
            "source_sha": handle.source_sha,
            "target_base_sha": handle.target_base_sha,
            "diff_version_id": handle.version_id,
            "start_status_id": handle.start_status_id,
            "issuer_id": identity.get("id"),
            **facts,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        package = f"{self._project(pr_info)}/packages/generic/panel-mira-review/{handle.source_sha}/{pr_info.number}-{handle.start_status_id}.json"
        await self._request(
            "PUT", package, content=raw, headers={"Content-Type": "application/json"}
        )
        await self._request(
            "POST",
            f"{self._project(pr_info)}/statuses/{handle.source_sha}",
            data={
                "state": "success" if facts["eligible"] else "failed",
                "name": handle.context,
                "pipeline_id": handle.pipeline_id,
                "target_url": pr_info.url,
                "description": f"panel-review-v1;start={handle.start_status_id};evidence={digest}",
            },
        )
