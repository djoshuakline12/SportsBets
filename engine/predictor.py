"""Main prediction engine that combines all factors to generate betting recommendations.

Pipeline:
1. Get latest odds from multiple bookmakers
2. Calculate Elo-based win probabilities
3. Adjust for weather (outdoor sports)
4. Compare model probability vs. bookmaker implied probability
5. Calculate EV for each side
6. Size bets using Kelly criterion
7. Output ranked predictions
"""

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from config import settings
from engine.elo import elo_win_probability, get_team_elos
from engine.ev_calculator import (
    calculate_ev_percentage,
    consensus_probability,
    decimal_to_implied_probability,
    find_best_odds,
)
from engine.kelly import calculate_stake
from models.schemas import BankrollEntry, OddsSnapshot, Prediction
from services.weather_service import calculate_weather_factor

logger = logging.getLogger(__name__)


def get_current_bankroll(db: Session) -> float:
    """Get the most recent bankroll balance."""
    entry = (
        db.query(BankrollEntry)
        .order_by(BankrollEntry.recorded_at.desc())
        .first()
    )
    return entry.balance if entry else 1000.0


def generate_predictions(
    db: Session,
    weather_data: dict[str, dict] | None = None,
) -> list[dict]:
    """Generate predictions for all upcoming events with odds data.

    Args:
        db: Database session
        weather_data: Optional dict of {event_id: weather_dict}

    Returns:
        List of prediction dicts sorted by EV (best first)
    """
    if weather_data is None:
        weather_data = {}

    bankroll = get_current_bankroll(db)

    # Get unique upcoming events from odds data
    events = _get_upcoming_events(db)
    predictions = []

    for event in events:
        try:
            pred = _predict_event(db, event, bankroll, weather_data)
            if pred:
                predictions.append(pred)
        except Exception as e:
            logger.error(f"Error predicting {event['event_id']}: {e}")

    # Sort by EV descending
    predictions.sort(key=lambda p: p["expected_value"], reverse=True)
    return predictions


def _get_upcoming_events(db: Session) -> list[dict]:
    """Get unique upcoming events with their odds from all bookmakers."""
    from sqlalchemy import distinct

    now = datetime.utcnow()
    event_ids = (
        db.query(distinct(OddsSnapshot.event_id))
        .filter(
            OddsSnapshot.commence_time > now,
            OddsSnapshot.market == "h2h",
        )
        .all()
    )

    events = []
    for (event_id,) in event_ids:
        odds_rows = (
            db.query(OddsSnapshot)
            .filter(
                OddsSnapshot.event_id == event_id,
                OddsSnapshot.market == "h2h",
            )
            .order_by(OddsSnapshot.captured_at.desc())
            .all()
        )
        if not odds_rows:
            continue

        first = odds_rows[0]
        home_odds = {}
        away_odds = {}
        for row in odds_rows:
            if row.bookmaker not in home_odds and row.home_price:
                home_odds[row.bookmaker] = row.home_price
            if row.bookmaker not in away_odds and row.away_price:
                away_odds[row.bookmaker] = row.away_price

        events.append(
            {
                "event_id": event_id,
                "sport": first.sport,
                "home_team": first.home_team,
                "away_team": first.away_team,
                "commence_time": first.commence_time,
                "home_odds": home_odds,
                "away_odds": away_odds,
            }
        )

    return events


def _predict_event(
    db: Session,
    event: dict,
    bankroll: float,
    weather_data: dict,
) -> dict | None:
    """Generate a prediction for a single event."""
    sport = event["sport"]
    home_team = event["home_team"]
    away_team = event["away_team"]

    # 1. Elo-based probabilities
    home_elo, away_elo = get_team_elos(db, home_team, away_team, sport)
    home_prob, away_prob = elo_win_probability(home_elo, away_elo)

    # 2. Weather adjustment
    weather = weather_data.get(event["event_id"])
    weather_factor = calculate_weather_factor(weather, sport)

    # Weather increases unpredictability -> push probabilities toward 50/50
    if weather_factor < 0:
        regression = abs(weather_factor)
        home_prob = home_prob * (1 - regression) + 0.5 * regression
        away_prob = 1 - home_prob

    # 3. Consensus market probability (wisdom of the crowd)
    market_home_prob = consensus_probability(event["home_odds"])
    market_away_prob = consensus_probability(event["away_odds"])

    # 4. Blend Elo with market (60% Elo, 40% market)
    blended_home = 0.6 * home_prob + 0.4 * market_home_prob
    blended_away = 1 - blended_home

    # 5. Find best odds and calculate EV for each side
    best_home_bk, best_home_odds = find_best_odds(event["home_odds"])
    best_away_bk, best_away_odds = find_best_odds(event["away_odds"])

    home_ev = calculate_ev_percentage(blended_home, best_home_odds) if best_home_odds > 0 else -1
    away_ev = calculate_ev_percentage(blended_away, best_away_odds) if best_away_odds > 0 else -1

    # 6. Pick the side with better EV
    if home_ev >= away_ev:
        side = "home"
        ev = home_ev
        prob = blended_home
        best_odds = best_home_odds
        best_bk = best_home_bk
    else:
        side = "away"
        ev = away_ev
        prob = blended_away
        best_odds = best_away_odds
        best_bk = best_away_bk

    # 7. Kelly sizing
    stake = calculate_stake(bankroll, prob, best_odds)

    # 8. Confidence score (0-100)
    confidence = _calculate_confidence(ev, prob, len(event["home_odds"]))

    factors = {
        "home_elo": round(home_elo, 1),
        "away_elo": round(away_elo, 1),
        "elo_home_prob": round(home_prob, 4),
        "market_home_prob": round(market_home_prob, 4),
        "blended_home_prob": round(blended_home, 4),
        "weather_factor": round(weather_factor, 4),
        "num_bookmakers": len(event["home_odds"]),
    }

    # Store prediction in DB
    pred = Prediction(
        event_id=event["event_id"],
        sport=sport,
        home_team=home_team,
        away_team=away_team,
        commence_time=event["commence_time"],
        home_win_prob=round(blended_home, 4),
        away_win_prob=round(blended_away, 4),
        recommended_side=side,
        expected_value=round(ev, 4),
        confidence=round(confidence, 1),
        best_odds=best_odds,
        best_bookmaker=best_bk,
        kelly_fraction=round(stake / bankroll if bankroll > 0 else 0, 4),
        factors=json.dumps(factors),
    )
    db.add(pred)
    db.commit()
    db.refresh(pred)

    return {
        "prediction_id": pred.id,
        "event_id": event["event_id"],
        "sport": sport,
        "home_team": home_team,
        "away_team": away_team,
        "commence_time": event["commence_time"].isoformat(),
        "recommended_side": side,
        "expected_value": round(ev, 4),
        "confidence": round(confidence, 1),
        "win_probability": round(prob, 4),
        "best_odds": best_odds,
        "best_bookmaker": best_bk,
        "stake": stake,
        "potential_payout": round(stake * best_odds, 2),
        "factors": factors,
    }


def _calculate_confidence(ev: float, prob: float, num_books: int) -> float:
    """Calculate a confidence score from 0-100.

    Based on: EV magnitude, probability strength, and data quality.
    """
    # EV component (0-40): higher EV = more confident
    ev_score = min(40, max(0, ev * 400))

    # Probability component (0-30): probability far from 50% = more confident
    prob_distance = abs(prob - 0.5) * 2  # 0 to 1 scale
    prob_score = prob_distance * 30

    # Data quality (0-30): more bookmakers = better data
    data_score = min(30, num_books * 3)

    return min(100, ev_score + prob_score + data_score)
