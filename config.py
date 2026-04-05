from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://user:password@localhost:5432/sportsbets"
    the_odds_api_key: str = ""
    weather_api_key: str = ""
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = "./kalshi_private_key.pem"
    max_bet_amount: float = 1.00
    kelly_fraction: float = 0.25
    min_ev_threshold: float = 0.02
    odds_refresh_minutes: int = 15
    supported_sports: list[str] = [
        "americanfootball_nfl",
        "basketball_nba",
        "baseball_mlb",
        "icehockey_nhl",
    ]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
