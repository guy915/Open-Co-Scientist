from __future__ import annotations

import pytest
from co_scientist.orchestration import notifications, task_worker
from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository.tasks import NewTask

from tests._client import create_run, make_client


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "synthetic-smtp.invalid")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "reports@example.org")


def test_retired_smtp_environment_does_not_advertise_completion_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)
    response = make_client().get("/status")
    assert response.status_code == 200
    assert response.json()["email_notifications_available"] is False


def test_anonymous_recipient_input_cannot_enqueue_mail(
    monkeypatch: pytest.MonkeyPatch,
    isolated_db: str,
) -> None:
    _configured(monkeypatch)
    response = create_run(
        make_client(),
        "synthetic research",
        notify_on_completion=True,
        completion_email="unverified-recipient@example.org",
    )
    assert response.status_code == 200
    run_id = response.json()["id"]
    notifications.enqueue_completion_notification(
        run_id,
        "synthetic research",
        "synthetic-report",
        db_path=isolated_db,
    )
    assert not any(
        task.task_type == "notification.email"
        for task in tasks.list_tasks(run_id, db_path=isolated_db)
    )


async def test_previously_queued_unverified_mail_is_finished_without_delivery(
    monkeypatch: pytest.MonkeyPatch,
    isolated_db: str,
    manual_worker: None,
) -> None:
    _configured(monkeypatch)
    response = create_run(make_client(), "synthetic research")
    run_id = response.json()["id"]
    task = tasks.enqueue_task(
        NewTask(
            run_id=run_id,
            task_type="notification.email",
            idempotency_key="legacy-email",
            inputs={"run_id": run_id, "email": "unverified-recipient@example.org"},
        ),
        db_path=isolated_db,
    )
    assert await task_worker.run_once("synthetic-worker", run_id=run_id, db_path=isolated_db)
    completed = tasks.get_task(task.id, db_path=isolated_db)
    assert completed is not None and completed.status == "completed"
    assert completed.result is not None and completed.result["status"] == "disabled"
