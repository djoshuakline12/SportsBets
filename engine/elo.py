"""Elo rating system for team strength estimation.

Each team starts at 1500. After each game, ratings adjust based on
outcome vs expectation. The difference in Elo ratings maps directly
to a win probability via the logistic function.
"""

import math
from datetime import datetime

from sqlalchemy.orm import Session

from models.schemas import Team

# K-factor: how much ratings change per game. Higher = more reactive.
K_FACTOR = 20

# Home advantage in Elo points (roughly 3% win probability)
HOME_ADVANTAGE = 65


def expected_score(rating_a: float, rating_b: float) -> float:
    """Calculate expected score (win probability) for player A."""
    return 1.0 / (1.0 + math.pow(10, (rating_b - rating_a) / 400))


def elo_win_probability(
    home_elo: float, away_elo: float, home_advantage: float = HOME_ADVANTAGE
) -> tuple[float, float]:
    """Calculate win probabilities for home and away teams.

    Returns (home_win_prob, away_win_prob).
    """
    home_prob = expected_score(home_elo + home_advantage, away_elo)
    away_prob = 1.0 - home_prob
    return home_prob, away_prob


def update_elo(
    winner_rating: float,
    loser_rating: float,
    margin: float = 1.0,
    k: float = K_FACTOR,
) -> tuple[float, float]:
    """Update Elo ratings after a game.

    Args:
        winner_rating: Current Elo of the winning team
        loser_rating: Current Elo of the losing team
        margin: Score margin multiplier (>1 for blowouts)
        k: K-factor for adjustment speed

    Returns:
        (new_winner_rating, new_loser_rating)
    """
    expected_win = expected_score(winner_rating, loser_rating)
    # Margin of victory multiplier: dampened log scale
    mov_mult = math.log(max(margin, 1) + 1) * (2.2 / ((winner_rating - loser_rating) * 0.001 + 2.2))
    adjustment = k * mov_mult * (1 - expected_win)

    new_winner = winner_rating + adjustment
    new_loser = loser_rating - adjustment
    return new_winner, new_loser


def get_or_create_team(db: Session, name: str, sport: str) -> Team:
    """Get a team from DB or create with default 1500 Elo."""
    team = db.query(Team).filter(Team.name == name, Team.sport == sport).first()
    if not team:
        team = Team(name=name, sport=sport, elo_rating=1500.0)
        db.add(team)
        db.commit()
        db.refresh(team)
    return team


def process_game_result(
    db: Session,
    sport: str,
    home_team_name: str,
    away_team_name: str,
    home_score: int,
    away_score: int,
) -> tuple[float, float]:
    """Process a completed game and update Elo ratings.

    Returns updated (home_elo, away_elo).
    """
    home_team = get_or_create_team(db, home_team_name, sport)
    away_team = get_or_create_team(db, away_team_name, sport)

    margin = abs(home_score - away_score)

    if home_score > away_score:
        new_home, new_away = update_elo(
            home_team.elo_rating, away_team.elo_rating, margin
        )
    else:
        new_away, new_home = update_elo(
            away_team.elo_rating, home_team.elo_rating, margin
        )

    home_team.elo_rating = new_home
    home_team.elo_updated_at = datetime.utcnow()
    away_team.elo_rating = new_away
    away_team.elo_updated_at = datetime.utcnow()

    db.commit()
    return new_home, new_away


def get_team_elos(
    db: Session, home_name: str, away_name: str, sport: str
) -> tuple[float, float]:
    """Get current Elo ratings for two teams."""
    home = get_or_create_team(db, home_name, sport)
    away = get_or_create_team(db, away_name, sport)
    return home.elo_rating, away.elo_rating
