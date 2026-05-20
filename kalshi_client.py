import time
import base64
import uuid
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URL = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"


class KalshiClient:
    def __init__(self, api_key_id: str, private_key_path: str = "private_key.pem"):
        self.api_key_id = api_key_id
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, timestamp_ms: int, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> dict:
        timestamp_ms = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(timestamp_ms, method, path),
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        }

    def _get(self, path: str, params: dict = None) -> dict:
        full_path = API_PREFIX + path
        headers = self._auth_headers("GET", full_path)
        resp = self.session.get(BASE_URL + full_path, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict = None) -> dict:
        full_path = API_PREFIX + path
        headers = self._auth_headers("POST", full_path)
        resp = self.session.post(BASE_URL + full_path, headers=headers, json=body or {})
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> dict:
        full_path = API_PREFIX + path
        headers = self._auth_headers("DELETE", full_path)
        resp = self.session.delete(BASE_URL + full_path, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ── Market data ──────────────────────────────────────────────────────────

    def get_markets(self, limit: int = 100, cursor: str = None, **filters) -> dict:
        params = {"limit": limit, **filters}
        if cursor:
            params["cursor"] = cursor
        return self._get("/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_tennis_markets(self, status: str = "open") -> list[dict]:
        """Returns open ATP and WTA match markets across both tours."""
        markets = []
        for series in ("KXATPMATCH", "KXWTAMATCH"):
            resp = self.get_markets(limit=100, status=status, series_ticker=series)
            markets.extend(resp.get("markets", []))
        return markets

    # ── Account ───────────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        return self._get("/portfolio/balance")

    def get_positions(self) -> dict:
        return self._get("/portfolio/positions")

    def get_orders(self, **filters) -> dict:
        return self._get("/portfolio/orders", params=filters)

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        yes_price: int,
        order_type: str = "limit",
        client_order_id: str = None,
    ) -> dict:
        """
        ticker     : market ticker e.g. 'NFLSUPER-25-KC'
        side       : 'yes' or 'no'
        count      : number of contracts
        yes_price  : price in cents for the YES side (1–99)
        order_type : 'limit' or 'market'
        """
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "type": order_type,
            "action": "buy",
            "side": side,
            "count": count,
            "yes_price": yes_price,
        }
        return self._post("/portfolio/orders", body)

    def sell_order(
        self,
        ticker: str,
        side: str,
        count: int,
        yes_price: int,
        client_order_id: str = None,
    ) -> dict:
        """
        Place a limit sell order to exit an open position.
        yes_price : the YES bid price in cents (1–99) — set to current bid to get filled quickly.
        """
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "type": "limit",
            "action": "sell",
            "side": side,
            "count": count,
            "yes_price": yes_price,
        }
        return self._post("/portfolio/orders", body)

    def cancel_order(self, order_id: str) -> dict:
        return self._delete(f"/portfolio/orders/{order_id}")
