from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from app.main import app

client = TestClient(app)


def test_root_returns_info():
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["message"] == "Dev Profile Unifier API"


def test_health_returns_ok():
    with patch("app.routers.health._profile_stats", new_callable=AsyncMock,
               return_value={"total": 0, "resolved": 0, "ambiguous": 0, "pending": 0, "failed": 0}), \
         patch("app.routers.health._enrichment_stats", new_callable=AsyncMock,
               return_value={"ready": 0, "pending": 0, "failed": 0}), \
         patch("app.routers.health._api_latency_avg", new_callable=AsyncMock, return_value={}), \
         patch("app.routers.health._api_failures", new_callable=AsyncMock, return_value={}), \
         patch("app.routers.health._resolution_stats", new_callable=AsyncMock,
               return_value={"avg_time_ms": 0}), \
         patch("app.routers.health._failed_profiles", new_callable=AsyncMock, return_value=[]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_resolve_returns_202():
    with patch("app.routers.profiles.resolve_profile",
               new_callable=AsyncMock, return_value=("fake-uuid-1234", "PENDING", "PENDING")):
        resp = client.post("/profiles/resolve", json={"name": "Linus Torvalds", "github": "torvalds"})
    assert resp.status_code == 202
    assert resp.json()["profile_id"] == "fake-uuid-1234"
    assert resp.json()["resolution_status"] == "PENDING"
    assert resp.json()["enrichment_status"] == "PENDING"


def test_get_profile_404():
    with patch("app.routers.profiles.get_profile_by_id",
               new_callable=AsyncMock, return_value=None):
        resp = client.get("/profiles/nonexistent-id")
    assert resp.status_code == 404