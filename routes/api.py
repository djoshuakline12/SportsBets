import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from engine.predictor import generate_predictions, get_current_bankroll
from models.database import get_db
from models.schemas import BankrollEntry, Bet, BetStatus, Prediction, UserSettings
from services.odds_service import get_latest_odds

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
def get_predictions(sport: str | None = None, db: Session = Depends(get_db)):
    """Get current predictions for upcoming events."""
    query = db.query(Prediction).filter(
        Prediction.commence_time > datetime.now(timezone.utc)
    )
    if sport:
        query = query.filter(Prediction.sport == sport)

    preds = query.order_by(Prediction.expected_value.desc()).limit(50).all()

    return [
        {
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
        }
        for p in preds
    ]


@router.get("/predictions/refresh")
async def refresh_predictions(db: Session = Depends(get_db)):
    """Trigger a fresh prediction run."""
    predictions = generate_predictions(db)
    return {"count": len(predictions), "predictions": predictions}


@router.get("/odds")
def get_odds(sport: str | None = None, db: Session = Depends(get_db)):
    """Get latest odds comparison across bookmakers."""
    return get_latest_odds(db, sport)


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
            "betfair_bet_id": b.betfair_bet_id,
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
