"""Betfair Exchange API integration for automated bet placement.

Uses betfairlightweight library for API access.
Requires: Betfair account, API app key, and SSL certificates.

Setup:
1. Register at developer.betfair.com
2. Create an Application Key
3. Generate SSL certificates (self-signed OK for non-interactive login)
4. Place certs in the path specified by BETFAIR_CERTS_PATH
"""

import logging
from datetime import datetime, timedelta

import betfairlightweight
from betfairlightweight import filters

from config import settings

logger = logging.getLogger(__name__)

_client: betfairlightweight.APIClient | None = None


def get_client() -> betfairlightweight.APIClient:
    """Get or create authenticated Betfair API client."""
    global _client
    if _client is None:
        _client = betfairlightweight.APIClient(
            username=settings.betfair_username,
            password=settings.betfair_password,
            app_key=settings.betfair_app_key,
            certs=settings.betfair_certs_path,
        )
        _client.login()
        logger.info("Betfair API authenticated successfully")
    return _client


def logout():
    """Logout from Betfair API."""
    global _client
    if _client:
        try:
            _client.logout()
        except Exception:
            pass
        _client = None


# Sport -> Betfair event type ID mapping
SPORT_TYPE_IDS = {
    "americanfootball_nfl": "6423",  # American Football
    "basketball_nba": "7522",  # Basketball
    "baseball_mlb": "7511",  # Baseball
    "icehockey_nhl": "7524",  # Ice Hockey
}


async def find_market(
    sport: str,
    home_team: str,
    away_team: str,
    commence_time: datetime,
) -> dict | None:
    """Find a Betfair market matching an event.

    Returns dict with market_id, selection_id_home, selection_id_away, etc.
    """
    client = get_client()
    event_type_id = SPORT_TYPE_IDS.get(sport)
    if not event_type_id:
        logger.warning(f"No Betfair event type for sport: {sport}")
        return None

    # Search for events around the commence time
    time_from = commence_time - timedelta(hours=1)
    time_to = commence_time + timedelta(hours=1)

    market_filter = filters.market_filter(
        event_type_ids=[event_type_id],
        market_start_time={
            "from": time_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": time_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        market_type_codes=["MATCH_ODDS", "MONEYLINE"],
    )

    try:
        market_catalogues = client.betting.list_market_catalogue(
            filter=market_filter,
            market_projection=["RUNNER_DESCRIPTION", "EVENT"],
            max_results=25,
        )
    except Exception as e:
        logger.error(f"Betfair market search failed: {e}")
        return None

    # Match by team names (fuzzy)
    home_lower = home_team.lower()
    away_lower = away_team.lower()

    for cat in market_catalogues:
        event_name = (cat.event.name or "").lower()
        if _fuzzy_match(home_lower, event_name) and _fuzzy_match(
            away_lower, event_name
        ):
            runners = {r.runner_name: r.selection_id for r in cat.runners}
            home_sel = _find_selection(runners, home_team)
            away_sel = _find_selection(runners, away_team)

            if home_sel and away_sel:
                return {
                    "market_id": cat.market_id,
                    "event_name": cat.event.name,
                    "home_selection_id": home_sel,
                    "away_selection_id": away_sel,
                    "runners": runners,
                }

    logger.info(f"No Betfair market found for {home_team} vs {away_team}")
    return None


async def place_bet(
    market_id: str,
    selection_id: int,
    stake: float,
    odds: float,
) -> dict:
    """Place a back bet on Betfair Exchange.

    Args:
        market_id: Betfair market ID
        selection_id: Runner selection ID
        stake: Amount to bet in account currency
        odds: Minimum odds to accept (decimal)

    Returns:
        Dict with bet_id, status, matched amount, etc.
    """
    client = get_client()

    limit_order = filters.limit_order(
        size=round(stake, 2),
        price=round(odds, 2),
        persistence_type="LAPSE",  # Cancel unmatched portion at in-play
    )

    instruction = filters.place_instruction(
        order_type="LIMIT",
        selection_id=selection_id,
        side="BACK",
        limit_order=limit_order,
    )

    try:
        result = client.betting.place_orders(
            market_id=market_id,
            instructions=[instruction],
        )

        if result.status == "SUCCESS":
            report = result.place_instruction_reports[0]
            logger.info(
                f"Bet placed: {report.bet_id} | "
                f"Matched: {report.size_matched} @ {report.average_price_matched}"
            )
            return {
                "success": True,
                "bet_id": report.bet_id,
                "status": report.status,
                "size_matched": report.size_matched,
                "average_price": report.average_price_matched,
                "placed_date": report.placed_date,
            }
        else:
            error_code = result.error_code
            logger.error(f"Bet placement failed: {error_code}")
            return {
                "success": False,
                "error": error_code,
            }

    except Exception as e:
        logger.error(f"Betfair place_orders exception: {e}")
        return {"success": False, "error": str(e)}


async def get_account_balance() -> dict:
    """Get Betfair account balance."""
    client = get_client()
    try:
        funds = client.account.get_account_funds()
        return {
            "available": funds.available_to_bet_balance,
            "exposure": funds.exposure,
            "retained_commission": funds.retained_commission,
        }
    except Exception as e:
        logger.error(f"Failed to get account balance: {e}")
        return {"available": 0, "exposure": 0, "error": str(e)}


async def get_settled_bets(hours: int = 24) -> list[dict]:
    """Get recently settled bets for P&L tracking."""
    client = get_client()
    try:
        settled = client.betting.list_cleared_orders(
            bet_status="SETTLED",
            settled_date_range={
                "from": (datetime.utcnow() - timedelta(hours=hours)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            },
        )
        return [
            {
                "bet_id": o.bet_id,
                "market_id": o.market_id,
                "profit": o.profit,
                "placed_date": str(o.placed_date),
                "settled_date": str(o.settled_date),
            }
            for o in settled.orders
        ]
    except Exception as e:
        logger.error(f"Failed to get settled bets: {e}")
        return []


def _fuzzy_match(needle: str, haystack: str) -> bool:
    """Check if key words from needle appear in haystack."""
    words = needle.split()
    # Match if at least half the words appear
    matches = sum(1 for w in words if w in haystack)
    return matches >= max(1, len(words) // 2)


def _find_selection(runners: dict[str, int], team_name: str) -> int | None:
    """Find a runner selection ID matching a team name."""
    team_lower = team_name.lower()
    for runner_name, sel_id in runners.items():
        if _fuzzy_match(team_lower, runner_name.lower()):
            return sel_id
    return None
