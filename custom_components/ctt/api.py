"""CTT tracker client: two keyless backends, chosen per tracking code.

All-digit codes go to the CTT Express tracker (:mod:`.express`), everything
else to ctt.pt (:mod:`.ctt`). Routing on the code's shape rather than a setting
lets one config entry hold parcels from both.
"""
from __future__ import annotations

from typing import Any

import aiohttp

from .const import CTTApiError
from .ctt import CTTClient
from .express import CTTExpressClient, is_express_code

__all__ = ["CTTApiClient", "CTTApiError"]


class CTTApiClient:
    """Dispatch each lookup to the CTT or the CTT Express backend."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise both backend clients on the shared session."""
        self._ctt = CTTClient(session)
        self._express = CTTExpressClient(session)

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel through the backend its code's shape selects."""
        if is_express_code(tracking_code):
            return await self._express.async_get_parcel(tracking_code)
        return await self._ctt.async_get_parcel(tracking_code)
