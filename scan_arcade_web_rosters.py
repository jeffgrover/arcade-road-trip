#!/usr/bin/env python3
"""Find arcade websites that may publish machine rosters.

This is an intentionally small first step toward website-backed game-list
validation. By default it only reports candidate arcades from DuckDB. Pass
--probe to fetch each website homepage once and score whether it appears to
link to a games, machines, collection, or lineup page.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import duckdb

from arcade_db import ACTIVE_LOCATION_STATUSES, DEFAULT_DUCKDB, connect, placeholders, rows


DEFAULT_REPORT_DIR = Path("reports")
DEFAULT_CACHE_DIR = Path("cache/web_rosters")
DEFAULT_LIMIT = 25
DEFAULT_MIN_GAME_COUNT = 50
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_BYTES = 1_500_000
ROSTER_KEYWORD_WEIGHTS = {
    "arcade games": 3,
    "cabinet": 2,
    "collection": 3,
    "current": 1,
    "game list": 4,
    "game-list": 4,
    "games": 1,
    "lineup": 3,
    "machine list": 4,
    "machines": 3,
    "pinball": 1,
    "repair": 2,
    "roster": 4,
    "view games": 4,
}
IGNORED_HINT_HOSTS = {
    "facebook.com",
    "g.page",
    "instagram.com",
    "tiktok.com",
    "twitch.tv",
    "x.com",
    "youtube.com",
}


@dataclass(frozen=True)
class Candidate:
    location_id: int
    name: str
    city: str
    state: str
    website_url: str
    game_count: int
    source_game_count: int
    status: str
    google_place_id: str


@dataclass(frozen=True)
class LinkHint:
    text: str
    url: str
    score: int


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    final_url: str
    status_code: int | None
    content_type: str
    title: str
    roster_score: int
    link_hints: list[LinkHint]
    cache_path: str
    error: str


class PageSummaryParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_title = False
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_by_name = {name.lower(): value for name, value in attrs}
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() == "a":
            href = attrs_by_name.get("href")
            self._current_href = urljoin(self.base_url, href) if href else None
            self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        if tag.lower() == "a" and self._current_href:
            text = normalize_space(" ".join(self._current_text))
            if text:
                self.links.append((text, self._current_href))
            self._current_href = None
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._current_href:
            self._current_text.append(data)

    @property
    def title(self) -> str:
        return normalize_space(" ".join(self.title_parts))


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url: str) -> str:
    value = url.strip()
    if not value:
        return value
    if not re.match(r"^https?://", value, flags=re.I):
        value = f"https://{value}"
    return value


def has_locations_column(conn: duckdb.DuckDBPyConnection, column_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'main'
          AND lower(table_name) = 'locations'
          AND lower(column_name) = lower(?)
        """,
        (column_name,),
    ).fetchone()
    return row is not None


def load_candidates(
    conn: duckdb.DuckDBPyConnection,
    *,
    limit: int,
    min_game_count: int,
    state: str | None = None,
    location_ids: list[int] | None = None,
) -> list[Candidate]:
    website_expr = "COALESCE(l.website_url, '')" if has_locations_column(conn, "website_url") else "''"
    google_place_expr = "COALESCE(l.google_place_id, '')" if has_locations_column(conn, "google_place_id") else "''"
    source_count_expr = "COALESCE(l.game_count, 0)" if has_locations_column(conn, "game_count") else "0"
    filters = [
        f"COALESCE(ls.status, 'active') IN ({placeholders(ACTIVE_LOCATION_STATUSES)})",
        f"{website_expr} <> ''",
    ]
    params: list[Any] = [*ACTIVE_LOCATION_STATUSES]
    if state:
        filters.append("upper(COALESCE(l.state, '')) = upper(?)")
        params.append(state)
    if location_ids:
        filters.append(f"l.location_id IN ({placeholders(location_ids)})")
        params.extend(location_ids)
    params.extend([min_game_count, limit])
    sql = f"""
        SELECT
            l.location_id,
            COALESCE(l.name, '') AS name,
            COALESCE(l.city, '') AS city,
            COALESCE(l.state, '') AS state,
            {website_expr} AS website_url,
            COUNT(lg.game_id) AS game_count,
            {source_count_expr} AS source_game_count,
            COALESCE(ls.status, 'active') AS status,
            {google_place_expr} AS google_place_id
        FROM locations l
        LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
        LEFT JOIN location_games lg ON lg.location_id = l.location_id
        WHERE {" AND ".join(filters)}
        GROUP BY
            l.location_id,
            l.name,
            l.city,
            l.state,
            website_url,
            source_game_count,
            status,
            google_place_id
        HAVING COUNT(lg.game_id) >= ?
        ORDER BY COUNT(lg.game_id) DESC, l.name
        LIMIT ?
    """
    return [Candidate(**row) for row in rows(conn, sql, params)]


def keyword_score(text: str) -> int:
    lower = text.lower()
    return sum(weight for keyword, weight in ROSTER_KEYWORD_WEIGHTS.items() if keyword in lower)


def ignored_hint_host(host: str) -> bool:
    normalized = host.lower()
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized in IGNORED_HINT_HOSTS


def score_links(links: list[tuple[str, str]], limit: int = 10) -> list[LinkHint]:
    hints: list[LinkHint] = []
    seen: set[str] = set()
    for text, url in links:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if ignored_hint_host(parsed.netloc):
            continue
        signature = f"{text.lower()}|{url}"
        if signature in seen:
            continue
        seen.add(signature)
        score = keyword_score(f"{text} {parsed.path}")
        if score:
            hints.append(LinkHint(text=text[:120], url=url, score=score))
    return sorted(hints, key=lambda hint: (-hint.score, hint.text.lower()))[:limit]


def cache_file_for_url(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return cache_dir / f"{digest}.html"


def probe_homepage(candidate: Candidate, cache_dir: Path, timeout_seconds: int, max_bytes: int) -> ProbeResult:
    url = normalize_url(candidate.website_url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_file_for_url(cache_dir, url)
    request = Request(
        url,
        headers={
            "User-Agent": "ArcadeRoadTripRosterReporter/0.1 (+https://github.com/jeffgrover/arcade-road-trip)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(max_bytes)
            final_url = response.geturl()
            status_code = getattr(response, "status", None)
            content_type = response.headers.get("content-type", "")
    except HTTPError as exc:
        return ProbeResult(False, url, exc.code, "", "", 0, [], "", f"HTTP {exc.code}: {exc.reason}")
    except (URLError, TimeoutError, OSError) as exc:
        return ProbeResult(False, url, None, "", "", 0, [], "", str(exc))

    cache_path.write_bytes(body)
    charset_match = re.search(r"charset=([^;]+)", content_type, flags=re.I)
    charset = charset_match.group(1).strip() if charset_match else "utf-8"
    text = body.decode(charset, errors="replace")
    parser = PageSummaryParser(final_url)
    parser.feed(text)
    link_hints = score_links(parser.links)
    roster_score = keyword_score(parser.title) + sum(hint.score for hint in link_hints)
    return ProbeResult(True, final_url, status_code, content_type, parser.title, roster_score, link_hints, str(cache_path), "")


def markdown_report(candidates: list[Candidate], probes: dict[int, ProbeResult], generated_at: str) -> str:
    lines = [
        "# Arcade Website Roster Candidates",
        "",
        f"Generated: {generated_at}",
        "",
        "These are active locations with website URLs and enough known machines to be worth checking for owner-published rosters.",
        "",
        "| Games | Arcade | Location | Website | Probe | Hints |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    for candidate in candidates:
        probe = probes.get(candidate.location_id)
        if probe is None:
            probe_text = "not probed"
            hints_text = ""
        elif not probe.ok:
            probe_text = f"failed: {probe.error}"
            hints_text = ""
        else:
            probe_text = f"score {probe.roster_score}"
            hints_text = "<br>".join(
                f"{html.escape(hint.text)} ({hint.score})" for hint in probe.link_hints[:4]
            )
        location = ", ".join(part for part in (candidate.city, candidate.state) if part)
        lines.append(
            "| "
            + " | ".join(
                [
                    str(candidate.game_count),
                    html.escape(candidate.name),
                    html.escape(location),
                    f"[{html.escape(normalize_url(candidate.website_url))}]({normalize_url(candidate.website_url)})",
                    html.escape(probe_text),
                    hints_text,
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("Probe scores are only triage hints. Treat missing or low-scoring links as unknown, not evidence that a roster does not exist.")
    return "\n".join(lines) + "\n"


def write_reports(candidates: list[Candidate], probes: dict[int, ProbeResult], report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = report_dir / f"web_roster_candidates_{stamp}.md"
    json_path = report_dir / f"web_roster_candidates_{stamp}.json"
    md_path.write_text(markdown_report(candidates, probes, generated_at), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "candidates": [asdict(candidate) for candidate in candidates],
                "probes": {str(location_id): asdict(probe) for location_id, probe in probes.items()},
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return md_path, json_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report arcade websites that may publish machine rosters.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--min-game-count", type=int, default=DEFAULT_MIN_GAME_COUNT)
    parser.add_argument("--state", help="Limit candidates to a state abbreviation.")
    parser.add_argument("--location-id", type=int, action="append", default=[])
    parser.add_argument("--probe", action="store_true", help="Fetch each candidate homepage once and score likely roster links.")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--delay-seconds", type=float, default=3.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    conn = connect(args.db, read_only=True)
    try:
        candidates = load_candidates(
            conn,
            limit=args.limit,
            min_game_count=args.min_game_count,
            state=args.state,
            location_ids=args.location_id,
        )
    finally:
        conn.close()

    probes: dict[int, ProbeResult] = {}
    if args.probe:
        for index, candidate in enumerate(candidates, start=1):
            if index > 1 and args.delay_seconds > 0:
                print(f"sleeping {args.delay_seconds:.1f}s before next website request")
                time.sleep(args.delay_seconds)
            print(f"probing {candidate.location_id}: {candidate.name} -> {candidate.website_url}")
            probes[candidate.location_id] = probe_homepage(
                candidate,
                args.cache_dir,
                args.timeout_seconds,
                args.max_bytes,
            )

    md_path, json_path = write_reports(candidates, probes, args.report_dir)
    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    print(f"candidates={len(candidates)} probed={len(probes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
