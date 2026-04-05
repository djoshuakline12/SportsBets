import logging

import httpx

logger = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

SPORT_MAP = {
    "americanfootball_nfl": ("football", "nfl"),
    "basketball_nba": ("basketball", "nba"),
    "baseball_mlb": ("baseball", "mlb"),
    "icehockey_nhl": ("hockey", "nhl"),
}


async def fetch_scoreboard(sport_key: str) -> dict:
    """Fetch current scoreboard from ESPN for a sport."""
    mapping = SPORT_MAP.get(sport_key)
    if not mapping:
        return {}
    sport, league = mapping
    url = f"{ESPN_BASE}/{sport}/{league}/scoreboard"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def fetch_team_stats(sport_key: str, team_id: str) -> dict:
    """Fetch team statistics from ESPN."""
    mapping = SPORT_MAP.get(sport_key)
    if not mapping:
        return {}
    sport, league = mapping
    url = f"{ESPN_BASE}/{sport}/{league}/teams/{team_id}/statistics"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def fetch_team_record(sport_key: str, team_id: str) -> dict:
    """Fetch team win/loss record from ESPN."""
    mapping = SPORT_MAP.get(sport_key)
    if not mapping:
        return {}
    sport, league = mapping
    url = f"{ESPN_BASE}/{sport}/{league}/teams/{team_id}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        team_data = data.get("team", {})
        record = team_data.get("record", {})
        return {
            "team_id": team_id,
            "name": team_data.get("displayName", ""),
            "record": record,
        }


async def fetch_all_teams(sport_key: str) -> list[dict]:
    """Fetch all teams for a sport from ESPN."""
    mapping = SPORT_MAP.get(sport_key)
    if not mapping:
        return []
    sport, league = mapping
    url = f"{ESPN_BASE}/{sport}/{league}/teams"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        teams = []
        for group in data.get("sports", [{}])[0].get("leagues", [{}])[0].get(
            "teams", []
        ):
            team = group.get("team", {})
            teams.append(
                {
                    "id": team.get("id"),
                    "name": team.get("displayName"),
                    "abbreviation": team.get("abbreviation"),
                }
            )
        return teams


async def get_recent_results(sport_key: str) -> list[dict]:
    """Fetch recent completed games for Elo updates."""
    mapping = SPORT_MAP.get(sport_key)
    if not mapping:
        return []
    sport, league = mapping
    url = f"{ESPN_BASE}/{sport}/{league}/scoreboard"
    params = {"dates": "", "limit": 50}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for event in data.get("events", []):
        competitions = event.get("competitions", [{}])
        if not competitions:
            continue
        comp = competitions[0]
        status = comp.get("status", {}).get("type", {}).get("name", "")
        if status != "STATUS_FINAL":
            continue

        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        results.append(
            {
                "event_id": event.get("id"),
                "home_team": home.get("team", {}).get("displayName", ""),
                "away_team": away.get("team", {}).get("displayName", ""),
                "home_score": int(home.get("score", 0)),
                "away_score": int(away.get("score", 0)),
                "winner": "home"
                if int(home.get("score", 0)) > int(away.get("score", 0))
                else "away",
            }
        )

    return results
