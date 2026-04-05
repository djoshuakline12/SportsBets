"""Automated betting scheduler.

Runs on a configurable interval to:
1. Refresh odds data
2. Update Elo ratings from recent results
3. Generate predictions
4. Place bets on positive EV opportunities via Kalshi
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
    """Attempt to place a bet via Kalshi."""
    event_id = prediction["event_id"]
    side = prediction["recommended_side"]
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

    # Find Kalshi market
    commence = datetime.fromisoformat(prediction["commence_time"])
    market = await kalshi_service.find_market(
        prediction["sport"],
        prediction["home_team"],
        prediction["away_team"],
        commence,
    )

    if not market:
        logger.info(f"No Kalshi market found for {event_id}")
        return

    # On Kalshi, "yes" = team wins, "no" = team loses
    # If we predict home wins and the market is about the home team, buy "yes"
    # We need to determine the market's subject to pick the right side
    kalshi_side = "yes" if side == "home" else "no"

    # Convert our model's probability to a price in cents
    win_prob = prediction["win_probability"]
    price_cents = max(1, min(99, int(win_prob * 100)))

    # Place bet
    result = await kalshi_service.place_bet(
        ticker=market["ticker"],
        side=kalshi_side,
        stake_dollars=stake,
        price_cents=price_cents,
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
        potential_payout=round(stake * prediction["best_odds"], 2),
        status=BetStatus.PENDING,
        betfair_bet_id=result.get("order_id"),  # reusing column for Kalshi order ID
        betfair_market_id=market["ticker"],  # reusing column for Kalshi ticker
    )
    db.add(bet)

    # Update bankroll
    bankroll = get_current_bankroll(db)
    entry = BankrollEntry(
        balance=bankroll - stake,
        change_amount=-stake,
        reason=f"Bet placed: {prediction['home_team']} vs {prediction['away_team']} ({side})",
        bet_id=bet.id,
    )
    db.add(entry)
    db.commit()

    if result.get("success"):
        logger.info(
            f"BET PLACED: {side} on {prediction['home_team']} vs {prediction['away_team']} "
            f"| Stake: ${stake} | Ticker: {market['ticker']} | EV: {prediction['expected_value']:.2%}"
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
        # Find pending bets matching this ticker
        bet = (
            db.query(Bet)
            .filter(
                Bet.betfair_market_id == ticker,  # ticker stored here
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

        # Update bankroll
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
