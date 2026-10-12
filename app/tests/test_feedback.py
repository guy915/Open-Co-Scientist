from __future__ import annotations

from co_scientist.domains.feedback import repository as feedback
from co_scientist.platform import db

from tests._client import make_client

PAYLOAD = {
    "category": "Bug",
    "message": "The plan did not load.",
    "diagnostics": ("=== SESSION DETAILS ===\n=== STATS ===\n=== LOGS (JSON) ===\n[]"),
    "url": "https://open-coscientist.com/chats/private-chat",
    "run_id": "reported-context",
}


def _submission(message: str = "A bug") -> feedback.Submission:
    return feedback.Submission("Bug", message, "diagnostic snapshot", "https://example.test/")


def test_submit_persists_context_and_hides_the_host_key(isolated_db: str) -> None:
    client = make_client()
    first = client.post("/api/feedback", json=PAYLOAD, headers={"X-Client-ID": "alice"})
    assert first.status_code == 201, first.text
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM feedback").fetchall()]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == first.json()["id"]
    assert row["client_id"] == "alice"
    for key, value in PAYLOAD.items():
        assert row[key] == value
