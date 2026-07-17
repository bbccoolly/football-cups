from __future__ import annotations

from dataclasses import dataclass


FOOTBALL_DATA_ROOT = "https://www.football-data.co.uk"


@dataclass(frozen=True)
class ResearchAsset:
    asset_id: str
    source_id: str
    url: str
    kind: str
    competition: str | None = None
    season: str | None = None

    @property
    def extension(self) -> str:
        return self.url.rsplit(".", 1)[-1].lower()


MAIN_LEAGUES = {
    "E0": "Premier League",
    "SP1": "La Liga",
    "D1": "Bundesliga",
    "I1": "Serie A",
    "F1": "Ligue 1",
}

ASSETS: tuple[ResearchAsset, ...] = tuple(
    ResearchAsset(
        asset_id=f"football-data-{season_code}-{league_code.lower()}",
        source_id="football-data",
        url=f"{FOOTBALL_DATA_ROOT}/mmz4281/{season_code}/{league_code}.csv",
        kind="football_data_csv",
        competition=competition,
        season=season,
    )
    for season_code, season in (("2425", "2024/25"), ("2526", "2025/26"))
    for league_code, competition in MAIN_LEAGUES.items()
) + tuple(
    ResearchAsset(
        asset_id=f"football-data-extra-{code.lower()}",
        source_id="football-data",
        url=f"{FOOTBALL_DATA_ROOT}/new/{code}.csv",
        kind="football_data_csv",
        competition=code,
    )
    for code in ("ARG", "BRA", "CHN", "JPN", "USA", "FIN", "NOR", "SWE")
) + (
    ResearchAsset(
        asset_id="football-data-world-cup-2026",
        source_id="football-data",
        url=f"{FOOTBALL_DATA_ROOT}/WorldCup2026.xlsx",
        kind="world_cup_xlsx",
        competition="World Cup",
    ),
)

ASSET_BY_ID = {asset.asset_id: asset for asset in ASSETS}
REGISTERED_HOSTS = frozenset({"www.football-data.co.uk"})
ROBOTS_URLS = {"www.football-data.co.uk": f"{FOOTBALL_DATA_ROOT}/robots.txt"}


def assets_for_source(source_id: str) -> list[ResearchAsset]:
    return [asset for asset in ASSETS if asset.source_id == source_id]
