import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from engine.predictor import generate_predictions, get_current_bankroll
from models.database import get_db
from models.schemas import BankrollEntry, Bet, BetStatus, Prediction, UserSettings
from services.odds_service import fetch_all_odds, get_latest_odds, store_odds
from services import kalshi_service

router = APIRouter(prefix="/api")


# --- Pydantic response models ---


class SettingsUpdate(BaseModel):
    initial_bankroll: float | None = None
    max_bet_amount: float | None = None
    kelly_fraction: float | None = None
    min_ev_threshold: float | None = None
    active_sports: list[str] | None = None
    auto_betting_enabled: bool | None = None


# --- Routes ---


@router.get("/predictions")
async def get_predictions(sport: str | None = None, db: Session = Depends(get_db)):
    """Get current predictions with Kalshi market availability."""
    query = db.query(Prediction).filter(
        Prediction.commence_time > datetime.now(timezone.utc)
    )
    if sport:
        query = query.filter(Prediction.sport == sport)

    preds = query.order_by(Prediction.expected_value.desc()).limit(50).all()

    results = []
    for p in preds:
        pred_data = {
            "id": p.id,
            "event_id": p.event_id,
            "sport": p.sport,
            "home_team": p.home_team,
            "away_team": p.away_team,
            "commence_time": p.commence_time.isoformat(),
            "home_win_prob": p.home_win_prob,
            "away_win_prob": p.away_win_prob,
            "recommended_side": p.recommended_side,
            "expected_value": p.expected_value,
            "confidence": p.confidence,
            "best_odds": p.best_odds,
            "best_bookmaker": p.best_bookmaker,
            "kelly_fraction": p.kelly_fraction,
            "factors": json.loads(p.factors) if p.factors else {},
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "kalshi_market": None,
        }

        # Try to find matching Kalshi market
        try:
            market = await kalshi_service.find_market(
                p.sport, p.home_team, p.away_team, p.commence_time
            )
            if market:
                pred_data["kalshi_market"] = market
        except Exception:
            pass

        results.append(pred_data)

    return results


@router.get("/predictions/refresh")
async def refresh_predictions(db: Session = Depends(get_db)):
    """Fetch fresh odds, then generate predictions."""
    # 1. Fetch odds from The Odds API
    try:
        events = await fetch_all_odds()
        odds_count = store_odds(db, events)
    except Exception as e:
        return {"error": f"Odds fetch failed: {str(e)}", "count": 0, "predictions": []}

    # 2. Generate predictions from stored odds
    predictions = generate_predictions(db)
    return {
        "odds_fetched": odds_count,
        "count": len(predictions),
        "predictions": predictions,
    }


@router.get("/odds")
async def get_odds(sport: str | None = None, db: Session = Depends(get_db)):
    """Get latest odds comparison across bookmakers, including Kalshi."""
    events = get_latest_odds(db, sport)

    # Enrich each event with Kalshi odds if available
    for event in events:
        try:
            kalshi_markets = await kalshi_service.search_sports_markets(
                event.get("sport", ""),
                event.get("home_team", ""),
                event.get("away_team", ""),
            )
            if kalshi_markets:
                best = max(kalshi_markets, key=lambda m: m.get("volume", 0))
                event["kalshi"] = {
                    "ticker": best["ticker"],
                    "title": best["title"],
                    "yes_price": best["yes_price"],
                    "no_price": best["no_price"],
                    "volume": best["volume"],
                    "close_time": best["close_time"],
                }
                # Also add Kalshi as a bookmaker in the odds comparison
                # yes_price maps to home team win probability/price
                if best["yes_price"] > 0:
                    event["bookmakers"]["kalshi"] = {
                        "home_price": round(1 / best["yes_price"], 2) if best["yes_price"] > 0 else None,
                        "away_price": round(1 / best["no_price"], 2) if best["no_price"] > 0 else None,
                        "draw_price": None,
                    }
        except Exception:
            pass

    return events


@router.get("/kalshi/markets")
async def get_kalshi_markets(sport: str | None = None):
    """Get all open Kalshi sports markets directly."""
    from config import settings

    sports = [sport] if sport else settings.supported_sports
    all_markets = []

    for s in sports:
        kalshi_sport = kalshi_service.SPORT_MAP.get(s, "")
        if not kalshi_sport:
            continue
        try:
            path = f"/markets?status=open&series_ticker={kalshi_sport}&limit=100"
            data = await kalshi_service._request("GET", path)
            markets = data.get("markets", [])
            for m in markets:
                all_markets.append({
                    "ticker": m.get("ticker"),
                    "title": m.get("title"),
                    "subtitle": m.get("subtitle"),
                    "event_ticker": m.get("event_ticker"),
                    "sport": s,
                    "yes_price": m.get("yes_price_cents", 0) / 100,
                    "no_price": m.get("no_price_cents", 0) / 100,
                    "yes_price_cents": m.get("yes_price_cents"),
                    "no_price_cents": m.get("no_price_cents"),
                    "volume": m.get("volume", 0),
                    "open_interest": m.get("open_interest", 0),
                    "close_time": m.get("close_time"),
                    "status": m.get("status"),
                })
        except Exception as e:
            all_markets.append({"sport": s, "error": str(e)})

    return {"count": len(all_markets), "markets": all_markets}


@router.get("/kalshi/debug")
async def kalshi_debug():
    """Debug Kalshi config — check if credentials are loaded."""
    from config import settings
    has_key_id = bool(settings.kalshi_api_key_id)
    has_pem = bool(settings.kalshi_private_key_pem)
    pem_len = len(settings.kalshi_private_key_pem) if has_pem else 0
    pem_starts = settings.kalshi_private_key_pem[:30] if has_pem else ""
    return {
        "kalshi_api_key_id_set": has_key_id,
        "kalshi_private_key_pem_set": has_pem,
        "kalshi_private_key_pem_length": pem_len,
        "kalshi_private_key_pem_starts_with": pem_starts,
    }


@router.get("/kalshi/balance")
async def get_kalshi_balance():
    """Get Kalshi account balance."""
    return await kalshi_service.get_account_balance()


@router.get("/kalshi/positions")
async def get_kalshi_positions():
    """Get open Kalshi positions."""
    return await kalshi_service.get_positions()


@router.get("/bets")
def get_bets(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """Get bet history."""
    query = db.query(Bet)
    if status:
        query = query.filter(Bet.status == BetStatus(status))
    bets = query.order_by(Bet.placed_at.desc()).limit(limit).all()

    return [
        {
            "id": b.id,
            "event_id": b.event_id,
            "sport": b.sport,
            "home_team": b.home_team,
            "away_team": b.away_team,
            "side": b.side,
            "stake": b.stake,
            "odds": b.odds,
            "potential_payout": b.potential_payout,
            "status": b.status.value if b.status else None,
            "profit_loss": b.profit_loss,
            "kalshi_order_id": b.betfair_bet_id,
            "kalshi_ticker": b.betfair_market_id,
            "placed_at": b.placed_at.isoformat() if b.placed_at else None,
            "settled_at": b.settled_at.isoformat() if b.settled_at else None,
        }
        for b in bets
    ]


@router.get("/bankroll")
def get_bankroll(db: Session = Depends(get_db)):
    """Get current bankroll and history."""
    current = get_current_bankroll(db)
    history = (
        db.query(BankrollEntry)
        .order_by(BankrollEntry.recorded_at.desc())
        .limit(100)
        .all()
    )

    return {
        "current_balance": current,
        "history": [
            {
                "balance": e.balance,
                "change": e.change_amount,
                "reason": e.reason,
                "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
            }
            for e in history
        ],
    }


@router.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    """Aggregated dashboard stats."""
    bankroll = get_current_bankroll(db)

    # Bet stats
    total_bets = db.query(Bet).count()
    won = db.query(Bet).filter(Bet.status == BetStatus.WON).count()
    lost = db.query(Bet).filter(Bet.status == BetStatus.LOST).count()
    pending = db.query(Bet).filter(Bet.status == BetStatus.PENDING).count()

    # P&L
    settled_bets = db.query(Bet).filter(Bet.profit_loss.isnot(None)).all()
    total_pl = sum(b.profit_loss for b in settled_bets)
    total_staked = sum(b.stake for b in settled_bets)
    roi = (total_pl / total_staked * 100) if total_staked > 0 else 0

    # Upcoming predictions
    upcoming = (
        db.query(Prediction)
        .filter(Prediction.commence_time > datetime.now(timezone.utc))
        .order_by(Prediction.expected_value.desc())
        .limit(5)
        .all()
    )

    return {
        "bankroll": bankroll,
        "total_bets": total_bets,
        "won": won,
        "lost": lost,
        "pending": pending,
        "win_rate": round(won / (won + lost) * 100, 1) if (won + lost) > 0 else 0,
        "total_profit_loss": round(total_pl, 2),
        "roi_percent": round(roi, 2),
        "total_staked": round(total_staked, 2),
        "upcoming_predictions": [
            {
                "home_team": p.home_team,
                "away_team": p.away_team,
                "sport": p.sport,
                "recommended_side": p.recommended_side,
                "expected_value": p.expected_value,
                "confidence": p.confidence,
                "commence_time": p.commence_time.isoformat(),
            }
            for p in upcoming
        ],
    }


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    """Get user settings."""
    s = db.query(UserSettings).first()
    if not s:
        s = UserSettings()
        db.add(s)
        db.commit()
        db.refresh(s)

    return {
        "initial_bankroll": s.initial_bankroll,
        "max_bet_amount": s.max_bet_amount,
        "kelly_fraction": s.kelly_fraction,
        "min_ev_threshold": s.min_ev_threshold,
        "active_sports": json.loads(s.active_sports) if s.active_sports else [],
        "auto_betting_enabled": bool(s.auto_betting_enabled),
    }


@router.post("/settings")
def update_settings(update: SettingsUpdate, db: Session = Depends(get_db)):
    """Update user settings."""
    s = db.query(UserSettings).first()
    if not s:
        s = UserSettings()
        db.add(s)

    if update.initial_bankroll is not None:
        s.initial_bankroll = update.initial_bankroll
    if update.max_bet_amount is not None:
        s.max_bet_amount = update.max_bet_amount
    if update.kelly_fraction is not None:
        s.kelly_fraction = update.kelly_fraction
    if update.min_ev_threshold is not None:
        s.min_ev_threshold = update.min_ev_threshold
    if update.active_sports is not None:
        s.active_sports = json.dumps(update.active_sports)
    if update.auto_betting_enabled is not None:
        s.auto_betting_enabled = 1 if update.auto_betting_enabled else 0

    db.commit()
    return {"status": "updated"}
