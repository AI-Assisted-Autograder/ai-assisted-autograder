from unittest.mock import patch
from fastapi.testclient import TestClient
from web.app.main import app

client = TestClient(app)

# Test that the home page loads successfully and returns HTTP 200.
def test_index():
    response = client.get("/")

    assert response.status_code == 200


# Test that the health endpoint is reachable and returns the expected status.
def test_health():
    with patch("web.app.main.engine") as mock_engine:
        mock_connection = mock_engine.connect.return_value.__enter__.return_value

        response = client.get("/health")

        mock_connection.execute.assert_called_once()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

# Test that a static file can be served successfully by the application.
def test_static_css():
    response = client.get("/static/style.css")

    assert response.status_code == 200
