"""Expected Value calculator for sports betting.

EV = (win_probability * profit_if_win) - (loss_probability * stake)

A positive EV bet is one where you expect to profit over the long run.
"""


def decimal_to_implied_probability(decimal_odds: float) -> float:
    """Convert decimal odds to implied probability.

    Example: 2.50 -> 0.40 (40% implied probability)
    """
    if decimal_odds <= 1.0:
        return 1.0
    return 1.0 / decimal_odds


def implied_probability_to_decimal(prob: float) -> float:
    """Convert probability to decimal odds."""
    if prob <= 0:
        return float("inf")
    return 1.0 / prob


def calculate_ev(
    win_probability: float,
    decimal_odds: float,
    stake: float = 1.0,
) -> float:
    """Calculate expected value of a bet.

    Args:
        win_probability: Your estimated probability of winning (0-1)
        decimal_odds: The decimal odds offered by the bookmaker
        stake: Amount wagered (default 1.0 for unit EV)

    Returns:
        Expected value in dollars. Positive = profitable bet.
    """
    profit_if_win = stake * (decimal_odds - 1)
    loss_if_lose = stake
    ev = (win_probability * profit_if_win) - ((1 - win_probability) * loss_if_lose)
    return ev


def calculate_ev_percentage(win_probability: float, decimal_odds: float) -> float:
    """Calculate EV as a percentage of stake.

    Returns: EV percentage (e.g., 0.05 = 5% edge)
    """
    return calculate_ev(win_probability, decimal_odds, stake=1.0)


def find_best_odds(bookmaker_odds: dict[str, float]) -> tuple[str, float]:
    """Find the bookmaker offering the best (highest) odds.

    Args:
        bookmaker_odds: dict of {bookmaker_name: decimal_odds}

    Returns:
        (best_bookmaker, best_odds)
    """
    if not bookmaker_odds:
        return ("", 0.0)
    best_bk = max(bookmaker_odds, key=bookmaker_odds.get)
    return best_bk, bookmaker_odds[best_bk]


def consensus_probability(bookmaker_odds: dict[str, float]) -> float:
    """Calculate consensus implied probability from multiple bookmakers.

    Averages the implied probabilities and removes vig by normalizing.
    """
    if not bookmaker_odds:
        return 0.5
    probs = [decimal_to_implied_probability(o) for o in bookmaker_odds.values() if o > 0]
    if not probs:
        return 0.5
    return sum(probs) / len(probs)
