"""Automated betting scheduler.

Runs on a configurable interval to:
1. Refresh odds data
2. Update Elo ratings from recent results
3. Generate predictions
4. Place limit orders on Kalshi for the best +EV opportunities
5. Settle completed bets and update bankroll
"""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from config import settings
from engine.predictor import generate_predictions, get_current_bankroll
from models.database import SessionLocal
from models.schemas import BankrollEntry, Bet, BetStatus, UserSettings
from services import kalshi_service, odds_service, stats_service

logger = logging.getLogger(__name__)



async def run_betting_cycle():
    """Execute one full betting cycle."""
    db = SessionLocal()
    try:
        user_settings = db.query(UserSettings).first()
        if user_settings and not user_settings.auto_betting_enabled:
            logger.info("Auto-betting is disabled, skipping cycle")
            return

        logger.info("=== Starting betting cycle ===")

        # 1. Refresh odds
        await _refresh_odds(db)

        # 2. Update Elo from recent results
        await _update_elo_ratings(db)

        # 3. Generate predictions
        predictions = generate_predictions(db)
        logger.info(f"Generated {len(predictions)} predictions")

        # 4. Filter for bettable predictions and sort by confidence
        min_ev = settings.min_ev_threshold
        bettable = [p for p in predictions if p["expected_value"] >= min_ev and p["stake"] > 0]
        bettable.sort(key=lambda p: p["confidence"], reverse=True)
        logger.info(f"Found {len(bettable)} bettable predictions (EV >= {min_ev})")

        # 5. Place bets — all +EV bets, stop only if bankroll runs out
        bets_placed = 0
        for pred in bettable:
            # Check remaining Kalshi balance before each bet
            try:
                balance = await kalshi_service.get_account_balance()
                available = balance.get("available", 0)
                if available < 0.10:
                    logger.info(f"Kalshi balance too low (${available:.2f}), stopping")
                    break
            except Exception:
                pass

            placed = await _place_bet(db, pred)
            if placed:
                bets_placed += 1

        # 6. Settle completed bets
        await _settle_bets(db)

        logger.info(f"=== Betting cycle complete: {bets_placed} bets placed ===")

    except Exception as e:
        logger.error(f"Betting cycle error: {e}", exc_info=True)
    finally:
        db.close()


async def _refresh_odds(db: Session):
    """Fetch and store latest odds."""
    try:
        events = await odds_service.fetch_all_odds()
        count = odds_service.store_odds(db, events)
        logger.info(f"Refreshed odds: {count} snapshots stored")
    except Exception as e:
        logger.error(f"Odds refresh failed: {e}")


async def _update_elo_ratings(db: Session):
    """Update Elo ratings from recently completed games."""
    from engine.elo import process_game_result

    for sport in settings.supported_sports:
        try:
            results = await stats_service.get_recent_results(sport)
            for result in results:
                process_game_result(
                    db,
                    sport,
                    result["home_team"],
                    result["away_team"],
                    result["home_score"],
                    result["away_score"],
                )
            if results:
                logger.info(f"Updated Elo from {len(results)} {sport} results")
        except Exception as e:
            logger.error(f"Elo update failed for {sport}: {e}")


async def _place_bet(db: Session, prediction: dict) -> bool:
    """Place a limit order on Kalshi. Returns True if order was placed.

    On Kalshi, each team has its own ticker. We buy "yes" on the team
    we think will win, at a price equal to our estimated probability.
    """
    event_id = prediction["event_id"]
    side = prediction["recommended_side"]  # "home" or "away"
    stake = prediction["stake"]
    bet_team = prediction["home_team"] if side == "home" else prediction["away_team"]

    # Check if we already have an active bet on this event
    existing = (
        db.query(Bet)
        .filter(Bet.event_id == event_id, Bet.status == BetStatus.PENDING)
        .first()
    )
    if existing:
        logger.info(f"Already have pending bet on {event_id}, skipping")
        return False

    # Find ALL Kalshi markets for this game
    markets = await kalshi_service.search_sports_markets(
        prediction["sport"],
        prediction["home_team"],
        prediction["away_team"],
    )

    if not markets:
        logger.info(f"No Kalshi market found for {event_id}")
        return False

    # Find the ticker specifically for our team
    # Kalshi tickers end with team abbreviation (e.g., -SAC, -UTA, -OKC)
    target_market = None
    bet_team_lower = bet_team.lower()

    for m in markets:
        ticker = m.get("ticker", "")
        title = (m.get("title") or "").lower()

        # Check if the ticker's last segment matches our team
        # e.g., KXNBAGAME-26APR05LACSAC-SAC -> last part is SAC
        ticker_team = ticker.split("-")[-1].lower() if "-" in ticker else ""

        # Match by: ticker suffix contains team city/name abbreviation,
        # OR title contains our team name
        if bet_team_lower in title or _team_matches_ticker(bet_team, ticker_team):
            target_market = m
            break

    if not target_market:
        logger.info(f"No exact Kalshi ticker match for {bet_team}, skipping")
        return False

    # Set limit price: our model's probability minus a small edge
    win_prob = prediction["win_probability"]
    price_cents = max(1, min(99, int(win_prob * 100) - 2))

    # Ensure stake doesn't exceed what we can afford
    cost = stake
    if cost < 0.05:
        logger.info(f"Stake too small (${cost:.2f}), skipping")
        return False

    # Place limit order — buy "yes" on our team's ticker
    result = await kalshi_service.place_bet(
        ticker=target_market["ticker"],
        side="yes",
        stake_dollars=stake,
        price_cents=price_cents,
    )

    if not result.get("success"):
        logger.warning(f"Kalshi order failed for {bet_team}: {result.get('error')}")
        return False

    # Build analysis
    factors = prediction.get("factors", {})
    analysis = (
        f"Model edge: {prediction['expected_value']:.1%} EV | "
        f"Elo: {factors.get('home_elo', 1500):.0f} vs {factors.get('away_elo', 1500):.0f} | "
        f"Win prob: {win_prob:.1%} | "
        f"Market consensus: {factors.get('market_home_prob', 0.5):.1%} home | "
        f"Blended: {factors.get('blended_home_prob', 0.5):.1%} home | "
        f"Data from {factors.get('num_bookmakers', 0)} books | "
        f"Kalshi: {target_market['ticker']} @ {price_cents}¢"
    )

    # Record bet
    bet = Bet(
        prediction_id=prediction.get("prediction_id"),
        event_id=event_id,
        sport=prediction["sport"],
        home_team=prediction["home_team"],
        away_team=prediction["away_team"],
        side=side,
        stake=stake,
        odds=100 / price_cents if price_cents > 0 else 1,
        potential_payout=round(stake * (100 / price_cents), 2),
        status=BetStatus.PENDING,
        betfair_bet_id=result.get("order_id"),
        betfair_market_id=target_market["ticker"],
    )
    db.add(bet)

    bankroll = get_current_bankroll(db)
    entry = BankrollEntry(
        balance=bankroll - stake,
        change_amount=-stake,
        reason=f"Bet: {bet_team} to win | {analysis}",
        bet_id=bet.id,
    )
    db.add(entry)
    db.commit()

    logger.info(
        f"BET PLACED: {bet_team} to win | "
        f"Ticker: {target_market['ticker']} @ {price_cents}¢ | "
        f"Stake: ${stake:.2f} | EV: {prediction['expected_value']:.2%}"
    )
    return True


# Common team abbreviations used in Kalshi tickers
TEAM_ABBREVS = {
    # NBA
    "oklahoma city thunder": ["okc"], "utah jazz": ["uta"],
    "sacramento kings": ["sac"], "los angeles clippers": ["lac"],
    "golden state warriors": ["gsw", "gs"], "houston rockets": ["hou"],
    "dallas mavericks": ["dal"], "los angeles lakers": ["lal", "la"],
    "denver nuggets": ["den"], "portland trail blazers": ["por"],
    "atlanta hawks": ["atl"], "new york knicks": ["nyk", "ny"],
    "san antonio spurs": ["sas", "sa"], "philadelphia 76ers": ["phi"],
    "minnesota timberwolves": ["min"], "charlotte hornets": ["cha"],
    "new orleans pelicans": ["nop", "no"], "orlando magic": ["orl"],
    "miami heat": ["mia"], "toronto raptors": ["tor"],
    "milwaukee bucks": ["mil"], "brooklyn nets": ["bkn"],
    "chicago bulls": ["chi"], "washington wizards": ["was"],
    "boston celtics": ["bos"], "cleveland cavaliers": ["cle"],
    "indiana pacers": ["ind"], "detroit pistons": ["det"],
    "memphis grizzlies": ["mem"], "phoenix suns": ["phx"],
    # NHL
    "colorado avalanche": ["col"], "st louis blues": ["stl"],
    "nashville predators": ["nsh"], "los angeles kings": ["lak"],
    "winnipeg jets": ["wpg", "win"], "seattle kraken": ["sea"],
    "new york rangers": ["nyr"], "washington capitals": ["wsh"],
    "buffalo sabres": ["buf"], "tampa bay lightning": ["tb", "tbl"],
    "san jose sharks": ["sj", "sjs"], "chicago blackhawks": ["chi"],
    "montréal canadiens": ["mtl"], "new jersey devils": ["nj", "njd"],
    "edmonton oilers": ["edm"], "anaheim ducks": ["ana"],
    # MLB
    "detroit tigers": ["det"], "minnesota twins": ["min"],
    "miami marlins": ["mia"], "cincinnati reds": ["cin"],
    "boston red sox": ["bos"], "milwaukee brewers": ["mil"],
    "pittsburgh pirates": ["pit"], "san diego padres": ["sd"],
    "cleveland guardians": ["cle"], "kansas city royals": ["kc"],
    "texas rangers": ["tex"], "seattle mariners": ["sea"],
    "san francisco giants": ["sf"], "philadelphia phillies": ["phi"],
    "toronto blue jays": ["tor"], "los angeles dodgers": ["lad", "la"],
    "colorado rockies": ["col"], "houston astros": ["hou"],
    "los angeles angels": ["laa"], "atlanta braves": ["atl"],
    "chicago white sox": ["cws"], "baltimore orioles": ["bal"],
    "washington nationals": ["wsh", "was"],
    "st. louis cardinals": ["stl"], "new york mets": ["nym"],
    "new york yankees": ["nyy"], "arizona diamondbacks": ["ari"],
    "tampa bay rays": ["tb"], "oakland athletics": ["oak"],
}


def _team_matches_ticker(team_name: str, ticker_suffix: str) -> bool:
    """Check if a team name matches a Kalshi ticker suffix."""
    team_lower = team_name.lower()
    ticker_lower = ticker_suffix.lower()

    # Direct lookup
    abbrevs = TEAM_ABBREVS.get(team_lower, [])
    if ticker_lower in abbrevs:
        return True

    # Check if ticker suffix appears in team name
    # e.g., "sac" in "sacramento kings"
    for word in team_lower.split():
        if ticker_lower and word.startswith(ticker_lower):
            return True

    return False


async def _settle_bets(db: Session):
    """Check Kalshi for settled positions and update records."""
    try:
        settlements = await kalshi_service.get_settlements(limit=100)
    except Exception as e:
        logger.error(f"Failed to fetch settlements: {e}")
        return

    for s in settlements:
        ticker = s.get("ticker")
        bet = (
            db.query(Bet)
            .filter(
                Bet.betfair_market_id == ticker,
                Bet.status == BetStatus.PENDING,
            )
            .first()
        )
        if not bet:
            continue

        revenue = s.get("revenue", 0)
        profit = revenue - bet.stake
        bet.profit_loss = round(profit, 2)
        bet.status = BetStatus.WON if profit > 0 else BetStatus.LOST
        bet.settled_at = datetime.utcnow()

        bankroll = get_current_bankroll(db)
        entry = BankrollEntry(
            balance=bankroll + revenue,
            change_amount=revenue,
            reason=f"Settled: {'WON' if profit > 0 else 'LOST'} ${abs(profit):.2f} on {ticker}",
            bet_id=bet.id,
        )
        db.add(entry)
        logger.info(f"Settled bet {bet.id}: {'WON' if profit > 0 else 'LOST'} ${abs(profit):.2f}")

    db.commit()
