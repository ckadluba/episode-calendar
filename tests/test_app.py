from fastapi.testclient import TestClient

from episode_calendar.main import app


def test_application_starts_and_reports_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
