"""Review direct pushes to PSMS dev and publish on the immutable head commit."""

import logging
import re
from urllib.parse import quote

from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.dashboard.models_config import llm_config_for
from mira.llm import create_llm
from mira.platforms.auth import PlatformAuth
from mira.providers.gitlab import GitLabProvider, _build_unified_diff

PROJECT = "rhet/physical-school-management-system"
REF = "refs/heads/dev"
ZERO = "0" * 40
_running: set[tuple[str, str]] = set()
logger = logging.getLogger(__name__)


def matches(payload: dict) -> bool:
    return (
        payload.get("project", {}).get("path_with_namespace") == PROJECT
        and payload.get("ref") == REF
        and all(re.fullmatch(r"[0-9a-f]{40}", str(payload.get(k, ""))) for k in ("before", "after"))
        and payload["after"] not in (ZERO, payload["before"])
    )


async def review_push(payload: dict, auth: PlatformAuth, bot_name: str) -> None:
    before, head = payload["before"], payload["after"]
    key = before, head
    if key in _running:
        return
    _running.add(key)
    try:
        provider = GitLabProvider(await auth.get_token())
        project = f"{provider._api}/projects/{quote(PROJECT, safe='')}/repository"
        commit = f"{project}/commits/{head}"
        marker = f"<!-- mira-dev-push:{before}:{head} -->"
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
            f"### Mira dev push review · `{head[:8]}`",
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
        logger.info("Published dev push review for %s@%s", PROJECT, head)
    except Exception:
        logger.exception("Failed dev push review for %s@%s", PROJECT, head)
        raise
    finally:
        _running.discard(key)
