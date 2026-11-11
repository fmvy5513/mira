"""Review rhet dev/main pushes and publish on the immutable head commit."""

import json
import logging
import re
import time
from urllib.parse import quote

from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.dashboard.models_config import llm_config_for
from mira.index.store import IndexStore
from mira.llm import create_llm
from mira.models import ReviewResult, Severity
from mira.platforms.auth import PlatformAuth
from mira.providers.gitlab import GitLabProvider, _build_unified_diff

REFS = {"refs/heads/dev", "refs/heads/main"}
ZERO = "0" * 40
_running: set[tuple[str, str, str, str]] = set()
logger = logging.getLogger(__name__)


def matches(payload: dict) -> bool:
    path = payload.get("project", {}).get("path_with_namespace") or ""
    return (
        path.startswith("rhet/")
        and all(part and part not in {".", ".."} for part in path.split("/"))
        and payload.get("ref") in REFS
        and all(re.fullmatch(r"[0-9a-f]{40}", str(payload.get(k, ""))) for k in ("before", "after"))
        and payload["after"] not in (ZERO, payload["before"])
    )


async def review_push(payload: dict, auth: PlatformAuth, bot_name: str) -> None:
    before, head = payload["before"], payload["after"]
    path = payload["project"]["path_with_namespace"]
    branch = payload["ref"].removeprefix("refs/heads/")
    key = path, branch, before, head
    if key in _running:
        return
    _running.add(key)
    try:
        provider = GitLabProvider(await auth.get_token())
        project = f"{provider._api}/projects/{quote(path, safe='')}/repository"
        commit = f"{project}/commits/{head}"
        marker = f"<!-- mira-{branch}-push:{before}:{head} -->"
        bot = await auth.get_bot_identity()
        notes = await provider._paginate(f"{commit}/comments")
        if bot and any(
            n.get("author", {}).get("username") == bot and marker in n.get("note", "")
            for n in notes
        ):
            return
        if before == ZERO:
            changes = await provider._paginate(f"{commit}/diff")
        else:
            response = await provider._request(
                "GET", f"{project}/compare", params={"from": before, "to": head, "straight": "true"}
            )
            comparison = response.json()
            if comparison.get("compare_timeout"):
                raise RuntimeError("GitLab compare timed out")
            changes = comparison["diffs"]
        diff = _build_unified_diff(changes)
        started = time.monotonic()
        cfg = load_config()
        engine = ReviewEngine(
            config=cfg,
            bot_name=bot_name,
            llm=create_llm(llm_config_for("review", cfg.llm)),
            security_llm=create_llm(llm_config_for("security", cfg.llm)),
            indexing_llm=create_llm(llm_config_for("indexing", cfg.llm)),
        )
        result = await engine.review_diff(diff)
        parts = [
            f"### Mira {branch} push review · `{head[:8]}`",
            result.skipped_reason or result.summary,
        ]
        if result.walkthrough and result.walkthrough.summary:
            parts.append(result.walkthrough.summary)
        for c in result.comments:
            parts.append(f"**{c.severity.name}** · `{c.path}:{c.line}` · {c.title}\n\n{c.body}")
        if result.skipped_paths or any(
            not ch.get("diff") or ch.get("too_large") or ch.get("collapsed") for ch in changes
        ):
            parts.append(
                "Coverage is partial: some paths were skipped or GitLab omitted their diff."
            )
        note = "\n\n".join(filter(None, parts))[:60000] + "\n\n" + marker
        await provider._request("POST", f"{commit}/comments", json={"note": note})
        logger.info("Published push review for %s@%s", path, head)
        _record_push_review(
            payload,
            branch=branch,
            diff=diff,
            result=result,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception:
        logger.exception("Failed push review for %s@%s", path, head)
        raise
    finally:
        _running.discard(key)


def _record_push_review(
    payload: dict, *, branch: str, diff: str, result: ReviewResult, duration_ms: int
) -> None:
    """Record a push review so the dashboard counts it (best effort).

    Dashboard stats and charts read ``review_events`` from each registered
    repo's index store. Push reviews have no merge request, so they are stored
    with ``pr_number=0`` and the head commit as the link. GitLab sends no
    install event, so the project is registered here too, as an MR would.
    """
    head = payload["after"]
    path = payload["project"]["path_with_namespace"]
    owner, _, repo = path.rpartition("/")
    if result.skipped_reason and not result.reviewed_files:
        return
    try:
        from mira.platforms.index_handlers import _get_app_db

        _get_app_db().register_repo(owner, repo, platform="gitlab")
        web_url = payload["project"].get("web_url") or ""
        store = IndexStore.open(owner, repo, platform="gitlab")
        try:
            store.record_review(
                pr_number=0,
                pr_title=f"{branch} push {head[:8]}",
                pr_url=f"{web_url}/-/commit/{head}" if web_url else "",
                comments_posted=len(result.comments),
                blockers=sum(c.severity == Severity.BLOCKER for c in result.comments),
                warnings=sum(c.severity == Severity.WARNING for c in result.comments),
                suggestions=sum(
                    c.severity in (Severity.SUGGESTION, Severity.NITPICK) for c in result.comments
                ),
                files_reviewed=result.reviewed_files,
                # Same count the PR review path uses, so charts stay comparable.
                lines_changed=sum(1 for line in diff.splitlines() if line.startswith(("+", "-"))),
                tokens_used=result.token_usage.get("total_tokens", 0),
                duration_ms=duration_ms,
                categories=",".join(sorted({c.category for c in result.comments if c.category})),
                author=payload.get("user_username") or "",
                author_avatar_url=payload.get("user_avatar") or "",
                reviewed_paths=json.dumps(result.reviewed_paths or []),
            )
        finally:
            store.close()
    except Exception:
        logger.warning("Failed to record push review for %s@%s", path, head, exc_info=True)
