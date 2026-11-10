"""Free CI replay of recorded eval cassettes (Phase 6): the real orchestration, tools and
trajectory checks run against recorded model responses, and every check must come out as it
did in the recorded real run. Skipped until cassettes exist (they are recorded by the first
paid eval run)."""

from pathlib import Path

import pytest

from agentops.settings import get_settings
from evals import cassette, runner
from evals.spec import load

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not cassette.expected(), reason="no cassettes recorded yet")
async def test_cassettes_replay_with_the_recorded_outcome(test_db: str, tmp_path: Path) -> None:
    expected = cassette.expected()
    scenarios = [s for s in load() if s.id in expected]
    stale = [s.id for s in scenarios if expected[s.id]["fingerprint"] != s.fingerprint()]
    assert stale == [], f"scenario edited after its cassette was recorded: {stale}"
    records, _ = await runner.run_all(
        scenarios,
        base=get_settings(),
        mode="replay",
        k=1,
        cap_usd=0,
        concurrency=2,
        out_dir=tmp_path / "replay",
    )
    assert len(records) == len(scenarios)
    for r in records:
        assert "error" not in r, (r["scenario"], r.get("error"))
        got = {c["name"]: c["pass"] for c in r["checks"]}
        assert got == expected[r["scenario"]]["expected"]["checks"], r["scenario"]
