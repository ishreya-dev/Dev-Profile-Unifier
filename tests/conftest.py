import pytest
from unittest.mock import AsyncMock, patch


@pytest.fixture(autouse=True)
def mock_db(monkeypatch):
    """
    Prevent any test from hitting real Supabase.
    Each test can override individual functions as needed.
    """
    with patch("app.services.database.get_db"), \
         patch("app.services.database.log_api_call", new_callable=AsyncMock), \
         patch("app.services.database.insert_raw_source", new_callable=AsyncMock, return_value="mock-raw-id"):
        yield