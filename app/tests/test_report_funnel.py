from __future__ import annotations

from co_scientist.domains.research_state.repository import hypotheses as store
from co_scientist.domains.research_state.repository.hypotheses import (
    HypothesisStateChanges,
    NewHypothesis,
)

from tests._client import DEFAULT_TEST_CLIENT_ID, make_client
from tests._report_helpers import render_markdown
from tests._store_helpers import seed_run

_FINALIST = {
    "id": "f1",
    "title": "Finalist idea",
    "statement": "Lactate shuttling sets the pace of ATP recovery.",
    "verification_verdict": "holds",
}
_SCREENED = {
    "id": "s1",
    "title": "Screened idea",
    "statement": "Glycogen release sets the pace of ATP recovery.",
}


def test_a_screened_idea_keeps_its_own_numbered_entry_after_the_finalists() -> None:
    markdown = render_markdown(top_hypotheses=[_FINALIST], screened_hypotheses=[_SCREENED])

    featured, screened = markdown.split("### 2. ", 1)
    assert "### 1. **Co-Scientist - Finalist idea**" in featured
    assert "screened, not deep-verified" not in featured.lower()
    assert screened.startswith("**Co-Scientist - Screened idea**")
    assert "screened, not deep-verified" in screened.lower()
    assert "Glycogen release sets the pace" in screened


def test_a_report_without_screened_ideas_has_no_screened_section() -> None:
    markdown = render_markdown(top_hypotheses=[_FINALIST], screened_hypotheses=[])

    assert "## Screened ideas" not in markdown


def test_the_ideas_api_marks_unexamined_ideas_screened_not_unverified(isolated_db: str) -> None:
    run = seed_run("Explain ATP recovery", client_id=DEFAULT_TEST_CLIENT_ID, db_path=isolated_db)
    examined = store.add_hypothesis(
        NewHypothesis(run_id=run.id, title="Examined", statement="Lactate."), db_path=isolated_db
    )
    store.update_hypothesis_state(
        examined, HypothesisStateChanges(verification_verdict="holds"), db_path=isolated_db
    )
    screened = store.add_hypothesis(
        NewHypothesis(run_id=run.id, title="Screened", statement="Glycogen."), db_path=isolated_db
    )

    hyps = make_client().get(f"/api/runs/{run.id}/hypotheses").json()["hypotheses"]

    flags = {h["id"]: (h["screened"], h["unverified"]) for h in hyps}
    assert flags == {examined: (False, False), screened: (True, False)}
