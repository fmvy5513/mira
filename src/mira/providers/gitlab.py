"""GitLab provider using the REST v4 API.

Mirrors GitHubProvider but speaks GitLab: merge requests instead of pull
requests, discussions/notes instead of review threads, and position-anchored
inline notes. Authentication is a static group/project access token sent as
``PRIVATE-TOKEN`` (the token's user is who comments are posted as).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote, unquote

import httpx

from mira.exceptions import ProviderError
from mira.models import (
    BotThreadRecord,
    FileHistoryEntry,
    HumanReviewComment,
    PRInfo,
    ReviewResult,
    UnresolvedThread,
)
from mira.platforms import profiles
from mira.providers.base import BaseProvider
from mira.providers.formatting import format_comment_body, format_key_issues
from mira.providers.gitlab_delivery import DeliveryReviewMixin

logger = logging.getLogger(__name__)

# https://gitlab.com/group/sub/project/-/merge_requests/123  or  group/project!123
_MR_URL_PATTERN = re.compile(
    r"(?:https?://[^/]+/)?(?P<owner>.+?)/(?P<repo>[^/]+?)(?:/-/merge_requests/|!)(?P<number>\d+)"
)


def parse_mr_url(mr_url: str) -> tuple[str, str, int]:
    """Parse an MR URL or shorthand into (owner, repo, iid).

    ``owner`` may contain slashes (nested groups). The project path for the API
    is ``owner/repo``.
    """
    match = _MR_URL_PATTERN.match(mr_url.strip())
    if not match:
        raise ProviderError(
            f"Cannot parse MR URL: {mr_url}. Expected "
            "https://gitlab.com/group/project/-/merge_requests/123 or group/project!123"
        )
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def _build_unified_diff(changes: list[dict[str, Any]]) -> str:
    """Assemble GitLab's per-file ``diff`` fragments into one unified diff.

    GitLab returns each file's ``@@`` hunks without the ``---``/``+++`` headers
    the diff parser needs, so we prepend them (using /dev/null for added or
    deleted files).
    """
    parts: list[str] = []
    for ch in changes:
        diff = ch.get("diff") or ""
        if not diff:
            continue
        old_path = ch.get("old_path", "")
        new_path = ch.get("new_path", "")
        old = "/dev/null" if ch.get("new_file") else f"a/{old_path}"
        new = "/dev/null" if ch.get("deleted_file") else f"b/{new_path}"
        parts.append(f"diff --git a/{old_path} b/{new_path}")
        # Without the file-mode line, a `diff --git` header paired with a
        # /dev/null side makes unidiff emit the file twice (the empty second
        # entry then clobbers the real line ranges and every finding is
        # dropped as "outside the diff").
        if ch.get("new_file"):
            parts.append("new file mode 100644")
        elif ch.get("deleted_file"):
            parts.append("deleted file mode 100644")
        parts.append(f"--- {old}")
        parts.append(f"+++ {new}")
        parts.append(diff if diff.endswith("\n") else diff + "\n")
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts)


class GitLabProvider(DeliveryReviewMixin, BaseProvider):
    """GitLab code hosting provider (REST v4)."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ProviderError("GitLab token is required")
        self._token = token
        self._api = profiles.resolve("gitlab")["api_url"] or "https://gitlab.com/api/v4"
        self._username: str | None = None

    # ── low-level HTTP ──────────────────────────────────────────────

    def _project(self, pr_info: PRInfo) -> str:
        pid = quote(f"{pr_info.owner}/{pr_info.repo}", safe="")
        return f"{self._api}/projects/{pid}"

    def _mr(self, pr_info: PRInfo) -> str:
        return f"{self._project(pr_info)}/merge_requests/{pr_info.number}"

    async def _request(
        self, method: str, url: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any
    ) -> httpx.Response:
        headers = {"PRIVATE-TOKEN": self._token, **kw.pop("headers", {})}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, **kw)
        if resp.status_code not in ok:
            raise ProviderError(f"GitLab {method} {url} → {resp.status_code}: {resp.text[:300]}")
        return resp

    async def _paginate(self, url: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        next_url: str | None = url + ("&" if "?" in url else "?") + "per_page=100"
        async with httpx.AsyncClient(timeout=30) as client:
            while next_url:
                resp = await client.get(next_url, headers={"PRIVATE-TOKEN": self._token})
                resp.raise_for_status()
                out.extend(resp.json())
                next_url = _next_link(resp.headers.get("link", ""))
        return out

    async def _self_username(self) -> str:
        """The token user's own username — who comments are actually posted as.

        Group/project access tokens post as a bot user (``project_<id>_bot_…``)
        whose name has nothing to do with MIRA_BOT_NAME, so thread dedup and
        auto-resolution must match on this, not the configured display name.
        """
        if self._username is None:
            try:
                resp = await self._request("GET", f"{self._api}/user")
                self._username = (resp.json() or {}).get("username", "") or ""
            except Exception as exc:
                logger.warning("Failed to resolve GitLab bot identity: %s", exc)
                self._username = ""
        return self._username

    # ── PR (MR) read ────────────────────────────────────────────────

    async def get_pr_info(self, pr_url: str) -> PRInfo:
        owner, repo, iid = parse_mr_url(pr_url)
        pid = quote(f"{owner}/{repo}", safe="")
        try:
            resp = await self._request("GET", f"{self._api}/projects/{pid}/merge_requests/{iid}")
            mr = resp.json()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch MR info: {e}") from e
        author = mr.get("author")
        if not isinstance(author, dict):
            author = {}
        username = author.get("username")
        avatar_url = author.get("avatar_url")
        return PRInfo(
            title=mr.get("title") or "",
            description=mr.get("description") or "",
            base_branch=mr.get("target_branch") or "",
            head_branch=mr.get("source_branch") or "",
            url=mr.get("web_url") or pr_url,
            number=iid,
            owner=owner,
            repo=repo,
            head_sha=mr.get("sha") or "",
            author=username if isinstance(username, str) else "",
            author_avatar_url=avatar_url if isinstance(avatar_url, str) else "",
            platform="gitlab",
        )

    async def get_repo_permission(
        self,
        owner: str,
        repo: str,
        username: str,
        *,
        user_id: int | str | None = None,
    ) -> str | None:
        """Fetch a user's project access level, normalized for authz."""
        pid = quote(f"{owner}/{repo}", safe="")
        try:
            if user_id is not None:
                resp = await self._request(
                    "GET",
                    f"{self._api}/projects/{pid}/members/all/{user_id}",
                    ok=(200, 404),
                )
                data = resp.json() if resp.status_code == 200 else None
                member = data if isinstance(data, dict) else None
            else:
                members = await self._paginate(
                    f"{self._api}/projects/{pid}/members/all?query={quote(username, safe='')}"
                )
                member = next(
                    (m for m in members if (m.get("username") or "").lower() == username.lower()),
                    None,
                )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Failed to fetch repo permission for {username}: {exc}") from exc

        if not member:
            return "none"
        try:
            level = int(member.get("access_level") or 0)
        except (TypeError, ValueError):
            return None
        if level >= 30:  # Developer / Maintainer / Owner
            return "write"
        if level >= 10:  # Guest / Reporter
            return "read"
        return "none"

    async def _changes(self, pr_info: PRInfo) -> dict[str, Any]:
        key = self._delivery_key(pr_info)
        pinned = getattr(self, "_delivery_inputs", {}).get(key)
        if pinned is not None:
            return pinned.review_inputs()
        if pinned is None:
            pinned = getattr(self, "_review_inputs", {}).get(key)
        if pinned is not None:
            return {"diff_refs": pinned["diff_refs"], "changes": pinned["changes"]}
        resp = await self._request("GET", f"{self._mr(pr_info)}/changes")
        return resp.json()

    async def _complete_review_inputs(self, pr_info: PRInfo) -> dict[str, Any]:
        """Read an exact collected version, never the lossy legacy /changes view."""
        if not re.fullmatch(r"[0-9a-f]{40}", pr_info.head_sha):
            raise ProviderError("MR review requires an exact source SHA")
        mr_url = self._mr(pr_info)

        async def require_current_revision() -> None:
            current = (await self._request("GET", mr_url)).json()
            if (
                current.get("sha") != pr_info.head_sha
                or current.get("target_branch") != pr_info.base_branch
            ):
                raise ProviderError("MR source or target changed during diff retrieval")

        await require_current_revision()
        version = None
        for page in range(1, 101):
            entries = (
                await self._request("GET", f"{mr_url}/versions?per_page=100&page={page}")
            ).json()
            if not isinstance(entries, list):
                raise ProviderError("The exact input diff version is unavailable")
            version = next(
                (
                    entry
                    for entry in entries
                    if isinstance(entry, dict) and entry.get("head_commit_sha") == pr_info.head_sha
                ),
                None,
            )
            if version is not None or len(entries) < 100:
                break
        if version is None or not str(version.get("id", "")).isdigit():
            raise ProviderError("The exact input diff version is unavailable")
        data = (await self._request("GET", f"{mr_url}/versions/{int(version['id'])}")).json()
        changes = data.get("diffs")
        if (
            data.get("head_commit_sha") != pr_info.head_sha
            or data.get("state") != "collected"
            or data.get("overflow")
            or not isinstance(changes, list)
            or not str(data.get("real_size", "")).isdigit()
            or int(data["real_size"]) != len(changes)
            or any(
                not isinstance(item, dict)
                or item.get("collapsed")
                or item.get("too_large")
                or not isinstance(item.get("diff"), str)
                or not isinstance(item.get("new_path"), str)
                or not item["new_path"]
                for item in changes
            )
        ):
            raise ProviderError("The exact input diff version is incomplete; review was not run")
        paths = [item["new_path"] for item in changes]
        if len(set(paths)) != len(paths):
            raise ProviderError("The exact input diff version contains duplicate paths")
        if any(
            not re.fullmatch(r"[0-9a-f]{40}", str(data.get(field, "")))
            for field in ("base_commit_sha", "start_commit_sha")
        ):
            raise ProviderError("The exact input diff version has invalid revision references")
        partition = await self._partition_review_changes(pr_info, changes, data["base_commit_sha"])
        await require_current_revision()
        return {
            "changes": changes,
            **partition,
            "diff_refs": {
                "head_sha": pr_info.head_sha,
                "base_sha": data["base_commit_sha"],
                "start_sha": data["start_commit_sha"],
            },
        }

    async def _partition_review_changes(
        self, pr_info: PRInfo, changes: list[dict], base_sha: str
    ) -> dict:
        """Apply the same bounded metadata proof to normal and delivery inputs."""
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("diff"), str)
            or item.get("collapsed")
            or item.get("too_large")
            or not isinstance(item.get("new_path"), str)
            or not item["new_path"]
            for item in changes
        ):
            raise ProviderError("The input diff is incomplete before metadata partition")
        metadata_paths = []
        empty_changes = [item for item in changes if not item["diff"].strip()]
        if len(empty_changes) > 20:
            raise ProviderError("Too many metadata-only files to verify within a bounded review")
        if not empty_changes:
            return {"source_changes": list(changes), "unreviewable_paths": []}
        try:
            async with asyncio.timeout(30):
                for item in empty_changes:
                    if not await self._verify_metadata_only(pr_info, item, base_sha):
                        raise ProviderError("Empty diff has no verified metadata-only explanation")
                    metadata_paths.append(item["new_path"])
        except TimeoutError as error:
            raise ProviderError("Metadata-only diff verification exceeded its deadline") from error
        return {
            "source_changes": [item for item in changes if item["diff"].strip()],
            "unreviewable_paths": metadata_paths,
        }

    async def _pinned_blob_metadata(self, pr_info: PRInfo, path: str, ref: str) -> tuple[str, int]:
        response = await self._request(
            "HEAD", f"{self._project(pr_info)}/repository/files/{quote(path, safe='')}?ref={ref}"
        )
        headers = {key.lower(): value for key, value in response.headers.items()}
        blob = headers.get("x-gitlab-blob-id", "")
        size = headers.get("x-gitlab-size", "")
        if (
            not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", blob)
            or not size.isdigit()
            or headers.get("x-gitlab-ref") != ref
            or headers.get("x-gitlab-commit-id") != ref
            or unquote(headers.get("x-gitlab-file-path", "")) != path
        ):
            raise ProviderError("Metadata-only file lacks immutable GitLab blob identity")
        return blob, int(size)

    async def _read_binary_prefix(self, pr_info: PRInfo, path: str, ref: str) -> bytes:
        """Bound the streamed probe even when GitLab ignores the Range header."""
        url = f"{self._project(pr_info)}/repository/files/{quote(path, safe='')}/raw?ref={ref}"
        async with (
            httpx.AsyncClient(timeout=10, follow_redirects=False) as client,
            client.stream(
                "GET", url, headers={"PRIVATE-TOKEN": self._token, "Range": "bytes=0-7999"}
            ) as response,
        ):
            if response.status_code not in (200, 206):
                raise ProviderError("Unable to verify the metadata-only binary file")
            async for chunk in response.aiter_bytes(chunk_size=8000):
                return chunk[:8000]
        return b""

    async def _verify_metadata_only(self, pr_info: PRInfo, item: dict, base_sha: str) -> bool:
        sides = []
        if item.get("new_file") is not True:
            old_path = item.get("old_path")
            if not isinstance(old_path, str) or not old_path:
                return False
            sides.append((old_path, base_sha))
        if item.get("deleted_file") is not True:
            sides.append((item["new_path"], pr_info.head_sha))
        if not sides:
            return False
        blobs = [await self._pinned_blob_metadata(pr_info, path, ref) for path, ref in sides]
        if len(blobs) == 1 and blobs[0][1] == 0:
            return True
        is_rename = item.get("renamed_file") is True and item.get("old_path") != item["new_path"]
        is_mode_change = (
            item.get("a_mode") in {"100644", "100755", "120000", "160000"}
            and item.get("b_mode") in {"100644", "100755", "120000", "160000"}
            and item["a_mode"] != item["b_mode"]
        )
        if len(blobs) == 2 and blobs[0][0] == blobs[1][0] and (is_rename or is_mode_change):
            return True
        if item.get("is_binary") is True:
            return True
        for (path, ref), (_, size) in zip(sides, blobs, strict=True):
            if size and b"\x00" in await self._read_binary_prefix(pr_info, path, ref):
                return True
        return False

    def get_unreviewable_paths(self, pr_info: PRInfo) -> list[str]:
        data = getattr(self, "_review_inputs", {}).get(self._delivery_key(pr_info), {})
        return list(data.get("unreviewable_paths", []))

    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        try:
            key = self._delivery_key(pr_info)
            data = getattr(self, "_delivery_inputs", {}).get(key)
            if data is not None:
                data = data.review_inputs()
            else:
                data = await self._complete_review_inputs(pr_info)
            source_changes = data.get("source_changes", data["changes"])
            text = _build_unified_diff(source_changes)
            from mira.core.diff_parser import parse_diff

            parsed = parse_diff(text)
            expected = {item["new_path"] for item in source_changes}
            if (
                len(parsed.files) != len(expected)
                or {file.path for file in parsed.files} != expected
            ):
                raise ProviderError("The parsed MR diff does not cover every changed file")
            data["unreviewable_paths"] = sorted(
                set(data.get("unreviewable_paths", []))
                | {file.path for file in parsed.files if file.is_binary}
            )
            if not hasattr(self, "_review_inputs"):
                self._review_inputs = {}
            self._review_inputs[key] = data
            return text
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(f"Failed to fetch complete MR diff: {error}") from error

    async def get_compare_diff(self, pr_info: PRInfo, base_sha: str, head_sha: str) -> str:
        """Diff between two commits, for round 2+ incremental reviews."""
        if base_sha == head_sha or not base_sha or not head_sha:
            return ""
        url = f"{self._project(pr_info)}/repository/compare?from={base_sha}&to={head_sha}"
        try:
            resp = await self._request("GET", url)
            return _build_unified_diff(resp.json().get("diffs", []))
        except Exception as e:
            raise ProviderError(f"Failed to fetch compare diff: {e}") from e

    async def get_file_content(self, pr_info: PRInfo, path: str, ref: str) -> str:
        """Raw file content at a ref — used to verify a thread's fix landed."""
        url = (
            f"{self._project(pr_info)}/repository/files/"
            f"{quote(path, safe='')}/raw?ref={quote(ref, safe='')}"
        )
        try:
            resp = await self._request("GET", url, ok=(200, 404))
        except ProviderError as exc:
            logger.warning("Failed to fetch %s@%s: %s", path, ref, exc)
            return ""
        return resp.text if resp.status_code == 200 else ""

    async def get_repo_tree(self, pr_info: PRInfo, ref: str) -> list[str]:
        """Every file path in the repo at a ref, for JIT cross-file context."""
        url = (
            f"{self._project(pr_info)}/repository/tree"
            f"?recursive=true&pagination=keyset&ref={quote(ref, safe='')}"
        )
        try:
            items = await self._paginate(url)
        except Exception as exc:
            logger.debug("Failed to fetch repo tree: %s", exc)
            return []
        return [it["path"] for it in items if it.get("type") == "blob"]

    async def get_file_history(
        self, pr_info: PRInfo, paths: list[str], max_per_file: int = 5
    ) -> dict[str, list[FileHistoryEntry]]:
        """Recent commits per file (most-recent first), for "why does this exist?" context."""
        if not paths:
            return {}

        sem = asyncio.Semaphore(8)
        base = f"{self._project(pr_info)}/repository/commits"
        headers = {"PRIVATE-TOKEN": self._token}

        async def _fetch_one(
            client: httpx.AsyncClient, path: str
        ) -> tuple[str, list[FileHistoryEntry]]:
            async with sem:
                try:
                    resp = await client.get(
                        base,
                        headers=headers,
                        params={
                            "path": path,
                            "ref_name": pr_info.head_branch,
                            "per_page": max_per_file,
                        },
                    )
                    if resp.status_code != 200:
                        return path, []
                    data = resp.json()
                except Exception as exc:
                    logger.debug("File history fetch failed for %s: %s", path, exc)
                    return path, []

            entries: list[FileHistoryEntry] = []
            for item in data[:max_per_file]:
                message = (item.get("message") or item.get("title") or "").strip()
                entries.append(
                    FileHistoryEntry(
                        sha=str(item.get("short_id") or item.get("id", ""))[:8],
                        message=message.split("\n\n", 1)[0][:300],
                        author=str(item.get("author_name", "")),
                        date=str(item.get("authored_date", "")),
                    )
                )
            return path, entries

        async with httpx.AsyncClient(timeout=30) as client:
            results = await asyncio.gather(*[_fetch_one(client, p) for p in paths])
        return {path: hist for path, hist in results if hist}

    # ── posting ─────────────────────────────────────────────────────

    async def post_review(
        self, pr_info: PRInfo, result: ReviewResult, bot_name: str = "miracodeai"
    ) -> None:
        if not result.comments:
            return
        try:
            data = await self._changes(pr_info)
        except Exception as e:
            raise ProviderError(f"Failed to load MR diff_refs: {e}") from e
        diff_refs = data.get("diff_refs") or {}

        posted = 0
        for comment in result.comments:
            body = format_comment_body(comment, bot_name=bot_name)
            line = (
                comment.end_line
                if comment.end_line and comment.end_line > comment.line
                else comment.line
            )
            position = {
                "position[position_type]": "text",
                "position[base_sha]": diff_refs.get("base_sha", ""),
                "position[start_sha]": diff_refs.get("start_sha", ""),
                "position[head_sha]": diff_refs.get("head_sha", ""),
                "position[old_path]": comment.path,
                "position[new_path]": comment.path,
                "position[new_line]": str(line),
            }
            try:
                await self._request(
                    "POST",
                    f"{self._mr(pr_info)}/discussions",
                    data={"body": body, **position},
                )
                posted += 1
            except ProviderError as exc:
                # GitLab 400s when a position lands on an unchanged/context line.
                # Fall back to a plain note so the finding still surfaces.
                logger.warning(
                    "Inline note failed for %s:%s (%s); posting as a note", comment.path, line, exc
                )
                note = f"**`{comment.path}:{line}`**\n\n{body}"
                try:
                    await self.post_comment(pr_info, note)
                    posted += 1
                except ProviderError:
                    logger.warning("Plain-note fallback also failed for %s:%s", comment.path, line)

        review_body = ""
        if result.summary:
            review_body = f"**Mira Review Summary**\n\n{result.summary}"
        if result.key_issues:
            review_body += format_key_issues(result.key_issues)
        if review_body:
            try:
                await self.post_comment(pr_info, review_body)
            except ProviderError as exc:
                logger.warning("Failed to post MR summary note: %s", exc)

        logger.info("GitLab: posted %d/%d review notes", posted, len(result.comments))

    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        await self._request("POST", f"{self._mr(pr_info)}/notes", data={"body": body})

    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        try:
            notes = await self._paginate(f"{self._mr(pr_info)}/notes")
        except Exception as e:
            raise ProviderError(f"Failed to list MR notes: {e}") from e
        for note in notes:
            if marker in (note.get("body") or ""):
                return int(note["id"])
        return None

    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        await self._request("PUT", f"{self._mr(pr_info)}/notes/{comment_id}", data={"body": body})

    async def get_comment_body(self, pr_info: PRInfo, comment_id: int) -> str:
        """Fetch an MR note's body by id. Best-effort."""
        try:
            resp = await self._request("GET", f"{self._mr(pr_info)}/notes/{comment_id}")
            return (resp.json().get("body") or "")[:1500]
        except Exception:
            return ""

    async def get_discussion_root_body(self, pr_info: PRInfo, discussion_id: str) -> str:
        """First note of a discussion — the bot's original comment. Best-effort.

        GitLab's note webhook carries the discussion id but not the parent note
        id, so this gives the thread-reply classifier the original suggestion as
        context (parity with GitHub's ``in_reply_to_id`` lookup).
        """
        try:
            resp = await self._request("GET", f"{self._mr(pr_info)}/discussions/{discussion_id}")
            notes = resp.json().get("notes", [])
            return (notes[0].get("body") or "")[:1500] if notes else ""
        except Exception:
            return ""

    async def reply_to_review_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        discussion_id = await self.get_thread_id_for_comment(str(comment_id), pr_info)
        if not discussion_id:
            # No discussion for this note — fall back to a plain MR note.
            await self.post_comment(pr_info, body)
            return
        await self._request(
            "POST",
            f"{self._mr(pr_info)}/discussions/{discussion_id}/notes",
            data={"body": body},
        )

    # ── discussions / threads ───────────────────────────────────────

    async def _discussions(self, pr_info: PRInfo) -> list[dict[str, Any]]:
        return await self._paginate(f"{self._mr(pr_info)}/discussions")

    @staticmethod
    def _first_inline_note(discussion: dict[str, Any]) -> dict[str, Any] | None:
        for note in discussion.get("notes", []):
            if note.get("type") == "DiffNote" or note.get("position"):
                return note
        return None

    async def get_all_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[BotThreadRecord]:
        try:
            discussions = await self._discussions(pr_info)
        except Exception as e:
            raise ProviderError(f"Failed to fetch MR discussions: {e}") from e
        bot_identities = {n for n in (await self._self_username(), bot_login) if n}
        records: list[BotThreadRecord] = []
        for disc in discussions:
            note = self._first_inline_note(disc)
            if note is None:
                continue
            author = (note.get("author") or {}).get("username", "")
            if bot_identities and author not in bot_identities:
                continue
            pos = note.get("position") or {}
            records.append(
                BotThreadRecord(
                    thread_id=str(disc["id"]),
                    path=pos.get("new_path") or pos.get("old_path") or "",
                    line=int(pos.get("new_line") or pos.get("old_line") or 0),
                    body=note.get("body") or "",
                    is_resolved=bool(note.get("resolved")),
                )
            )
        return records

    async def get_unresolved_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[UnresolvedThread]:
        threads = await self.get_all_bot_threads(pr_info, bot_login)
        return [
            UnresolvedThread(thread_id=t.thread_id, path=t.path, line=t.line, body=t.body)
            for t in threads
            if not t.is_resolved
        ]

    async def resolve_threads(self, pr_info: PRInfo, thread_ids: list[str]) -> int:
        resolved = 0
        for tid in thread_ids:
            try:
                await self._request(
                    "PUT", f"{self._mr(pr_info)}/discussions/{tid}", data={"resolved": "true"}
                )
                resolved += 1
            except ProviderError as exc:
                logger.warning("Failed to resolve discussion %s: %s", tid, exc)
        return resolved

    async def resolve_outdated_review_threads(self, pr_info: PRInfo) -> int:
        # GitLab's REST API doesn't flag a discussion as "outdated", and
        # resolving every unresolved bot thread would also clear still-valid
        # ones. We rely on the engine's already-posted dedup instead, so this
        # is intentionally a no-op for v1.
        return 0

    async def get_thread_id_for_comment(self, comment_node_id: str, pr_info: PRInfo) -> str | None:
        try:
            discussions = await self._discussions(pr_info)
        except Exception:
            return None
        for disc in discussions:
            for note in disc.get("notes", []):
                if str(note.get("id")) == str(comment_node_id):
                    return str(disc["id"])
        return None

    async def get_human_review_comments(
        self, pr_info: PRInfo, bot_login: str
    ) -> list[HumanReviewComment]:
        try:
            discussions = await self._discussions(pr_info)
        except Exception as e:
            raise ProviderError(f"Failed to fetch human review comments: {e}") from e
        bot_identities = {n for n in (await self._self_username(), bot_login) if n}
        out: list[HumanReviewComment] = []
        for disc in discussions:
            for note in disc.get("notes", []):
                pos = note.get("position")
                author = (note.get("author") or {}).get("username", "")
                if not pos or author in bot_identities or note.get("system"):
                    continue
                out.append(
                    HumanReviewComment(
                        path=pos.get("new_path") or pos.get("old_path") or "",
                        line=int(pos.get("new_line") or pos.get("old_line") or 0),
                        body=note.get("body") or "",
                        author=author,
                    )
                )
        return out

    # ── labels ──────────────────────────────────────────────────────

    async def get_pr_description(self, pr_info: PRInfo) -> str:
        resp = await self._request("GET", self._mr(pr_info))
        return (resp.json() or {}).get("description") or ""

    async def update_pr_description(self, pr_info: PRInfo, body: str) -> None:
        await self._request("PUT", self._mr(pr_info), data={"description": body})

    async def add_label(self, pr_info: PRInfo, label: str) -> None:
        await self._request("PUT", self._mr(pr_info), data={"add_labels": label})

    async def remove_label(self, pr_info: PRInfo, label: str) -> None:
        await self._request("PUT", self._mr(pr_info), data={"remove_labels": label})


def _next_link(link_header: str) -> str | None:
    """Extract the rel="next" URL from a Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None
