from unittest.mock import MagicMock, patch

import pytest

from stroummeeschter.envoy import EnvoyAuthError, EnvoyClient


def test_authenticates_once_then_fetches_production():
    client = EnvoyClient("https://envoy", "faketoken")
    with patch.object(client, "_session") as session:
        session.post.return_value = MagicMock(status_code=200)
        session.get.return_value = MagicMock(status_code=200, json=lambda: {"production": []})

        result = client.production()

        assert result == {"production": []}
        session.post.assert_called_once()
        assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer faketoken"


def test_raises_envoy_auth_error_on_bad_token():
    client = EnvoyClient("https://envoy", "badtoken")
    with patch.object(client, "_session") as session:
        session.post.return_value = MagicMock(status_code=401)
        with pytest.raises(EnvoyAuthError):
            client.production()


def test_reauthenticates_when_session_expires():
    client = EnvoyClient("https://envoy", "faketoken")
    client._authenticated = True  # simulate an already-established session
    with patch.object(client, "_session") as session:
        session.get.side_effect = [
            MagicMock(status_code=401),  # stale session
            MagicMock(status_code=200, json=lambda: {"production": ["ok"]}),
        ]
        session.post.return_value = MagicMock(status_code=200)

        result = client.production()

        assert result == {"production": ["ok"]}
        session.post.assert_called_once()  # re-authenticated exactly once
