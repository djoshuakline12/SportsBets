"""Kelly Criterion for optimal bet sizing.

The Kelly Criterion calculates the optimal fraction of your bankroll
to wager on a bet with positive expected value. We use fractional Kelly
(typically 25%) to reduce variance and account for estimation errors.

Full Kelly formula: f* = (bp - q) / b
where:
    b = decimal odds - 1 (net odds)
    p = probability of winning
    q = 1 - p (probability of losing)
    f* = fraction of bankroll to wager
"""

from config import settings


def kelly_fraction(
    win_probability: float,
    decimal_odds: float,
    fraction: float | None = None,
) -> float:
    """Calculate the Kelly Criterion bet size as a fraction of bankroll.

    Args:
        win_probability: Your estimated probability of winning (0-1)
        decimal_odds: The decimal odds offered
        fraction: Kelly fraction (e.g., 0.25 for quarter Kelly).
                  Defaults to settings.kelly_fraction.

    Returns:
        Fraction of bankroll to bet (0 if negative edge).
    """
    if fraction is None:
        fraction = settings.kelly_fraction

    b = decimal_odds - 1  # net odds received on a winning bet
    if b <= 0:
        return 0.0

    p = win_probability
    q = 1 - p

    full_kelly = (b * p - q) / b

    if full_kelly <= 0:
        return 0.0

    return full_kelly * fraction


def calculate_stake(
    bankroll: float,
    win_probability: float,
    decimal_odds: float,
    max_bet: float | None = None,
    fraction: float | None = None,
) -> float:
    """Calculate the dollar amount to stake on a bet.

    Applies Kelly criterion, then caps at max_bet.

    Args:
        bankroll: Current bankroll in dollars
        win_probability: Estimated win probability (0-1)
        decimal_odds: Decimal odds offered
        max_bet: Maximum allowed bet. Defaults to settings.max_bet_amount.
        fraction: Kelly fraction override.

    Returns:
        Dollar amount to stake (can be 0 if no edge).
    """
    if max_bet is None:
        max_bet = settings.max_bet_amount

    kf = kelly_fraction(win_probability, decimal_odds, fraction)
    stake = bankroll * kf
    stake = min(stake, max_bet)
    stake = round(stake, 2)
    return max(0.0, stake)
