#!/usr/bin/env python3
"""Build report-only review plans from owner-published arcade rosters."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from scan_arcade_web_rosters import normalize_game_name


DEFAULT_REPORT_DIR = Path("reports")
DEFAULT_LIMIT = 10
CANONICAL_MATCH_THRESHOLD = 0.78
REVIEW_READY_MIN_MATCH_RATIO = 0.90
REVIEW_READY_MAX_CHANGE_PRESSURE = 0.15
REVIEW_READY_MAX_IGNORED_RATIO = 0.05
SHORT_ALIAS_MIN_SCORE = 0.9
MANUAL_TITLE_ALIASES = {
    "kuzure okami": "kodure ookami samurai assassin",
}


@dataclass(frozen=True)
class CanonicalCandidate:
    db_name: str
    website_name: str
    similarity: float


@dataclass(frozen=True)
class LocationReconciliation:
    location_id: int
    name: str
    city: str
    state: str
    game_count: int
    roster_url: str
    db_game_count: int
    matched_db_game_count: int
    match_ratio: float
    review_status: str
    review_note: str
    change_pressure: float
    ignored_ratio: float
    add_candidate_count: int
    remove_candidate_count: int
    canonical_candidate_count: int
    ignored_website_name_count: int
    add_candidates: list[str]
    remove_candidates: list[str]
    canonical_candidates: list[CanonicalCandidate]
    ignored_website_names: list[str]
    matched_db_games_sample: list[str]


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def read_manifest_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def infer_scan_report_path(manifest_report: Path) -> Path:
    name = manifest_report.name
    if not name.startswith("web_roster_manifests_") or not name.endswith(".csv"):
        raise ValueError("Cannot infer scan report path; pass --scan-report explicitly.")
    stamp = name.removeprefix("web_roster_manifests_").removesuffix(".csv")
    return manifest_report.with_name(f"web_roster_candidates_{stamp}.json")


def read_scan_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_game_name(left), normalize_game_name(right)).ratio()


def candidate_similarity(website_name: str, db_name: str) -> float:
    """Score likely same-game names, including terse owner-roster aliases."""
    website_norm = normalize_game_name(website_name)
    db_norm = normalize_game_name(db_name)
    if not website_norm or not db_norm:
        return 0.0
    manual_alias = MANUAL_TITLE_ALIASES.get(website_norm)
    if manual_alias and manual_alias == db_norm:
        return 0.95
    if website_norm == db_norm:
        return 1.0
    base = SequenceMatcher(None, website_norm, db_norm).ratio()
    website_compact = website_norm.replace(" ", "")
    db_compact = db_norm.replace(" ", "")
    if db_norm in {f"the {website_norm}", f"{website_norm} the"}:
        return max(base, SHORT_ALIAS_MIN_SCORE)
    if website_norm in {f"the {db_norm}", f"{db_norm} the"}:
        return max(base, SHORT_ALIAS_MIN_SCORE)
    if len(website_norm) >= 4 and (
        db_norm.startswith(f"{website_norm} ")
        or db_norm.endswith(f" {website_norm}")
        or f" {website_norm} " in f" {db_norm} "
        or db_compact.startswith(website_compact)
    ):
        return max(base, SHORT_ALIAS_MIN_SCORE)
    if len(db_norm) >= 4 and (
        website_norm.startswith(f"{db_norm} ")
        or website_norm.endswith(f" {db_norm}")
        or f" {db_norm} " in f" {website_norm} "
        or website_compact.startswith(db_compact)
    ):
        return max(base, SHORT_ALIAS_MIN_SCORE)
    return base


def is_noise_name(name: str) -> bool:
    normalized = normalize_game_name(name)
    if len(normalized) < 3:
        return True
    if re.fullmatch(
        r"(games?|pinball|arcade|video games?|classic games?|our games?|view games?|"
        r"arcade games?|pinball games?|machines?|more|about|contact|submit|welcome)",
        normalized,
    ):
        return True
    if re.search(r"@", name):
        return True
    if re.search(
        r"\b("
        r"account|blog|bottom of page|buy|careers?|contact|documentation|events?|facebook|"
        r"copyright|developed by|gift|hours?|instagram|leagues?|liability|location|menu|orders?|parties|party|photo|"
        r"powered by|price|private|rate|repair|rental|rent|sale|sell|service|shop|"
        r"sign in|sign out|submit|thanks|theme|ticket|top of page|tournaments?|waiver|weblizar|welcome"
        r")\b",
        normalized,
    ):
        return True
    return False


def pair_canonical_candidates(
    website_only_names: list[str],
    missing_db_games: list[str],
    threshold: float = CANONICAL_MATCH_THRESHOLD,
) -> tuple[list[CanonicalCandidate], set[str], set[str]]:
    candidates: list[CanonicalCandidate] = []
    used_website: set[str] = set()
    used_db: set[str] = set()
    scored_pairs: list[tuple[float, str, str]] = []
    for website_name in website_only_names:
        for db_name in missing_db_games:
            similarity = candidate_similarity(website_name, db_name)
            if similarity >= threshold:
                scored_pairs.append((similarity, website_name, db_name))
    for similarity, website_name, db_name in sorted(scored_pairs, reverse=True):
        if website_name in used_website or db_name in used_db:
            continue
        used_website.add(website_name)
        used_db.add(db_name)
        candidates.append(CanonicalCandidate(db_name=db_name, website_name=website_name, similarity=round(similarity, 4)))
    return candidates, used_website, used_db


def detect_review_status(
    *,
    match_ratio: float,
    db_game_count: int,
    add_candidate_count: int,
    remove_candidate_count: int,
    ignored_website_name_count: int,
    website_only_name_count: int,
) -> tuple[str, str, float, float]:
    if db_game_count == 0:
        return "needs_parser", "No DB games available for comparison.", 0.0, 0.0
    change_pressure = (add_candidate_count + remove_candidate_count) / db_game_count
    ignored_ratio = ignored_website_name_count / website_only_name_count if website_only_name_count else 0.0
    if match_ratio < REVIEW_READY_MIN_MATCH_RATIO:
        return "partial_or_stale", "Match rate is below the review-ready threshold.", round(change_pressure, 4), round(ignored_ratio, 4)
    if ignored_ratio > REVIEW_READY_MAX_IGNORED_RATIO:
        return "needs_parser", "Website extraction contains too much page chrome.", round(change_pressure, 4), round(ignored_ratio, 4)
    if change_pressure > REVIEW_READY_MAX_CHANGE_PRESSURE:
        return "needs_parser", "Add/remove pressure is too high for direct review.", round(change_pressure, 4), round(ignored_ratio, 4)
    return "review_ready", "Deterministic extraction looks similar to the clean roster pages.", round(change_pressure, 4), round(ignored_ratio, 4)


def build_location_reconciliation(
    manifest_record: dict[str, Any],
    comparison: dict[str, Any],
    max_names: int,
) -> LocationReconciliation:
    website_only_names = list(comparison.get("website_only_names", []))
    missing_db_games = list(comparison.get("missing_db_games", []))
    ignored = [name for name in website_only_names if is_noise_name(name)]
    usable_website_only = [name for name in website_only_names if name not in set(ignored)]
    canonical, used_website, used_db = pair_canonical_candidates(usable_website_only, missing_db_games)
    add_candidates = [name for name in usable_website_only if name not in used_website]
    remove_candidates = [name for name in missing_db_games if name not in used_db]
    db_game_count = int(comparison.get("db_game_count", manifest_record.get("db_game_count") or 0))
    matched_db_game_count = len(comparison.get("matched_db_games", []))
    match_ratio = round(float(manifest_record.get("match_ratio") or 0), 4)
    review_status, review_note, change_pressure, ignored_ratio = detect_review_status(
        match_ratio=match_ratio,
        db_game_count=db_game_count,
        add_candidate_count=len(add_candidates),
        remove_candidate_count=len(remove_candidates),
        ignored_website_name_count=len(ignored),
        website_only_name_count=len(website_only_names),
    )
    return LocationReconciliation(
        location_id=int(manifest_record["location_id"]),
        name=manifest_record["name"],
        city=manifest_record["city"],
        state=manifest_record["state"],
        game_count=int(manifest_record["game_count"] or 0),
        roster_url=manifest_record.get("best_roster_url", ""),
        db_game_count=db_game_count,
        matched_db_game_count=matched_db_game_count,
        match_ratio=match_ratio,
        review_status=review_status,
        review_note=review_note,
        change_pressure=change_pressure,
        ignored_ratio=ignored_ratio,
        add_candidate_count=len(add_candidates),
        remove_candidate_count=len(remove_candidates),
        canonical_candidate_count=len(canonical),
        ignored_website_name_count=len(ignored),
        add_candidates=add_candidates[:max_names],
        remove_candidates=remove_candidates[:max_names],
        canonical_candidates=canonical[:max_names],
        ignored_website_names=ignored[:max_names],
        matched_db_games_sample=list(comparison.get("matched_db_games", []))[:max_names],
    )


def select_manifest_records(records: list[dict[str, Any]], limit: int, location_id: int | None = None) -> list[dict[str, Any]]:
    likely = [record for record in records if parse_bool(record.get("likely_manifest", ""))]
    if location_id is not None:
        likely = [record for record in likely if int(record["location_id"]) == location_id]
    selected = sorted(
        likely,
        key=lambda record: (
            int(record.get("matched_db_game_count") or 0),
            int(record.get("game_count") or 0),
        ),
        reverse=True,
    )
    return selected if limit <= 0 else selected[:limit]


def build_reconciliations(
    manifest_records: list[dict[str, Any]],
    scan_report: dict[str, Any],
    limit: int,
    max_names: int,
    review_ready_only: bool = False,
    location_id: int | None = None,
) -> list[LocationReconciliation]:
    comparisons = scan_report.get("comparisons", {})
    reconciliations: list[LocationReconciliation] = []
    for manifest_record in select_manifest_records(manifest_records, limit, location_id=location_id):
        record_location_id = manifest_record["location_id"]
        comparison = comparisons.get(str(record_location_id))
        if not comparison:
            continue
        reconciliation = build_location_reconciliation(manifest_record, comparison, max_names)
        if review_ready_only and reconciliation.review_status != "review_ready":
            continue
        reconciliations.append(reconciliation)
    return reconciliations


def markdown_list(values: list[str]) -> str:
    if not values:
        return "none"
    return ", ".join(html.escape(value) for value in values)


def markdown_report(reconciliations: list[LocationReconciliation], generated_at: str) -> str:
    lines = [
        "# Web Roster Reconciliation Review",
        "",
        f"Generated: {generated_at}",
        "",
        "Report-only candidate plan. Do not apply these changes without human approval.",
        "",
        "| Status | Matched | Arcade | Location | Roster | Add | Remove | Canonical |",
        "| --- | ---: | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for item in reconciliations:
        location = ", ".join(part for part in (item.city, item.state) if part)
        roster = f"[source]({item.roster_url})" if item.roster_url else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    item.review_status,
                    f"{item.matched_db_game_count}/{item.db_game_count}",
                    html.escape(item.name),
                    html.escape(location),
                    roster,
                    str(item.add_candidate_count),
                    str(item.remove_candidate_count),
                    str(item.canonical_candidate_count),
                ]
            )
            + " |"
        )
    for item in reconciliations:
        lines.extend(
            [
                "",
                f"## {html.escape(item.name)}",
                "",
                f"- Location id: `{item.location_id}`",
                f"- Match: `{item.matched_db_game_count}/{item.db_game_count}` (`{item.match_ratio:.2%}`)",
                f"- Review status: `{item.review_status}` - {html.escape(item.review_note)}",
                f"- Change pressure: `{item.change_pressure:.2%}`; ignored website-name ratio: `{item.ignored_ratio:.2%}`",
                f"- Roster URL: {item.roster_url or 'none'}",
                f"- Add candidates ({item.add_candidate_count}, sample): {markdown_list(item.add_candidates)}",
                f"- Remove candidates ({item.remove_candidate_count}, sample): {markdown_list(item.remove_candidates)}",
            ]
        )
        if item.canonical_candidates:
            pairs = [
                f"{html.escape(candidate.website_name)} -> {html.escape(candidate.db_name)} ({candidate.similarity:.2f})"
                for candidate in item.canonical_candidates
            ]
            lines.append(f"- Canonical/rename candidates ({item.canonical_candidate_count}, sample): {', '.join(pairs)}")
        else:
            lines.append("- Canonical/rename candidates: none")
        lines.append(f"- Ignored website names ({item.ignored_website_name_count}, sample): {markdown_list(item.ignored_website_names)}")
        lines.append(f"- Matched DB sample: {markdown_list(item.matched_db_games_sample)}")
    return "\n".join(lines) + "\n"


def write_reports(reconciliations: list[LocationReconciliation], report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = report_dir / f"web_roster_reconciliation_{stamp}.md"
    json_path = report_dir / f"web_roster_reconciliation_{stamp}.json"
    md_path.write_text(markdown_report(reconciliations, generated_at), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "reconciliations": [asdict(item) for item in reconciliations],
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return md_path, json_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build report-only review plans from web roster scan reports.")
    parser.add_argument("--manifest-report", type=Path, required=True)
    parser.add_argument("--scan-report", type=Path)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-names", type=int, default=25)
    parser.add_argument("--location-id", type=int, help="Only reconcile one location id from the manifest.")
    parser.add_argument("--review-ready-only", action="store_true", help="Only emit rosters that pass the simple cleanliness detector.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    scan_report_path = args.scan_report or infer_scan_report_path(args.manifest_report)
    manifest_records = read_manifest_records(args.manifest_report)
    scan_report = read_scan_report(scan_report_path)
    reconciliations = build_reconciliations(
        manifest_records,
        scan_report,
        args.limit,
        args.max_names,
        review_ready_only=args.review_ready_only,
        location_id=args.location_id,
    )
    md_path, json_path = write_reports(reconciliations, args.report_dir)
    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    print(f"reconciled={len(reconciliations)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
