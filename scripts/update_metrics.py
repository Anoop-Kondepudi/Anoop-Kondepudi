#!/usr/bin/env python3
"""Generate the public profile metrics SVG without publishing repository details."""

from __future__ import annotations

import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_ROOT = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "assets" / "metrics.svg"


def request_json(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[Any, int]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "github-profile-metrics",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return (json.loads(body) if body else None), response.status
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub API request failed with HTTP {error.code}") from error


def api_get(path: str, token: str, **params: Any) -> tuple[Any, int]:
    query = urllib.parse.urlencode(params)
    suffix = f"?{query}" if query else ""
    return request_json(f"{API_ROOT}{path}{suffix}", token)


def list_repositories(token: str) -> list[dict[str, Any]]:
    repositories: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        batch, _ = api_get(
            "/user/repos",
            token,
            affiliation="owner,collaborator,organization_member",
            per_page=100,
            page=page,
        )
        if not batch:
            break
        for repository in batch:
            repositories[repository["full_name"]] = repository
        if len(batch) < 100:
            break
        page += 1
    return list(repositories.values())


def contribution_total(token: str, username: str, start: datetime, end: datetime) -> int:
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar { totalContributions }
        }
      }
    }
    """
    result, _ = request_json(
        GRAPHQL_URL,
        token,
        method="POST",
        payload={
            "query": query,
            "variables": {
                "login": username,
                "from": start.isoformat().replace("+00:00", "Z"),
                "to": end.isoformat().replace("+00:00", "Z"),
            },
        },
    )
    return int(
        result["data"]["user"]["contributionsCollection"]["contributionCalendar"][
            "totalContributions"
        ]
    )


def pull_request_count(
    token: str,
    username: str,
    start: datetime,
    end: datetime,
    *,
    merged: bool = False,
) -> int:
    date_range = f"{start.date().isoformat()}..{end.date().isoformat()}"
    qualifiers = ["is:pr", f"author:{username}", f"created:{date_range}"]
    if merged:
        qualifiers.append("is:merged")
    result, _ = api_get("/search/issues", token, q=" ".join(qualifiers), per_page=1)
    return int(result["total_count"])


def contributor_stats(
    token: str,
    repositories: list[dict[str, Any]],
    username: str,
    cutoff: int,
) -> dict[str, int]:
    totals = {"additions": 0, "deletions": 0, "commits": 0, "repositories": 0}
    skipped = 0

    for repository in repositories:
        owner, name = repository["full_name"].split("/", 1)
        path = f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/stats/contributors"
        data: Any = None

        for attempt in range(3):
            try:
                data, status = api_get(path, token)
            except RuntimeError:
                skipped += 1
                break
            if status != 202:
                break
            time.sleep(2**attempt)

        if not isinstance(data, list):
            continue

        repository_commits = 0
        for contributor in data:
            login = (contributor.get("author") or {}).get("login", "")
            if login.casefold() != username.casefold():
                continue
            for week in contributor.get("weeks", []):
                if int(week.get("w", 0)) < cutoff:
                    continue
                totals["additions"] += int(week.get("a", 0))
                totals["deletions"] += int(week.get("d", 0))
                commits = int(week.get("c", 0))
                totals["commits"] += commits
                repository_commits += commits

        if repository_commits:
            totals["repositories"] += 1

    print(
        f"Analyzed {len(repositories)} accessible repositories; "
        f"skipped {skipped} inaccessible statistics responses."
    )
    return totals


def compact_number(value: int, *, signed: bool = False) -> str:
    sign = "+" if signed else ""
    if value >= 1_000_000:
        return f"{sign}{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{sign}{value / 1_000:.1f}K"
    return f"{sign}{value:,}"


def render_svg(
    *,
    year: int,
    updated: datetime,
    contributions: int,
    pull_requests: int,
    merged_pull_requests: int,
    stats: dict[str, int],
) -> str:
    values = [
        (f"{contributions:,}", "CONTRIBUTIONS", "#8fe3d6"),
        (f"{pull_requests:,}", "PULL REQUESTS OPENED", "#ffbd6b"),
        (f"{merged_pull_requests:,}", "PULL REQUESTS MERGED", "#8fe3d6"),
        (f"{stats['commits']:,}", "COMMITS ANALYZED", "#ffbd6b"),
        (compact_number(stats["additions"], signed=True), "LINES ADDED", "#8fe3d6"),
        (f"-{compact_number(stats['deletions'])}", "LINES REMOVED", "#ffbd6b"),
    ]
    positions = [(48, 92), (424, 92), (800, 92), (48, 204), (424, 204), (800, 204)]
    cards = []
    for (value, label, accent), (x, y) in zip(values, positions):
        cards.append(
            f"""
    <g transform="translate({x} {y})">
      <rect width="352" height="92" rx="14" fill="#ffffff" fill-opacity="0.045" stroke="#ffffff" stroke-opacity="0.08"/>
      <text x="22" y="48" fill="#f5f1e8" font-size="30" font-weight="750">{html.escape(value)}</text>
      <text x="22" y="72" fill="{accent}" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="12" font-weight="700" letter-spacing="1.4">{html.escape(label)}</text>
    </g>"""
        )

    updated_text = updated.strftime("UPDATED %b %d, %Y").upper()
    repository_text = (
        f"DEFAULT-BRANCH ACTIVITY ACROSS {stats['repositories']:,} REPOSITORIES"
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="362" viewBox="0 0 1200 362" role="img" aria-labelledby="title description">
  <title id="title">{year} engineering ledger</title>
  <desc id="description">Year-to-date aggregate GitHub engineering activity.</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#08151e"/>
      <stop offset="1" stop-color="#102d3c"/>
    </linearGradient>
    <linearGradient id="rule" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#ffb454"/>
      <stop offset="1" stop-color="#55d6c2"/>
    </linearGradient>
  </defs>
  <rect width="1200" height="362" rx="24" fill="url(#bg)"/>
  <text x="48" y="49" fill="#f5f1e8" font-family="Georgia, 'Times New Roman', serif" font-size="26" font-weight="700">Engineering ledger</text>
  <text x="1152" y="47" text-anchor="end" fill="#7f9ba4" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="13" letter-spacing="1.5">YEAR TO DATE / {year}</text>
  <rect x="48" y="67" width="1104" height="2" rx="1" fill="url(#rule)" opacity="0.8"/>
  <g font-family="ui-sans-serif, system-ui, sans-serif">{''.join(cards)}
  </g>
  <text x="48" y="335" fill="#7f9ba4" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="12">{html.escape(repository_text)}</text>
  <text x="1152" y="335" text-anchor="end" fill="#7f9ba4" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="12">{updated_text}</text>
</svg>
"""


def main() -> None:
    token = os.environ.get("PROFILE_METRICS_TOKEN", "").strip()
    username = os.environ.get("PROFILE_METRICS_USER", "").strip()
    if not token:
        print("PROFILE_METRICS_TOKEN is not configured; keeping the existing card.")
        return
    if not username:
        raise RuntimeError("PROFILE_METRICS_USER is required")

    now = datetime.now(timezone.utc)
    start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    repositories = list_repositories(token)
    stats = contributor_stats(token, repositories, username, int(start.timestamp()))
    svg = render_svg(
        year=now.year,
        updated=now,
        contributions=contribution_total(token, username, start, now),
        pull_requests=pull_request_count(token, username, start, now),
        merged_pull_requests=pull_request_count(
            token, username, start, now, merged=True
        ),
        stats=stats,
    )
    OUTPUT_PATH.write_text(svg, encoding="utf-8")
    print("Updated aggregate profile metrics without publishing repository details.")


if __name__ == "__main__":
    main()
