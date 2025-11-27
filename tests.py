import pytest
from starlette.testclient import TestClient

from main import app, settings

REAL_URL = "https://essekkat.pl/"  # przykładowa prawdziwa strona

client = TestClient(app)


@pytest.fixture
def mock_tmp_dir(monkeypatch, tmp_path):
    new_settings = settings.model_copy(deep=True)
    new_settings.data_dir = tmp_path
    monkeypatch.setattr("main.settings", new_settings)


def test_happy_path(mock_tmp_dir):
    # When submitting a valid URL
    response = client.post(
        "/submit",
        json={
            "url": REAL_URL,
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": "req-dfd275036d2869caa7072e47ab5a9fe1",
        "title": "Weaseland",
        "url": "https://essekkat.pl/",
    }
    # When requesting the RSS feed
    response = client.get("/feed/rss")
    assert response.status_code == 200
    assert response.content
    # When requesting the Atom feed
    response = client.get("/feed/atom")
    assert response.status_code == 200
    assert response.content
    # When requesting the EPUB file
    response = client.get("/feed/epub")
    assert response.status_code == 200
    assert response.content
