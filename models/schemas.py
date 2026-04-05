import datetime
import enum

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from models.database import Base


class BetStatus(enum.Enum):
    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    VOID = "void"


class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    sport = Column(String(100), nullable=False)
    external_id = Column(String(200), unique=True)
    elo_rating = Column(Float, default=1500.0)
    elo_updated_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"

    id = Column(Integer, primary_key=True)
    sport = Column(String(100), nullable=False)
    event_id = Column(String(200), nullable=False)
    home_team = Column(String(200), nullable=False)
    away_team = Column(String(200), nullable=False)
    commence_time = Column(DateTime, nullable=False)
    bookmaker = Column(String(100), nullable=False)
    market = Column(String(50), nullable=False)
    home_price = Column(Float)
    away_price = Column(Float)
    draw_price = Column(Float)
    home_point = Column(Float)
    away_point = Column(Float)
    over_price = Column(Float)
    under_price = Column(Float)
    over_point = Column(Float)
    captured_at = Column(DateTime, server_default=func.now())


class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True)
    event_id = Column(String(200), nullable=False)
    sport = Column(String(100), nullable=False)
    home_team = Column(String(200), nullable=False)
    away_team = Column(String(200), nullable=False)
    commence_time = Column(DateTime, nullable=False)
    home_win_prob = Column(Float, nullable=False)
    away_win_prob = Column(Float, nullable=False)
    draw_prob = Column(Float, default=0.0)
    recommended_side = Column(String(10))
    expected_value = Column(Float)
    confidence = Column(Float)
    best_odds = Column(Float)
    best_bookmaker = Column(String(100))
    kelly_fraction = Column(Float)
    factors = Column(Text)  # JSON string of contributing factors
    created_at = Column(DateTime, server_default=func.now())


class Bet(Base):
    __tablename__ = "bets"

    id = Column(Integer, primary_key=True)
    prediction_id = Column(Integer, ForeignKey("predictions.id"))
    event_id = Column(String(200), nullable=False)
    sport = Column(String(100), nullable=False)
    home_team = Column(String(200))
    away_team = Column(String(200))
    side = Column(String(10), nullable=False)  # home, away, over, under
    stake = Column(Float, nullable=False)
    odds = Column(Float, nullable=False)
    potential_payout = Column(Float, nullable=False)
    status = Column(Enum(BetStatus), default=BetStatus.PENDING)
    betfair_bet_id = Column(String(200))
    betfair_market_id = Column(String(200))
    profit_loss = Column(Float)
    settled_at = Column(DateTime)
    placed_at = Column(DateTime, server_default=func.now())

    prediction = relationship("Prediction")


class BankrollEntry(Base):
    __tablename__ = "bankroll_entries"

    id = Column(Integer, primary_key=True)
    balance = Column(Float, nullable=False)
    change_amount = Column(Float, default=0.0)
    reason = Column(String(200))
    bet_id = Column(Integer, ForeignKey("bets.id"), nullable=True)
    recorded_at = Column(DateTime, server_default=func.now())


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True)
    initial_bankroll = Column(Float, default=5.0)
    max_bet_amount = Column(Float, default=1.0)
    kelly_fraction = Column(Float, default=0.25)
    min_ev_threshold = Column(Float, default=0.02)
    active_sports = Column(Text)  # JSON list
    auto_betting_enabled = Column(Integer, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
