"""Authentication and trace-metadata tests for the Homework 2 endpoint."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

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


def test_message_trace_records_server_session_id(world: dict, monkeypatch) -> None:
    server_app._SESSIONS.clear()
    created = server_app.create_session(
        server_app.SessionCreate(user_id=1, role="shopper")
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(server_app, "_tracer", provider.get_tracer(__name__))
    monkeypatch.setattr(server_app, "build_agent", lambda *args, **kwargs: object())

    async def fake_run(*args, **kwargs):
        return SimpleNamespace(final_output="Done.")

    monkeypatch.setattr(server_app.Runner, "run", fake_run)
    asyncio.run(
        server_app.post_message(
            created["session_id"],
            server_app.MessageIn(message="Check my order."),
            authorization=f"Bearer {created['token']}",
        )
    )

    span = next(
        item
        for item in exporter.get_finished_spans()
        if item.name == "cartwheel.session_message"
    )
    assert span.attributes["cartwheel.session_id"] == created["session_id"]
