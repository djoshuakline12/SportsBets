"""Automated betting scheduler.

Runs on a configurable interval to:
1. Refresh odds data
2. Update Elo ratings from recent results
3. Generate predictions
4. Place limit orders on Kalshi for positive EV opportunities
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

        # 4. Filter for bettable predictions
        min_ev = settings.min_ev_threshold
        bettable = [p for p in predictions if p["expected_value"] >= min_ev and p["stake"] > 0]
        logger.info(f"Found {len(bettable)} bettable predictions (EV >= {min_ev})")

        # 5. Place bets
        for pred in bettable:
            await _place_bet(db, pred)

        # 6. Settle completed bets
        await _settle_bets(db)

        logger.info("=== Betting cycle complete ===")

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


async def _place_bet(db: Session, prediction: dict):
    """Place a limit order on Kalshi.

    On Kalshi, each team has its own ticker. We buy "yes" on the team
    we think will win, at a price equal to our estimated probability.
    This posts a limit order — if nobody takes the other side, it sits
    on the book until the market closes or someone fills it.
    """
    event_id = prediction["event_id"]
    side = prediction["recommended_side"]  # "home" or "away"
    stake = prediction["stake"]

    # Check if we already have an active bet on this event
    existing = (
        db.query(Bet)
        .filter(Bet.event_id == event_id, Bet.status == BetStatus.PENDING)
        .first()
    )
    if existing:
        logger.info(f"Already have pending bet on {event_id}, skipping")
        return

    # Find ALL Kalshi markets for this game (one per team)
    commence = datetime.fromisoformat(prediction["commence_time"])
    markets = await kalshi_service.search_sports_markets(
        prediction["sport"],
        prediction["home_team"],
        prediction["away_team"],
    )

    if not markets:
        logger.info(f"No Kalshi market found for {event_id}")
        return

    # Find the right ticker for the team we're betting on
    # Kalshi tickers end with team abbreviation (e.g., -OKC, -UTA)
    bet_team = prediction["home_team"] if side == "home" else prediction["away_team"]
    target_market = None

    for m in markets:
        title = (m.get("title") or "").lower()
        ticker = (m.get("ticker") or "").lower()
        # Each team gets its own ticker — find the one for our team
        if kalshi_service._fuzzy_match(bet_team.lower(), f"{title} {ticker}"):
            target_market = m
            break

    if not target_market:
        # Fallback: use first market and buy "yes"
        target_market = markets[0]
        logger.info(f"Using first market as fallback: {target_market['ticker']}")

    # Set limit price based on our model's probability
    # We buy "yes" at a price that gives us +EV
    # Our probability for this team winning = prediction's win_probability
    win_prob = prediction["win_probability"]
    # Post at slightly below our estimated probability for better value
    price_cents = max(1, min(99, int(win_prob * 100) - 2))

    # Always buy "yes" on the team's specific ticker
    kalshi_side = "yes"

    # Place limit order
    result = await kalshi_service.place_bet(
        ticker=target_market["ticker"],
        side=kalshi_side,
        stake_dollars=stake,
        price_cents=price_cents,
    )

    # Build analysis string for why we placed this bet
    factors = prediction.get("factors", {})
    analysis = (
        f"Model edge: {prediction['expected_value']:.1%} EV | "
        f"Elo: {factors.get('home_elo', 1500):.0f} vs {factors.get('away_elo', 1500):.0f} | "
        f"Model prob: {win_prob:.1%} | "
        f"Market consensus: {factors.get('market_home_prob', 0.5):.1%} | "
        f"Blended: {factors.get('blended_home_prob', 0.5):.1%} | "
        f"Books: {factors.get('num_bookmakers', 0)} | "
        f"Kalshi ticker: {target_market['ticker']} | "
        f"Limit price: ${price_cents}¢"
    )

    # Record bet in database
    bet = Bet(
        prediction_id=prediction.get("prediction_id"),
        event_id=event_id,
        sport=prediction["sport"],
        home_team=prediction["home_team"],
        away_team=prediction["away_team"],
        side=side,
        stake=stake,
        odds=prediction["best_odds"],
        potential_payout=round(stake * (100 / price_cents), 2),
        status=BetStatus.PENDING,
        betfair_bet_id=result.get("order_id"),  # Kalshi order ID
        betfair_market_id=target_market["ticker"],  # Kalshi ticker
    )
    db.add(bet)

    # Update bankroll
    bankroll = get_current_bankroll(db)
    entry = BankrollEntry(
        balance=bankroll - stake,
        change_amount=-stake,
        reason=f"Bet placed: {bet_team} to win ({analysis})",
        bet_id=bet.id,
    )
    db.add(entry)
    db.commit()

    if result.get("success"):
        logger.info(
            f"BET PLACED: {bet_team} to win | "
            f"Ticker: {target_market['ticker']} | "
            f"Stake: ${stake} @ {price_cents}¢ | "
            f"EV: {prediction['expected_value']:.2%} | "
            f"Status: {result.get('status', 'unknown')}"
        )
    else:
        logger.warning(f"Bet placement failed for {event_id}: {result.get('error')}")


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
            reason=f"Bet settled: {'WON' if profit > 0 else 'LOST'} ${abs(profit):.2f}",
            bet_id=bet.id,
        )
        db.add(entry)
        logger.info(f"Settled bet {bet.id}: {'WON' if profit > 0 else 'LOST'} ${abs(profit):.2f}")

    db.commit()
