#!/usr/bin/env python3
"""Build conservative canonical game links without rewriting source rows."""

from __future__ import annotations

import argparse
import difflib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import duckdb

from arcade_db import ACTIVE_LOCATION_STATUSES, DEFAULT_DUCKDB, connect, execute_script, rows


DEFAULT_DB = DEFAULT_DUCKDB
REPORTS_DIR = Path("reports")
ACTIVE_STATUSES = ACTIVE_LOCATION_STATUSES
AUTO_THRESHOLD = 0.995
REVIEW_THRESHOLD = 0.90
EDITION_WORDS = {
    "arcade",
    "game",
    "limited",
    "edition",
    "premium",
    "pro",
    "le",
    "se",
    "special",
    "remake",
    "pinball",
}
VARIANT_TOKENS = {
    "2p",
    "4p",
    "6p",
    "ce",
    "dx",
    "dxplus",
    "le",
    "plus",
    "premium",
    "pro",
    "se",
}
MANUFACTURER_ALIASES = {
    "bally midway": "bally",
    "bally manufacturing": "bally",
    "chicago gaming company": "chicago gaming",
    "dave and busters": "dave and busters",
    "dave busters": "dave and busters",
    "stern pinball": "stern",
    "stern pinball inc": "stern",
    "stern pinball inc.": "stern",
    "williams electronic games": "williams",
    "williams electronics": "williams",
}


@dataclass(frozen=True)
class Game:
    game_id: int
    name: str
    manufacturer: str
    location_count: int
    active_location_count: int


@dataclass(frozen=True)
class Link:
    alias_game_id: int
    canonical_game_id: int
    confidence: float
    reason: str
    alias_name: str
    canonical_name: str


@dataclass(frozen=True)
class ReviewPair:
    left_game_id: int
    right_game_id: int
    confidence: float
    reason: str
    left_name: str
    right_name: str
    left_locations: int
    right_locations: int


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: Optional[str]) -> str:
    value = (value or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"\bac\s*/?\s*dc\b", "acdc", value)
    value = "".join(ch if ch.isalnum() else " " for ch in value)
    return re.sub(r"\s+", " ", value).strip()


def title_key(value: Optional[str]) -> str:
    normalized = normalize_text(value)
    if normalized.startswith("the "):
        normalized = normalized[4:]
    if normalized.endswith(" the"):
        normalized = normalized[:-4]
    return "".join(ch for ch in normalized if ch.isalnum())


def loose_title(value: Optional[str]) -> str:
    tokens = [token for token in normalize_text(value).split() if token not in EDITION_WORDS]
    if tokens[:1] == ["the"]:
        tokens = tokens[1:]
    return " ".join(tokens)


def manufacturer_key(value: Optional[str]) -> str:
    normalized = normalize_text(value)
    return MANUFACTURER_ALIASES.get(normalized, normalized)


def compatible_manufacturer(left: Game, right: Game) -> bool:
    left_key = manufacturer_key(left.manufacturer)
    right_key = manufacturer_key(right.manufacturer)
    if not left_key or not right_key:
        return True
    return left_key == right_key


def safe_exact_title_cluster(cluster: list[Game]) -> bool:
    manufacturers = {manufacturer_key(game.manufacturer) for game in cluster if manufacturer_key(game.manufacturer)}
    names = {normalize_text(game.name) for game in cluster}
    representative = next(iter(names), "")
    tokens = representative.split()
    if manufacturers and len(manufacturers) == 1 and all(manufacturer_key(game.manufacturer) for game in cluster):
        return True
    return len(representative) >= 12 or len(tokens) >= 3


def title_similarity(left: str, right: str) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return difflib.SequenceMatcher(None, left_norm, right_norm).ratio()


def digit_tokens(value: str) -> list[str]:
    return re.findall(r"\d+", normalize_text(value))


def variant_tokens(value: str) -> set[str]:
    tokens = set(normalize_text(value).split())
    compact_tokens = {token.replace(" ", "") for token in tokens}
    return (tokens | compact_tokens) & VARIANT_TOKENS


def safe_spelling_link(left: Game, right: Game) -> bool:
    if digit_tokens(left.name) != digit_tokens(right.name):
        return False
    if variant_tokens(left.name) != variant_tokens(right.name):
        return False
    return True


def source_rank(game_id: int) -> int:
    if game_id > 0:
        return 0
    if -1999999999 <= game_id <= -1000000000:
        return 1
    if -2999999999 <= game_id <= -2000000000:
        return 2
    return 3


def canonical_game(games: Iterable[Game]) -> Game:
    return sorted(
        games,
        key=lambda game: (
            source_rank(game.game_id),
            -game.active_location_count,
            -game.location_count,
            game.name.lower(),
            abs(game.game_id),
        ),
    )[0]


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS game_canonical_links (
            alias_game_id BIGINT PRIMARY KEY,
            canonical_game_id BIGINT NOT NULL,
            confidence DOUBLE NOT NULL,
            reason VARCHAR NOT NULL,
            source VARCHAR NOT NULL DEFAULT 'auto',
            notes VARCHAR,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_game_canonical_links_canonical
            ON game_canonical_links(canonical_game_id);
        """
    )


def load_games(conn: duckdb.DuckDBPyConnection) -> list[Game]:
    game_rows = rows(
        conn,
        f"""
        SELECT
            g.game_id,
            g.name,
            COALESCE(g.manufacturer, '') AS manufacturer,
            COUNT(DISTINCT lg.location_id) AS location_count,
            COUNT(DISTINCT CASE
                WHEN COALESCE(ls.status, 'active') IN ({",".join("?" for _ in ACTIVE_STATUSES)})
                THEN lg.location_id
            END) AS active_location_count
        FROM games g
        LEFT JOIN location_games lg ON lg.game_id = g.game_id
        LEFT JOIN locations l ON l.location_id = lg.location_id
        LEFT JOIN location_statuses ls ON ls.location_id = l.location_id
        GROUP BY g.game_id, g.name, g.manufacturer
        """,
        ACTIVE_STATUSES,
    )
    return [
        Game(
            game_id=int(row["game_id"]),
            name=str(row["name"] or ""),
            manufacturer=str(row["manufacturer"] or ""),
            location_count=int(row["location_count"] or 0),
            active_location_count=int(row["active_location_count"] or 0),
        )
        for row in game_rows
        if row["name"]
    ]


def exact_key_links(games: list[Game]) -> list[Link]:
    by_key: dict[str, list[Game]] = defaultdict(list)
    for game in games:
        key = title_key(game.name)
        if len(key) >= 4:
            by_key[key].append(game)

    links: list[Link] = []
    for cluster in by_key.values():
        if len(cluster) < 2:
            continue
        if not safe_exact_title_cluster(cluster):
            continue
        canonical = canonical_game(cluster)
        for game in cluster:
            if game.game_id == canonical.game_id:
                continue
            links.append(
                Link(
                    alias_game_id=game.game_id,
                    canonical_game_id=canonical.game_id,
                    confidence=1.0,
                    reason="exact_compact_title",
                    alias_name=game.name,
                    canonical_name=canonical.name,
                )
            )
    return links


def spelling_links_and_review(games: list[Game], existing_alias_ids: set[int]) -> tuple[list[Link], list[ReviewPair]]:
    by_prefix: dict[str, list[Game]] = defaultdict(list)
    for game in games:
        key = title_key(game.name)
        if len(key) >= 4:
            by_prefix[key[:4]].append(game)

    links: list[Link] = []
    review_pairs: list[ReviewPair] = []
    seen_pairs: set[tuple[int, int]] = set()
    linked_aliases = set(existing_alias_ids)

    for cluster in by_prefix.values():
        if len(cluster) < 2:
            continue
        for index, left in enumerate(cluster):
            for right in cluster[index + 1 :]:
                pair_key = tuple(sorted((left.game_id, right.game_id)))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                if not compatible_manufacturer(left, right):
                    continue
                if title_key(left.name) == title_key(right.name):
                    continue

                confidence = title_similarity(left.name, right.name)
                loose_confidence = title_similarity(loose_title(left.name), loose_title(right.name))
                if confidence >= AUTO_THRESHOLD and safe_spelling_link(left, right):
                    canonical = canonical_game([left, right])
                    alias = right if canonical.game_id == left.game_id else left
                    if alias.game_id not in linked_aliases:
                        links.append(
                            Link(
                                alias_game_id=alias.game_id,
                                canonical_game_id=canonical.game_id,
                                confidence=confidence,
                                reason="high_confidence_spelling",
                                alias_name=alias.name,
                                canonical_name=canonical.name,
                            )
                        )
                        linked_aliases.add(alias.game_id)
                elif loose_confidence >= REVIEW_THRESHOLD:
                    review_pairs.append(
                        ReviewPair(
                            left_game_id=left.game_id,
                            right_game_id=right.game_id,
                            confidence=loose_confidence,
                            reason="possible_variant_or_edition",
                            left_name=left.name,
                            right_name=right.name,
                            left_locations=left.active_location_count,
                            right_locations=right.active_location_count,
                        )
                    )
    return links, review_pairs


def proposed_links(games: list[Game]) -> tuple[list[Link], list[ReviewPair]]:
    exact_links = exact_key_links(games)
    exact_aliases = {link.alias_game_id for link in exact_links}
    spelling_links, review_pairs = spelling_links_and_review(games, exact_aliases)

    best_by_alias: dict[int, Link] = {}
    for link in [*exact_links, *spelling_links]:
        current = best_by_alias.get(link.alias_game_id)
        if current is None or (link.confidence, -source_rank(link.canonical_game_id)) > (
            current.confidence,
            -source_rank(current.canonical_game_id),
        ):
            best_by_alias[link.alias_game_id] = link
    return sorted(best_by_alias.values(), key=lambda link: (link.canonical_game_id, link.alias_game_id)), review_pairs


def apply_links(conn: duckdb.DuckDBPyConnection, links: list[Link]) -> None:
    if not links:
        return
    timestamp = utc_now()
    alias_ids = [link.alias_game_id for link in links]
    existing = {
        int(row["alias_game_id"]): str(row["source"] or "")
        for row in rows(
            conn,
            """
            SELECT alias_game_id, source
            FROM game_canonical_links
            WHERE alias_game_id IN (SELECT unnest(?))
            """,
            (alias_ids,),
        )
    }
    auto_alias_ids = [alias_id for alias_id, source in existing.items() if source == "auto"]
    if auto_alias_ids:
        conn.execute(
            "DELETE FROM game_canonical_links WHERE source = 'auto' AND alias_game_id IN (SELECT unnest(?))",
            (auto_alias_ids,),
        )
    insertable = [link for link in links if existing.get(link.alias_game_id) in (None, "auto")]
    if not insertable:
        return
    conn.executemany(
        """
        INSERT INTO game_canonical_links (
            alias_game_id, canonical_game_id, confidence, reason, source, notes, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'auto', NULL, ?, ?)
        """,
        [
            (
                link.alias_game_id,
                link.canonical_game_id,
                link.confidence,
                link.reason,
                timestamp,
                timestamp,
            )
            for link in insertable
        ],
    )


def write_report(links: list[Link], review_pairs: list[ReviewPair], reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"game_canonicalization_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    lines = [
        "# Game Canonicalization Report",
        "",
        f"- Auto-link candidates: {len(links)}",
        f"- Review-only pairs: {len(review_pairs)}",
        "",
        "## Auto-Link Candidates",
        "",
        "| alias_id | alias_name | canonical_id | canonical_name | confidence | reason |",
        "| --- | --- | --- | --- | ---: | --- |",
    ]
    for link in links[:500]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(link.alias_game_id),
                    escape_md(link.alias_name),
                    str(link.canonical_game_id),
                    escape_md(link.canonical_name),
                    f"{link.confidence:.3f}",
                    link.reason,
                ]
            )
            + " |"
        )
    if len(links) > 500:
        lines.append(f"| ... | {len(links) - 500} additional links omitted |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Review-Only Possible Variant Links",
            "",
            "| left_id | left_name | left_locations | right_id | right_name | right_locations | confidence | reason |",
            "| --- | --- | ---: | --- | --- | ---: | ---: | --- |",
        ]
    )
    for pair in sorted(review_pairs, key=lambda item: item.confidence, reverse=True)[:500]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(pair.left_game_id),
                    escape_md(pair.left_name),
                    str(pair.left_locations),
                    str(pair.right_game_id),
                    escape_md(pair.right_name),
                    str(pair.right_locations),
                    f"{pair.confidence:.3f}",
                    pair.reason,
                ]
            )
            + " |"
        )
    if len(review_pairs) > 500:
        lines.append(f"| ... | {len(review_pairs) - 500} additional review pairs omitted |  |  |  |  |  |  |")
    path.write_text("\n".join(lines) + "\n")
    return path


def escape_md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create canonical game links for source-specific duplicate game rows.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="Write auto-links to game_canonical_links.")
    parser.add_argument("--report", action="store_true", help="Write a markdown review report.")
    parser.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with connect(args.db, read_only=not args.apply) as conn:
        if args.apply:
            ensure_schema(conn)
        games = load_games(conn)
        links, review_pairs = proposed_links(games)
        if args.apply:
            apply_links(conn, links)
            conn.commit()
    report_path = write_report(links, review_pairs, args.reports_dir) if args.report else None
    print(f"games scanned: {len(games)}")
    print(f"auto-link candidates: {len(links)}")
    print(f"review-only pairs: {len(review_pairs)}")
    if args.apply:
        print(f"auto-links applied: {len(links)}")
    else:
        print("dry run: no database changes applied")
    if report_path:
        print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
