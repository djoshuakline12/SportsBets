import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from config import settings
from models.schemas import OddsSnapshot

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"


async def fetch_odds_for_sport(sport: str) -> list[dict]:
    """Fetch live odds from The Odds API for a given sport."""
    url = f"{BASE_URL}/sports/{sport}/odds/"
    params = {
        "apiKey": settings.the_odds_api_key,
        "regions": "us,eu,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining", "?")
        logger.info(f"Odds API credits remaining: {remaining}")
        return resp.json()


async def fetch_all_odds() -> list[dict]:
    """Fetch odds for all configured sports."""
    all_events = []
    for sport in settings.supported_sports:
        try:
            events = await fetch_odds_for_sport(sport)
            all_events.extend(events)
            logger.info(f"Fetched {len(events)} events for {sport}")
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to fetch odds for {sport}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching {sport}: {e}")
    return all_events


def store_odds(db: Session, events: list[dict]) -> int:
    """Parse API response and store odds snapshots in the database."""
    count = 0
    for event in events:
        event_id = event["id"]
        sport = event.get("sport_key", "")
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        commence = datetime.fromisoformat(
            event["commence_time"].replace("Z", "+00:00")
        )

        for bookmaker in event.get("bookmakers", []):
            bk_name = bookmaker["key"]
            for market in bookmaker.get("markets", []):
                market_key = market["key"]
                outcomes = {o["name"]: o for o in market.get("outcomes", [])}

                snap = OddsSnapshot(
                    sport=sport,
                    event_id=event_id,
                    home_team=home,
                    away_team=away,
                    commence_time=commence,
                    bookmaker=bk_name,
                    market=market_key,
                )

                if market_key == "h2h":
                    home_out = outcomes.get(home, {})
                    away_out = outcomes.get(away, {})
                    snap.home_price = home_out.get("price")
                    snap.away_price = away_out.get("price")
                    draw_out = outcomes.get("Draw", {})
                    snap.draw_price = draw_out.get("price")

                elif market_key == "spreads":
                    home_out = outcomes.get(home, {})
                    away_out = outcomes.get(away, {})
                    snap.home_price = home_out.get("price")
                    snap.home_point = home_out.get("point")
                    snap.away_price = away_out.get("price")
                    snap.away_point = away_out.get("point")

                elif market_key == "totals":
                    over_out = outcomes.get("Over", {})
                    under_out = outcomes.get("Under", {})
                    snap.over_price = over_out.get("price")
                    snap.over_point = over_out.get("point")
                    snap.under_price = under_out.get("price")

                db.add(snap)
                count += 1

    db.commit()
    logger.info(f"Stored {count} odds snapshots")
    return count


def get_latest_odds(db: Session, sport: str | None = None) -> list[dict]:
    """Get the most recent odds for upcoming events, grouped by event."""
    query = db.query(OddsSnapshot).filter(
        OddsSnapshot.commence_time > datetime.now(timezone.utc),
        OddsSnapshot.market == "h2h",
    )
    if sport:
        query = query.filter(OddsSnapshot.sport == sport)

    snapshots = query.order_by(OddsSnapshot.captured_at.desc()).all()

    events: dict[str, dict] = {}
    for snap in snapshots:
        if snap.event_id not in events:
            events[snap.event_id] = {
                "event_id": snap.event_id,
                "sport": snap.sport,
                "home_team": snap.home_team,
                "away_team": snap.away_team,
                "commence_time": snap.commence_time.isoformat(),
                "bookmakers": {},
            }
        ev = events[snap.event_id]
        if snap.bookmaker not in ev["bookmakers"]:
            ev["bookmakers"][snap.bookmaker] = {
                "home_price": snap.home_price,
                "away_price": snap.away_price,
                "draw_price": snap.draw_price,
            }

    return list(events.values())
