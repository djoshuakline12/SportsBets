"""Kalshi Exchange API integration for automated bet placement.

Kalshi is a CFTC-regulated prediction market (Designated Contract Market)
that offers sports event contracts with a full trading API.

Setup:
1. Create account at kalshi.com
2. Go to Settings → API Keys → Generate API Key
3. Save the private key PEM file
4. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env
"""

import base64
import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Maps The Odds API sport keys to Kalshi series tickers for individual games
SPORT_MAP = {
    "americanfootball_nfl": "KXNFLGAME",
    "basketball_nba": "KXNBAGAME",
    "baseball_mlb": "KXMLBGAME",
    "icehockey_nhl": "KXNHLGAME",
}


def _load_private_key():
    """Load RSA private key from env var (preferred) or PEM file (fallback)."""
    # Option 1: PEM content directly in env var (for Railway/cloud deploys)
    pem_content = settings.kalshi_private_key_pem
    if pem_content:
        # Railway may escape newlines as literal \n — convert them back
        pem_content = pem_content.replace("\\n", "\n")
        # If newlines got stripped entirely, reconstruct the PEM format
        if "\n" not in pem_content.strip():
            # Extract the base64 content between header and footer
            content = pem_content
            content = content.replace("-----BEGIN RSA PRIVATE KEY-----", "")
            content = content.replace("-----END RSA PRIVATE KEY-----", "")
            content = content.replace(" ", "")
            # Re-wrap at 64 chars per line (PEM standard)
            lines = [content[i : i + 64] for i in range(0, len(content), 64)]
            pem_content = (
                "-----BEGIN RSA PRIVATE KEY-----\n"
                + "\n".join(lines)
                + "\n-----END RSA PRIVATE KEY-----\n"
            )
        pem_data = pem_content.encode()
        return serialization.load_pem_private_key(pem_data, password=None)

    # Option 2: PEM file on disk (for local dev)
    key_path = Path(settings.kalshi_private_key_path)
    if key_path.exists():
        pem_data = key_path.read_bytes()
        return serialization.load_pem_private_key(pem_data, password=None)

    raise FileNotFoundError(
        "Kalshi private key not found. Set KALSHI_PRIVATE_KEY_PEM env var "
        "with the PEM content, or place the key file at "
        f"{settings.kalshi_private_key_path}."
    )


def _sign_request(method: str, path: str, timestamp_ms: int) -> str:
    """Create RSA-PSS signature for Kalshi API authentication.

    Signature message format: timestamp + method + path
    """
    private_key = _load_private_key()
    message = f"{timestamp_ms}{method}{path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def _auth_headers(method: str, path: str) -> dict:
    """Generate authentication headers for a Kalshi API request."""
    timestamp_ms = int(time.time() * 1000)
    signature = _sign_request(method.upper(), path, timestamp_ms)
    return {
        "KALSHI-ACCESS-KEY": settings.kalshi_api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "Content-Type": "application/json",
    }


async def _request(
    method: str, path: str, body: dict | None = None, auth: bool = True
) -> dict:
    """Make a request to the Kalshi API. Public endpoints can skip auth."""
    url = f"{BASE_URL}{path}"
    if auth:
        headers = _auth_headers(method.upper(), path)
    else:
        headers = {"Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=15) as client:
        if method.upper() == "GET":
            resp = await client.get(url, headers=headers)
        elif method.upper() == "POST":
            resp = await client.post(url, headers=headers, json=body)
        elif method.upper() == "DELETE":
            resp = await client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported method: {method}")

        resp.raise_for_status()
        return resp.json() if resp.content else {}


async def get_account_balance() -> dict:
    """Get Kalshi account balance."""
    try:
        data = await _request("GET", "/portfolio/balance")
        balance = data.get("balance", {})
        return {
            "available": balance.get("available_balance_cents", 0) / 100,
            "portfolio_value": balance.get("portfolio_value_cents", 0) / 100,
            "total": balance.get("total_value_cents", 0) / 100,
        }
    except Exception as e:
        logger.error(f"Failed to get Kalshi balance: {e}")
        return {"available": 0, "error": str(e)}


async def search_sports_markets(
    sport: str,
    home_team: str,
    away_team: str,
) -> list[dict]:
    """Search Kalshi for sports markets matching a game.

    Returns list of matching market dicts with ticker, title, etc.
    """
    kalshi_sport = SPORT_MAP.get(sport, "")
    if not kalshi_sport:
        return []

    path = f"/markets?status=open&series_ticker={kalshi_sport}"

    try:
        data = await _request("GET", path, auth=False)
        markets = data.get("markets", [])

        # Filter for markets matching our teams
        matching = []
        home_lower = home_team.lower()
        away_lower = away_team.lower()

        for market in markets:
            title = (market.get("title") or "").lower()
            subtitle = (market.get("subtitle") or "").lower()
            event_title = (market.get("event_title") or "").lower()
            combined = f"{title} {subtitle} {event_title}"

            if _fuzzy_match(home_lower, combined) or _fuzzy_match(
                away_lower, combined
            ):
                matching.append(
                    {
                        "ticker": market["ticker"],
                        "title": market.get("title", ""),
                        "subtitle": market.get("subtitle", ""),
                        "event_ticker": market.get("event_ticker", ""),
                        "yes_price": market.get("yes_price_cents", 0) / 100,
                        "no_price": market.get("no_price_cents", 0) / 100,
                        "volume": market.get("volume", 0),
                        "close_time": market.get("close_time", ""),
                    }
                )

        return matching

    except Exception as e:
        logger.error(f"Kalshi market search failed: {e}")
        return []


async def find_market(
    sport: str,
    home_team: str,
    away_team: str,
    commence_time: datetime,
) -> dict | None:
    """Find a Kalshi market matching a specific game.

    Returns the best matching market dict or None.
    """
    markets = await search_sports_markets(sport, home_team, away_team)
    if not markets:
        return None

    # Prefer markets with higher volume (more liquid)
    markets.sort(key=lambda m: m.get("volume", 0), reverse=True)
    best = markets[0]

    logger.info(
        f"Found Kalshi market: {best['ticker']} - {best['title']} "
        f"(yes: ${best['yes_price']:.2f}, vol: {best['volume']})"
    )
    return best


async def place_bet(
    ticker: str,
    side: str,
    stake_dollars: float,
    price_cents: int | None = None,
) -> dict:
    """Place a bet (buy contracts) on Kalshi.

    Args:
        ticker: Market ticker (e.g., "NFL-WINNER-KC")
        side: "yes" or "no"
        stake_dollars: Amount to risk in dollars
        price_cents: Limit price in cents (1-99). If None, uses current market price.

    Returns:
        Dict with order details or error.
    """
    # Calculate number of contracts from stake
    # Each contract pays $1 if it resolves in your favor
    # Cost per contract = price_cents / 100
    if price_cents and price_cents > 0:
        cost_per_contract = price_cents / 100
        count = int(stake_dollars / cost_per_contract)
    else:
        count = int(stake_dollars)  # Market order, roughly $1 per contract

    if count < 1:
        return {"success": False, "error": "Stake too small for even 1 contract"}

    body = {
        "ticker": ticker,
        "side": side,
        "action": "buy",
        "count": count,
        "type": "limit",
    }

    if price_cents:
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents

    try:
        data = await _request("POST", "/portfolio/orders", body)
        order = data.get("order", {})

        logger.info(
            f"Kalshi order placed: {order.get('order_id')} | "
            f"Ticker: {ticker} | Side: {side} | Count: {count} | "
            f"Status: {order.get('status')}"
        )

        return {
            "success": True,
            "order_id": order.get("order_id"),
            "status": order.get("status"),
            "ticker": ticker,
            "side": side,
            "count": count,
            "price_dollars": order.get("yes_price_dollars")
            or order.get("no_price_dollars"),
            "created_time": order.get("created_time"),
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"Kalshi order failed ({e.response.status_code}): {e.response.text}")
        return {"success": False, "error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        logger.error(f"Kalshi order exception: {e}")
        return {"success": False, "error": str(e)}


async def get_positions() -> list[dict]:
    """Get current open positions."""
    try:
        data = await _request("GET", "/portfolio/positions")
        positions = data.get("market_positions", [])
        return [
            {
                "ticker": p.get("ticker"),
                "total_cost": p.get("total_cost_cents", 0) / 100,
                "position": p.get("position"),
                "market_exposure": p.get("market_exposure_cents", 0) / 100,
                "realized_pnl": p.get("realized_pnl_cents", 0) / 100,
            }
            for p in positions
        ]
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        return []


async def get_settlements(limit: int = 50) -> list[dict]:
    """Get recently settled positions for P&L tracking."""
    try:
        data = await _request("GET", f"/portfolio/settlements?limit={limit}")
        settlements = data.get("settlements", [])
        return [
            {
                "ticker": s.get("ticker"),
                "revenue": s.get("revenue_cents", 0) / 100,
                "settled_time": s.get("settled_time"),
                "yes_count": s.get("yes_count", 0),
                "no_count": s.get("no_count", 0),
            }
            for s in settlements
        ]
    except Exception as e:
        logger.error(f"Failed to get settlements: {e}")
        return []


async def get_fills(limit: int = 50) -> list[dict]:
    """Get recent order fills."""
    try:
        data = await _request("GET", f"/portfolio/fills?limit={limit}")
        fills = data.get("fills", [])
        return [
            {
                "order_id": f.get("order_id"),
                "ticker": f.get("ticker"),
                "side": f.get("side"),
                "action": f.get("action"),
                "count": f.get("count"),
                "price_cents": f.get("price_cents"),
                "created_time": f.get("created_time"),
            }
            for f in fills
        ]
    except Exception as e:
        logger.error(f"Failed to get fills: {e}")
        return []


def _fuzzy_match(needle: str, haystack: str) -> bool:
    """Check if key words from needle appear in haystack."""
    words = needle.split()
    # Match if at least half the words (min 1) appear
    matches = sum(1 for w in words if w in haystack)
    return matches >= max(1, len(words) // 2)
