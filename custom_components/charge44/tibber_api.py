from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import TIBBER_API_ENDPOINT

_LOGGER = logging.getLogger(__name__)


HOMES_QUERY = """
{
  viewer {
    homes {
      id
      appNickname
      address {
        address1
        postalCode
        city
      }
      currentSubscription {
        status
      }
    }
  }
}
"""

PRICE_QUERY = """
{{
  viewer {{
    home(id: "{home_id}") {{
      currentSubscription {{
        priceInfo(resolution: QUARTER_HOURLY) {{
          current {{ startsAt total energy tax currency level }}
          today   {{ startsAt total energy tax currency level }}
          tomorrow {{ startsAt total energy tax currency level }}
        }}
      }}
    }}
  }}
}}
"""


class TibberApiClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        home_id: str | None = None,
    ) -> None:
        self._session = session
        self._token = token
        self._home_id = home_id

    @property
    def home_id(self) -> str | None:
        return self._home_id

    @home_id.setter
    def home_id(self, value: str) -> None:
        self._home_id = value

    async def _query(self, query: str) -> dict[str, Any] | None:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            async with self._session.post(
                TIBBER_API_ENDPOINT, json={"query": query}, headers=headers
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Tibber API HTTP %s", resp.status)
                    return None
                body = await resp.json()
                if "errors" in body:
                    _LOGGER.warning("Tibber GraphQL errors: %s", body["errors"])
                    return None
                return body.get("data")
        except aiohttp.ClientError as err:
            _LOGGER.warning("Tibber connection error: %s", err)
            return None

    async def async_get_homes(self) -> list[dict[str, Any]]:
        data = await self._query(HOMES_QUERY)
        if not data:
            return []
        return data.get("viewer", {}).get("homes", []) or []

    async def async_verify(self) -> bool:
        return len(await self.async_get_homes()) > 0

    async def async_get_prices(self) -> dict[str, Any] | None:
        if not self._home_id:
            return None
        data = await self._query(PRICE_QUERY.format(home_id=self._home_id))
        if not data:
            return None
        try:
            info = data["viewer"]["home"]["currentSubscription"]["priceInfo"]
        except (KeyError, TypeError):
            return None
        return {
            "current": info.get("current"),
            "today": info.get("today", []) or [],
            "tomorrow": info.get("tomorrow", []) or [],
        }
