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
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import duckdb

from arcade_db import (
    DEFAULT_DUCKDB,
    active_atlas_location_clause,
    active_atlas_location_params,
    connect,
    placeholders,
    rows,
)


DEFAULT_REPORT_DIR = Path("reports")
DEFAULT_CACHE_DIR = Path("cache/web_rosters")
DEFAULT_LIMIT = 25
DEFAULT_MIN_GAME_COUNT = 50
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_BYTES = 1_500_000
DEFAULT_MAX_ROSTER_PAGES = 3
MAX_EXTRACTED_NAMES_PER_PAGE = 1_500
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
TRUSTED_EXTERNAL_ROSTER_HOSTS = {
    "pinside.com",
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
class PageFetchResult:
    ok: bool
    final_url: str
    status_code: int | None
    content_type: str
    title: str
    links: list[tuple[str, str]]
    text: str
    cache_path: str
    error: str


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


@dataclass(frozen=True)
class RosterPageResult:
    source_text: str
    source_url: str
    source_score: int
    ok: bool
    final_url: str
    status_code: int | None
    content_type: str
    title: str
    roster_score: int
    extracted_names: list[str]
    cache_path: str
    error: str


@dataclass(frozen=True)
class RosterComparison:
    db_game_count: int
    roster_page_count: int
    matched_db_games: list[str]
    missing_db_games: list[str]
    website_only_names: list[str]


class PageSummaryParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        attrs_by_name = {name.lower(): value for name, value in attrs}
        if normalized_tag == "title":
            self._in_title = True
        if normalized_tag == "a":
            href = attrs_by_name.get("href")
            self._current_href = urljoin(self.base_url, href) if href else None
            self._current_text = []
        if normalized_tag in {"br", "div", "li", "p", "td", "th", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if normalized_tag == "title":
            self._in_title = False
        if normalized_tag == "a" and self._current_href:
            text = normalize_space(" ".join(self._current_text))
            if text:
                self.links.append((text, self._current_href))
            self._current_href = None
            self._current_text = []
        if normalized_tag in {"div", "li", "p", "td", "th", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._current_href:
            self._current_text.append(data)
        text = normalize_space(data)
        if text:
            self.text_parts.append(text + " ")

    @property
    def title(self) -> str:
        return normalize_space(" ".join(self.title_parts))

    @property
    def text(self) -> str:
        lines = [normalize_space(line) for line in "".join(self.text_parts).splitlines()]
        return "\n".join(line for line in lines if line)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url: str) -> str:
    value = url.strip()
    if not value:
        return value
    if not re.match(r"^https?://", value, flags=re.I):
        value = f"https://{value}"
    return value


def normalized_host(url: str) -> str:
    host = urlparse(normalize_url(url)).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_internal_url(base_url: str, target_url: str) -> bool:
    return normalized_host(base_url) == normalized_host(target_url)


def is_trusted_external_roster_url(url: str) -> bool:
    return normalized_host(url) in TRUSTED_EXTERNAL_ROSTER_HOSTS


def should_follow_roster_url(base_url: str, target_url: str, allow_trusted_external: bool) -> bool:
    return is_internal_url(base_url, target_url) or (
        allow_trusted_external and is_trusted_external_roster_url(target_url)
    )


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
        active_atlas_location_clause(),
        f"{website_expr} <> ''",
    ]
    params: list[Any] = [*active_atlas_location_params()]
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


def normalize_game_name(value: str) -> str:
    normalized = html.unescape(value).lower()
    normalized = re.sub(r"\([^)]*\)", " ", normalized)
    normalized = re.sub(r"^(.+),\s*(the|a|an)$", r"\2 \1", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return normalize_space(normalized)


def title_in_text(title: str, normalized_text: str) -> bool:
    normalized_title = normalize_game_name(title)
    if len(normalized_title) < 4:
        return False
    return f" {normalized_title} " in f" {normalized_text} "


def likely_same_game(left: str, right: str) -> bool:
    left_norm = normalize_game_name(left)
    right_norm = normalize_game_name(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    if len(left_norm) >= 5 and f" {left_norm} " in f" {right_norm} ":
        return True
    if len(right_norm) >= 5 and f" {right_norm} " in f" {left_norm} ":
        return True
    return SequenceMatcher(None, left_norm, right_norm).ratio() >= 0.88


def extract_machine_name_candidates(text: str, limit: int = 200) -> list[str]:
    reject_patterns = re.compile(
        r"(admission|birthday|calendar|contact|directions|email|facebook|food|footer|"
        r"hours?|instagram|login|membership|menu|newsletter|party|phone|privacy|restaurant|"
        r"subscribe|ticket|tiktok|toggle navigation|twitter|video walk.?through|youtube|www\.|https?://)",
        flags=re.I,
    )
    candidates: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = normalize_space(raw_line)
        line = re.sub(r"^\s*[-*\u2022]\s+", "", line)
        line = re.sub(r"^\s*\d+[\.)]\s+", "", line)
        line = re.sub(r"\s+(?:\(?(?:working|down|needs repair|out of order|coming soon|sold)\)?)$", "", line, flags=re.I)
        line = line.strip(" :-\u2013\u2014|\"")
        if not line or len(line) < 3 or len(line) > 80:
            continue
        if re.search(r"\b\d{1,2}\s*(?:am|pm)\b", line, flags=re.I):
            continue
        if re.search(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", line):
            continue
        if re.search(r"\d", line) and re.search(r"\b(ave|avenue|blvd|boulevard|dr|drive|rd|road|st|street)\.?\b", line, flags=re.I):
            continue
        if re.search(r"\b\d+\s+days\s+a\s+year\b", line, flags=re.I):
            continue
        if line.lower() in {"games", "game list", "games list", "home"}:
            continue
        if line.lower().startswith(("about ", "current games", "games list")):
            continue
        if reject_patterns.search(line):
            continue
        if not re.search(r"[A-Za-z]", line) and not re.fullmatch(r"\d{3,4}", line):
            continue
        if len(line.split()) > 9:
            continue
        key = normalize_game_name(line)
        if len(key) < 3 or key in seen:
            continue
        seen.add(key)
        candidates.append(line)
        if len(candidates) >= limit:
            break
    return candidates


def extract_pinside_machine_names(text: str, limit: int = MAX_EXTRACTED_NAMES_PER_PAGE) -> list[str]:
    lines = [normalize_space(line) for line in text.splitlines()]
    start = None
    for index, line in enumerate(lines):
        if re.search(r"\b\d+\s+games\s+listed\s+for\s+this\s+location\b", line, flags=re.I):
            start = index + 1
            break
    if start is None:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for line in lines[start:]:
        if not line:
            continue
        if line.lower() in {"comments", "pictures", "photos", "location comments"}:
            break
        if line.lower().startswith("visit this location"):
            break
        if re.search(r"\badded\s+on\s+\d{4}-\d{2}-\d{2}\b", line, flags=re.I):
            continue
        if re.match(r"^(?:em|ss|solid state|electro-mechanical)\b", line, flags=re.I):
            continue
        line = re.sub(r"^Machine:\s*", "", line, flags=re.I).strip()
        candidates = extract_machine_name_candidates(line, limit=1)
        if not candidates:
            continue
        candidate = candidates[0]
        key = normalize_game_name(candidate)
        if key in seen:
            continue
        seen.add(key)
        names.append(candidate)
        if len(names) >= limit:
            break
    return names


def extract_machine_names_from_page(url: str, text: str, limit: int = MAX_EXTRACTED_NAMES_PER_PAGE) -> list[str]:
    if normalized_host(url) == "pinside.com":
        names = extract_pinside_machine_names(text, limit=limit)
        if names:
            return names
    return extract_machine_name_candidates(text, limit=limit)


def cache_file_for_url(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return cache_dir / f"{digest}.html"


def fetch_page(url: str, cache_dir: Path, timeout_seconds: int, max_bytes: int) -> PageFetchResult:
    normalized_url = normalize_url(url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_file_for_url(cache_dir, normalized_url)
    request = Request(
        normalized_url,
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
        return PageFetchResult(False, normalized_url, exc.code, "", "", [], "", "", f"HTTP {exc.code}: {exc.reason}")
    except (URLError, TimeoutError, OSError) as exc:
        return PageFetchResult(False, normalized_url, None, "", "", [], "", "", str(exc))

    cache_path.write_bytes(body)
    charset_match = re.search(r"charset=([^;]+)", content_type, flags=re.I)
    charset = charset_match.group(1).strip() if charset_match else "utf-8"
    text = body.decode(charset, errors="replace")
    parser = PageSummaryParser(final_url)
    parser.feed(text)
    return PageFetchResult(True, final_url, status_code, content_type, parser.title, parser.links, parser.text, str(cache_path), "")


def probe_homepage(candidate: Candidate, cache_dir: Path, timeout_seconds: int, max_bytes: int) -> ProbeResult:
    fetched = fetch_page(candidate.website_url, cache_dir, timeout_seconds, max_bytes)
    if not fetched.ok:
        return ProbeResult(
            fetched.ok,
            fetched.final_url,
            fetched.status_code,
            fetched.content_type,
            fetched.title,
            0,
            [],
            fetched.cache_path,
            fetched.error,
        )
    link_hints = score_links(fetched.links)
    roster_score = keyword_score(fetched.title) + sum(hint.score for hint in link_hints)
    return ProbeResult(
        fetched.ok,
        fetched.final_url,
        fetched.status_code,
        fetched.content_type,
        fetched.title,
        roster_score,
        link_hints,
        fetched.cache_path,
        fetched.error,
    )


def discover_roster_pages(
    probe: ProbeResult,
    cache_dir: Path,
    timeout_seconds: int,
    max_bytes: int,
    max_pages: int,
    delay_seconds: float,
    allow_trusted_external: bool,
) -> list[RosterPageResult]:
    if not probe.ok:
        return []
    pages: list[RosterPageResult] = []
    seen_urls: set[str] = {probe.final_url.rstrip("/")}
    for hint in probe.link_hints:
        if len(pages) >= max_pages:
            break
        if not should_follow_roster_url(probe.final_url, hint.url, allow_trusted_external):
            continue
        signature = hint.url.rstrip("/")
        if signature in seen_urls:
            continue
        seen_urls.add(signature)
        if delay_seconds > 0:
            print(f"sleeping {delay_seconds:.1f}s before roster-page request")
            time.sleep(delay_seconds)
        print(f"probing roster page hint: {hint.text} -> {hint.url}")
        fetched = fetch_page(hint.url, cache_dir, timeout_seconds, max_bytes)
        if fetched.ok:
            extracted = extract_machine_names_from_page(fetched.final_url, fetched.text)
            roster_score = keyword_score(fetched.title) + keyword_score(fetched.text[:5000])
        else:
            extracted = []
            roster_score = 0
        pages.append(
            RosterPageResult(
                source_text=hint.text,
                source_url=hint.url,
                source_score=hint.score,
                ok=fetched.ok,
                final_url=fetched.final_url,
                status_code=fetched.status_code,
                content_type=fetched.content_type,
                title=fetched.title,
                roster_score=roster_score,
                extracted_names=extracted[:MAX_EXTRACTED_NAMES_PER_PAGE],
                cache_path=fetched.cache_path,
                error=fetched.error,
            )
        )
    return pages


def load_location_game_names(conn: duckdb.DuckDBPyConnection, location_ids: list[int]) -> dict[int, list[str]]:
    if not location_ids:
        return {}
    sql = f"""
        SELECT DISTINCT lg.location_id, g.name
        FROM location_games lg
        JOIN games g ON g.game_id = lg.game_id
        WHERE lg.location_id IN ({placeholders(location_ids)})
        ORDER BY lg.location_id, g.name
    """
    result: dict[int, list[str]] = {location_id: [] for location_id in location_ids}
    for row in rows(conn, sql, location_ids):
        result.setdefault(row["location_id"], []).append(row["name"])
    return result


def compare_roster_to_database(db_game_names: list[str], pages: list[RosterPageResult]) -> RosterComparison:
    if not any(page.ok for page in pages):
        return RosterComparison(
            db_game_count=len(db_game_names),
            roster_page_count=0,
            matched_db_games=[],
            missing_db_games=[],
            website_only_names=[],
        )
    page_text = "\n".join(page.title + "\n" + "\n".join(page.extracted_names) for page in pages if page.ok)
    normalized_text = normalize_game_name(page_text)
    extracted_names: list[str] = []
    seen: set[str] = set()
    for page in pages:
        for name in page.extracted_names:
            key = normalize_game_name(name)
            if key and key not in seen:
                seen.add(key)
                extracted_names.append(name)
    matched = [
        name
        for name in db_game_names
        if title_in_text(name, normalized_text) or any(likely_same_game(name, roster_name) for roster_name in extracted_names)
    ]
    missing = [name for name in db_game_names if name not in set(matched)]
    website_only = [
        name
        for name in extracted_names
        if not any(likely_same_game(name, db_name) for db_name in db_game_names)
    ]
    return RosterComparison(
        db_game_count=len(db_game_names),
        roster_page_count=sum(1 for page in pages if page.ok),
        matched_db_games=matched,
        missing_db_games=missing,
        website_only_names=website_only,
    )


def markdown_report(
    candidates: list[Candidate],
    probes: dict[int, ProbeResult],
    roster_pages: dict[int, list[RosterPageResult]],
    comparisons: dict[int, RosterComparison],
    generated_at: str,
) -> str:
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
            hints = [f"{html.escape(hint.text)} ({hint.score})" for hint in probe.link_hints[:4]]
            pages = roster_pages.get(candidate.location_id, [])
            if pages:
                hints.extend(
                    f"page: {html.escape(page.source_text)} ({len(page.extracted_names)} names)"
                    for page in pages[:3]
                )
            hints_text = "<br>".join(hints)
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
        comparison = comparisons.get(candidate.location_id)
        if comparison:
            validation_text = (
                "no roster pages read"
                if comparison.roster_page_count == 0
                else f"{len(comparison.matched_db_games)}/{comparison.db_game_count} DB names seen; "
                f"{len(comparison.website_only_names)} website-only candidates"
            )
            lines.append(
                f"|  |  |  |  | validation | "
                f"{validation_text} |"
            )
    lines.append("")
    lines.append("Probe scores are only triage hints. Treat missing or low-scoring links as unknown, not evidence that a roster does not exist.")
    if roster_pages:
        lines.extend(["", "## Discovered Roster Pages", ""])
        for candidate in candidates:
            pages = roster_pages.get(candidate.location_id, [])
            if not pages:
                continue
            lines.append(f"### {candidate.name}")
            for page in pages:
                if page.ok:
                    lines.append(
                        f"- [{html.escape(page.source_text)}]({page.final_url}) "
                        f"score={page.roster_score} extracted_names={len(page.extracted_names)}"
                    )
                    if page.extracted_names:
                        preview = ", ".join(html.escape(name) for name in page.extracted_names[:10])
                        lines.append(f"  - sample: {preview}")
                else:
                    lines.append(f"- {html.escape(page.source_text)} failed: {html.escape(page.error)}")
            lines.append("")
    if comparisons:
        lines.extend(["", "## Validation Preview", ""])
        for candidate in candidates:
            comparison = comparisons.get(candidate.location_id)
            if not comparison:
                continue
            lines.append(f"### {candidate.name}")
            if comparison.roster_page_count == 0:
                lines.append(
                    f"- DB games: {comparison.db_game_count}; roster pages read: 0; "
                    "no DB mismatch conclusions."
                )
                lines.append("")
                continue
            lines.append(
                f"- DB games: {comparison.db_game_count}; roster pages read: {comparison.roster_page_count}; "
                f"DB games seen on website: {len(comparison.matched_db_games)}"
            )
            if comparison.missing_db_games:
                lines.append("- DB games not seen on website sample: " + ", ".join(html.escape(name) for name in comparison.missing_db_games[:20]))
            if comparison.website_only_names:
                lines.append("- Website-only candidate sample: " + ", ".join(html.escape(name) for name in comparison.website_only_names[:20]))
            lines.append("")
    return "\n".join(lines) + "\n"


def write_reports(
    candidates: list[Candidate],
    probes: dict[int, ProbeResult],
    roster_pages: dict[int, list[RosterPageResult]],
    comparisons: dict[int, RosterComparison],
    report_dir: Path,
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = report_dir / f"web_roster_candidates_{stamp}.md"
    json_path = report_dir / f"web_roster_candidates_{stamp}.json"
    md_path.write_text(markdown_report(candidates, probes, roster_pages, comparisons, generated_at), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "candidates": [asdict(candidate) for candidate in candidates],
                "probes": {str(location_id): asdict(probe) for location_id, probe in probes.items()},
                "roster_pages": {
                    str(location_id): [asdict(page) for page in pages]
                    for location_id, pages in roster_pages.items()
                },
                "comparisons": {
                    str(location_id): asdict(comparison)
                    for location_id, comparison in comparisons.items()
                },
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
    parser.add_argument("--discover-pages", action="store_true", help="Fetch high-scoring internal roster-page links from probed homepages.")
    parser.add_argument("--no-external-roster-hosts", action="store_true", help="Do not follow trusted external roster hosts such as Pinside.")
    parser.add_argument("--max-roster-pages", type=int, default=DEFAULT_MAX_ROSTER_PAGES)
    parser.add_argument("--compare", action="store_true", help="Compare discovered roster-page text against current DB machine names.")
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
        game_names_by_location = (
            load_location_game_names(conn, [candidate.location_id for candidate in candidates])
            if args.compare
            else {}
        )
    finally:
        conn.close()

    probes: dict[int, ProbeResult] = {}
    roster_pages: dict[int, list[RosterPageResult]] = {}
    should_probe = args.probe or args.discover_pages or args.compare
    should_discover = args.discover_pages or args.compare
    if should_probe:
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
            if should_discover:
                roster_pages[candidate.location_id] = discover_roster_pages(
                    probes[candidate.location_id],
                    args.cache_dir,
                    args.timeout_seconds,
                    args.max_bytes,
                    args.max_roster_pages,
                    args.delay_seconds,
                    not args.no_external_roster_hosts,
                )

    comparisons: dict[int, RosterComparison] = {}
    if args.compare:
        for candidate in candidates:
            comparisons[candidate.location_id] = compare_roster_to_database(
                game_names_by_location.get(candidate.location_id, []),
                roster_pages.get(candidate.location_id, []),
            )

    md_path, json_path = write_reports(candidates, probes, roster_pages, comparisons, args.report_dir)
    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    print(
        f"candidates={len(candidates)} probed={len(probes)} "
        f"roster_page_groups={len(roster_pages)} compared={len(comparisons)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
