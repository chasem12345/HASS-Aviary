"""0.37.0: ``_fire_event`` retries only failures where Core cannot have fired the event.

A read timeout or a 504/500 can arrive after Home Assistant already fired
``aviary_detection``; retrying those sent two or three identical notifications.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import notify


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.posts = 0

    async def post(self, url, json=None, headers=None):
        self.posts += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return httpx.Response(out, text="x")


@pytest.fixture()
def client(monkeypatch):
    def install(*outcomes):
        fake = FakeClient(outcomes)
        monkeypatch.setattr(notify._http, "_client", fake)  # noqa: SLF001
        monkeypatch.setattr(notify, "_EVENT_RETRY_S", 0.0)
        return fake
    return install


def fire():
    return asyncio.run(notify._fire_event("aviary_detection", {}))  # noqa: SLF001


def test_read_timeout_is_not_retried(client):
    fake = client(httpx.ReadTimeout("slow"), 200)
    assert "not retried" in fire()
    assert fake.posts == 1


@pytest.mark.parametrize("status", [500, 504, 401])
def test_ambiguous_or_permanent_status_is_not_retried(client, status):
    fake = client(status, 200)
    assert str(status) in fire()
    assert fake.posts == 1


def test_core_restart_is_still_ridden_out(client):
    req = httpx.Request("POST", "http://supervisor")
    fake = client(httpx.ConnectError("refused", request=req), 502, 200)
    assert fire() is None
    assert fake.posts == 3
