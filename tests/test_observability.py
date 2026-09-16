"""Authentication tests for the Homework 2 endpoint."""

import pytest
from fastapi import HTTPException

from server import app as server_app


def test_create_session_rejects_role_mismatch(world: dict) -> None:
    server_app._SESSIONS.clear()

    # The database—not the caller's claim—determines the user's role.
    with pytest.raises(HTTPException) as error:
        server_app.create_session(
            server_app.SessionCreate(user_id=1, role="merchant")
        )

    assert error.value.status_code == 403


def test_token_cannot_authorize_another_session(world: dict) -> None:
    server_app._SESSIONS.clear()

    # Separate sessions must remain isolated, even when owned by the same user.
    first = server_app.create_session(
        server_app.SessionCreate(user_id=1, role="shopper")
    )
    second = server_app.create_session(
        server_app.SessionCreate(user_id=1, role="shopper")
    )

    with pytest.raises(HTTPException) as error:
        server_app._authorize(
            second["session_id"],
            f"Bearer {first['token']}",
        )

    assert error.value.status_code == 403
