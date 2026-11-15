from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mira.core import passes
from mira.models import HunkInfo
from tests.test_chunker import _make_file


@pytest.mark.asyncio
@pytest.mark.parametrize("later_hunk", [False, True])
async def test_security_pass_never_submits_incomplete_hunk(monkeypatch, later_hunk):
    config = SimpleNamespace(
        llm=SimpleNamespace(max_context_tokens=4000),
        review=SimpleNamespace(max_concurrent_chunks=2),
    )
    monkeypatch.setattr(passes, "load_config", lambda: config)
    scan = AsyncMock(return_value=passes._SecurityScanOutcome())
    monkeypatch.setattr(passes, "_security_scan_once", scan)
    file = _make_file("large.py", 100000)
    if later_hunk:
        file.hunks.insert(0, HunkInfo(1, 1, 1, 1, "@@ -1 +1 @@\n-old\n+new"))
    executor = Mock()
    llm = SimpleNamespace()
    result = await passes.security_review_pass(
        llm, [file], [file], security_llm=llm, agentic_executor_factory=executor
    )
    assert len(result.failures) == 1
    assert result.failures[0].paths == ["large.py"]
    assert "budget" in result.failures[0].reason.lower()
    assert scan.await_count == int(later_hunk)
    assert executor.call_count == int(later_hunk)
    if later_hunk:
        assert scan.call_args.args[2][0].hunks == [file.hunks[0]]
