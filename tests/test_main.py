from fastapi.testclient import TestClient

from app.main import Runtime, app, get_runtime
from app.settings import Settings
from tests.fakes import FakeDriveStore, FakeGmailClient, FakeSheetsBackend, FakeSheetStore


def runtime(gmail=None, backend=None):
    return Runtime(
        settings=Settings(sheet_id="sheet-1", gcp_project="proj"),
        store=FakeSheetStore(backend or FakeSheetsBackend.from_fixtures()),
        drive=FakeDriveStore(),
        gmail=gmail or FakeGmailClient(),
    )


def client_with(rt):
    app.dependency_overrides[get_runtime] = lambda: rt
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_healthz_has_no_data():
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}


def test_gmail_job(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("t1", "GRC/To log", body_text=fixture_text("manpower_planning.txt"))
    response = client_with(runtime(gmail)).post("/jobs/gmail")
    assert response.status_code == 200
    assert response.json() == {
        "job": "gmail", "threads": 1, "created": 1, "duplicates": 0, "failed": 0, "gave_up": 0,
    }  # fmt: skip


def test_gmail_job_schema_error_is_500():
    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_failures"]
    response = client_with(runtime(backend=backend)).post("/jobs/gmail")
    assert response.status_code == 500
    assert response.json() == {"job": "gmail", "error": "schema_error"}


def test_gmail_job_auth_error_is_500():
    gmail = FakeGmailClient()
    gmail.add("t1", "GRC/To log", body_text="x")
    gmail.auth_broken = True
    response = client_with(runtime(gmail)).post("/jobs/gmail")
    assert response.status_code == 500
    assert response.json()["error"] == "gmail_auth_error"
