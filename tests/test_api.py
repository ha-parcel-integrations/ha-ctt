"""Tests for the dispatcher that picks a backend per tracking code."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ctt.api import CTTApiClient


@pytest.mark.parametrize(
    "code,backend,other",
    [
        ("0000000000000000000001", "_express", "_ctt"),
        ("DW000000001PT", "_ctt", "_express"),
    ],
)
async def test_dispatcher_sends_each_code_to_one_backend_only(code, backend, other):
    with (
        patch("custom_components.ctt.api.CTTClient"),
        patch("custom_components.ctt.api.CTTExpressClient"),
    ):
        client = CTTApiClient(MagicMock())
    getattr(client, backend).async_get_parcel = AsyncMock(return_value={"ok": True})
    getattr(client, other).async_get_parcel = AsyncMock()

    assert await client.async_get_parcel(code) == {"ok": True}
    getattr(client, backend).async_get_parcel.assert_awaited_once_with(code)
    getattr(client, other).async_get_parcel.assert_not_awaited()
