#!/usr/bin/env python3
"""Render the profile cards from live GitHub data.

Privacy contract: private repositories are read only to compute aggregate
numbers (contribution counts, streaks, language byte totals). No private
repository name, description, or any per-repository detail is ever written
to the generated SVGs. The only repository names that appear in output are
the ones in FEATURED (all public).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
FONTS = ASSETS / "fonts"

USERNAME = "Anoop-Kondepudi"
DISPLAY_NAME = "Anoop Kondepudi"
GRAPHQL_URL = "https://api.github.com/graphql"
API_ROOT = "https://api.github.com"
PLATFORM_FILE = ROOT / "platform.json"  # optional, semi-static platform stats

# Languages that are markup / data rather than authored program code.
MARKUP_LANGUAGES = {
    "HTML", "CSS", "SCSS", "Less", "Markdown", "MDX", "SVG", "XML", "JSON",
    "YAML", "TeX", "Jupyter Notebook", "Rich Text Format", "Batchfile",
}
LANGUAGE_RENAMES = {"PLpgSQL": "SQL", "TSQL": "SQL", "PLSQL": "SQL"}
LANGUAGE_MIN_SHARE = 0.005  # below this share a language folds into Other

W = 880  # card canvas width; README images render at 100% width


# --------------------------------------------------------------------------
# Data fetch
# --------------------------------------------------------------------------

def gql(token: str, query: str, variables: dict | None = None):
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-cards",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if body.get("errors"):
        raise RuntimeError(f"GraphQL error: {body['errors']}")
    return body["data"]


def rest(token: str, path: str):
    req = urllib.request.Request(
        API_ROOT + path,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "profile-cards",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
        return (json.loads(body) if body else None), resp.status


def fetch_loc(token: str, repo_names: list[str], year_epoch: int) -> dict:
    """Sum this user's additions/deletions/commits (default branch) across repos,
    both all-time and for weeks starting on/after year_epoch.

    /stats/contributors returns 202 while GitHub warms its cache; retry briefly
    and skip repos that never settle rather than failing the whole render.
    """
    totals = {"all": {"additions": 0, "deletions": 0, "commits": 0},
              "year": {"additions": 0, "deletions": 0, "commits": 0}}
    for name in repo_names:
        data = None
        for attempt in range(5):
            try:
                body, status = rest(token, f"/repos/{USERNAME}/{name}/stats/contributors")
            except Exception:
                break
            if status == 202:
                time.sleep(min(2 ** attempt, 8))
                continue
            data = body
            break
        if not isinstance(data, list):
            continue
        for contributor in data:
            if (contributor.get("author") or {}).get("login", "").casefold() != USERNAME.casefold():
                continue
            for week in contributor.get("weeks", []):
                buckets = ["all", "year"] if int(week.get("w", 0)) >= year_epoch else ["all"]
                for b in buckets:
                    totals[b]["additions"] += int(week.get("a", 0))
                    totals[b]["deletions"] += int(week.get("d", 0))
                    totals[b]["commits"] += int(week.get("c", 0))
    return totals


STATS_URL = os.environ.get(
    "STATS_URL", "https://www.studysolutions.app/api/public/profile-stats")


def fetch_platform_stats() -> dict:
    """Live aggregate counts from the platform's public stats endpoint.

    Returns {} when the endpoint is unreachable so the committed platform.json
    snapshot keeps the card rendering.
    """
    try:
        req = urllib.request.Request(STATS_URL, headers={"User-Agent": "profile-cards"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        return {k: v for k, v in data.items() if isinstance(v, (int, float)) and v > 0}
    except Exception:
        return {}


def fetch_discord_members(invite: str = "studysolutions") -> int | None:
    """Live member count from Discord's public invite API (no auth needed)."""
    try:
        req = urllib.request.Request(
            f"https://discord.com/api/v10/invites/{invite}?with_counts=true",
            headers={"User-Agent": "profile-cards"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return int(json.loads(resp.read().decode())["approximate_member_count"])
    except Exception:
        return None


def fetch(token: str) -> dict:
    this_year = datetime.now(timezone.utc).year
    viewer = gql(token, """
    query {
      viewer {
        login name createdAt followers { totalCount }
        private: repositories(first: 1, ownerAffiliations: OWNER, privacy: PRIVATE) { totalCount }
        repositories(first: 100, ownerAffiliations: OWNER) {
          totalCount
          nodes {
            name isPrivate isFork isArchived pushedAt stargazerCount
            languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
              edges { size node { name color } }
            }
          }
        }
      }
      merged: search(type: ISSUE, query: "author:%s is:pr is:merged") { issueCount }
      merged_year: search(type: ISSUE, query: "author:%s is:pr is:merged merged:>=%d-01-01") { issueCount }
    }
    """ % (USERNAME, USERNAME, this_year))

    me = viewer["viewer"]
    if me["login"].casefold() != USERNAME.casefold():
        raise RuntimeError("token does not belong to the expected account")
    if me["private"]["totalCount"] == 0:
        raise RuntimeError(
            "token cannot see private repositories - refusing to render "
            "misleading public-only numbers"
        )

    created = datetime.fromisoformat(me["createdAt"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    calendars = []
    year_start = created
    while year_start < now:
        year_end = min(year_start + timedelta(days=364), now)
        data = gql(token, """
        query($from: DateTime!, $to: DateTime!) {
          viewer {
            contributionsCollection(from: $from, to: $to) {
              contributionCalendar {
                totalContributions
                weeks { contributionDays { date contributionCount } }
              }
            }
          }
        }
        """, {"from": year_start.isoformat(), "to": year_end.isoformat()})
        calendars.append(data["viewer"]["contributionsCollection"]["contributionCalendar"])
        year_start = year_end + timedelta(seconds=1)

    loc = fetch_loc(
        token,
        [r["name"] for r in me["repositories"]["nodes"]
         if not r["isFork"] and not r["isArchived"]],
        int(datetime(this_year, 1, 1, tzinfo=timezone.utc).timestamp()),
    )

    return {
        "fetched_at": now.isoformat(),
        "created_at": me["createdAt"],
        "repositories": me["repositories"],
        "prs_merged": viewer["merged"]["issueCount"],
        "prs_merged_year": viewer["merged_year"]["issueCount"],
        "calendars": calendars,
        "loc": loc,
        "discord_members": fetch_discord_members(),
    }


# --------------------------------------------------------------------------
# Aggregation (everything leaving this function is aggregate-only)
# --------------------------------------------------------------------------

def compute(raw: dict) -> dict:
    now = datetime.fromisoformat(raw["fetched_at"])
    today = now.date()

    # calendar windows include full padded weeks, so ranges can overlap at the
    # seams - take max per date rather than summing to avoid double counting
    days: dict[str, int] = {}
    for cal in raw["calendars"]:
        for week in cal["weeks"]:
            for d in week["contributionDays"]:
                days[d["date"]] = max(days.get(d["date"], 0), d["contributionCount"])

    def count(day: date) -> int:
        return days.get(day.isoformat(), 0)

    total_all_time = sum(days.values())
    ytd = sum(c for d, c in days.items() if d.startswith(str(today.year)))
    elapsed_days = (today - date(today.year, 1, 1)).days + 1
    per_day = ytd / max(elapsed_days, 1)

    # current streak: count back from today; a zero today does not break it yet
    streak = 0
    cursor = today
    if count(cursor) == 0:
        cursor -= timedelta(days=1)
    while count(cursor) > 0:
        streak += 1
        cursor -= timedelta(days=1)

    longest = run = 0
    for d in sorted(days):
        run = run + 1 if days[d] > 0 else 0
        longest = max(longest, run)

    active_365 = sum(
        1 for d, c in days.items()
        if c > 0 and date.fromisoformat(d) > today - timedelta(days=365)
    )

    spark = [count(today - timedelta(days=i)) for i in range(90, -1, -1)]

    repos = [
        r for r in raw["repositories"]["nodes"]
        if not r["isFork"] and not r["isArchived"]
    ]
    private_count = sum(1 for r in repos if r["isPrivate"])
    last_push = max(
        (r["pushedAt"] for r in repos if r["name"] != USERNAME),
        default=None,
    )

    lang_bytes: dict[str, int] = {}
    lang_colors: dict[str, str] = {}
    for r in repos:
        for edge in r["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in MARKUP_LANGUAGES:
                continue
            name = LANGUAGE_RENAMES.get(name, name)
            lang_bytes[name] = lang_bytes.get(name, 0) + edge["size"]
            lang_colors.setdefault(name, edge["node"]["color"] or "#8b8fa3")
    total_bytes = sum(lang_bytes.values()) or 1
    ranked = sorted(lang_bytes.items(), key=lambda kv: -kv[1])
    languages, other = [], 0.0
    for name, size in ranked:
        share = size / total_bytes
        if share >= LANGUAGE_MIN_SHARE and len(languages) < 5:
            languages.append({"name": name, "share": share, "color": lang_colors[name]})
        else:
            other += share
    if other > 0.001:
        languages.append({"name": "Other", "share": other, "color": "#6e7286"})

    loc = raw.get("loc", {})
    loc_year = loc.get("year", {})
    platform = {}
    if PLATFORM_FILE.exists():
        platform = json.loads(PLATFORM_FILE.read_text())

    # live endpoint values win over the committed snapshot
    live = fetch_platform_stats()
    live_fields = []
    for key, label in (("ai_checks_all_time", "AI CHECKS"),
                       ("registered_users", "USERS"),
                       ("supabase_requests_24h", "SUPABASE")):
        if live.get(key):
            platform[key] = int(live[key])
            live_fields.append(label)

    # daily local jobs push these as Actions variables
    if os.environ.get("PLATFORM_VERCEL_REQUESTS"):
        platform["vercel_requests"] = int(os.environ["PLATFORM_VERCEL_REQUESTS"])
        platform["vercel_window"] = os.environ.get("PLATFORM_VERCEL_WINDOW", "LAST 24 HOURS")
    platform["live_fields"] = live_fields

    discord = raw.get("discord_members") or platform.get("discord_members_fallback")

    return {
        "now": now,
        "since_year": datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00")).year,
        "total_all_time": total_all_time,
        "commits_y": loc_year.get("commits", 0),
        "lines_y": loc_year.get("additions", 0) + loc_year.get("deletions", 0),
        "prs_merged_y": raw.get("prs_merged_year", 0),
        "discord": discord,
        "platform": platform,
        "ytd": ytd,
        "year": today.year,
        "per_day": per_day,
        "streak": streak,
        "longest": longest,
        "active_365": active_365,
        "days": days,
        "spark": spark,
        "spark_peak": max(spark) if spark else 0,
        "spark_peak_date": (today - timedelta(days=(len(spark) - 1 - spark.index(max(spark))))) if spark else today,
        "repo_count": len(repos),
        "private_count": private_count,
        "last_push": last_push,
        "prs_merged": raw["prs_merged"],
        "languages": languages,
    }


# --------------------------------------------------------------------------
# Themes & type helpers
# --------------------------------------------------------------------------

DARK = {
    "id": "dark",
    "card_top": "#0b0b11",
    "card_bottom": "#08080d",
    "border": "rgba(255,255,255,0.085)",
    "hairline": "rgba(255,255,255,0.07)",
    "ink": "#f2f2f7",
    "sub": "#a6aabd",
    "muted": "#666b80",
    "faint": "rgba(255,255,255,0.035)",
    "accent": "#a78bfa",
    "accent_dim": "rgba(167,139,250,0.55)",
    "accent_soft": "rgba(167,139,250,0.14)",
    "accent_border": "rgba(167,139,250,0.28)",
    "good": "#34d399",
    "seg_stroke": "none",
    "lang_overrides": {"SQL": "#4787ba"},  # canonical PLpgSQL navy fails 3:1 on obsidian
    "spark_bar": "rgba(167,139,250,0.34)",
    "heat_ramp": ["rgba(255,255,255,0.05)", "rgba(167,139,250,0.22)", "rgba(167,139,250,0.45)",
                  "rgba(167,139,250,0.70)", "#a78bfa"],
}
LIGHT = {
    "id": "light",
    "card_top": "#ffffff",
    "card_bottom": "#fbfbfd",
    "border": "#d7d9e0",
    "hairline": "#e6e7ee",
    "ink": "#181920",
    "sub": "#4c5064",
    "muted": "#8f93a8",
    "faint": "rgba(20,22,40,0.03)",
    "accent": "#6a48d7",
    "accent_dim": "rgba(106,72,215,0.6)",
    "accent_soft": "rgba(106,72,215,0.09)",
    "accent_border": "rgba(106,72,215,0.35)",
    "good": "#0e8f63",
    "seg_stroke": "rgba(20,22,40,0.18)",
    "lang_overrides": {},
    "spark_bar": "rgba(106,72,215,0.20)",
    "heat_ramp": ["#eef0f4", "#dcd2f6", "#b7a2ee", "#8a68e2", "#5b38d2"],
}

_font_cache: dict[str, str] = {}


def font_b64(stem: str) -> str:
    if stem not in _font_cache:
        _font_cache[stem] = (FONTS / f"{stem}.woff2.b64").read_text().strip()
    return _font_cache[stem]


FONT_FAMILIES = {
    "disp": ("Bricolage", "bricolage-600", 600, "normal"),
    "serif": ("InstrumentIt", "instrument-italic", 400, "italic"),
    "mono": ("JBMono", "jetbrains-400", 400, "normal"),
    "monobold": ("JBMonoBold", "jetbrains-600", 600, "normal"),
}


def font_css(keys: list[str]) -> str:
    faces = []
    for k in keys:
        fam, stem, weight, style = FONT_FAMILIES[k]
        faces.append(
            f"@font-face{{font-family:'{fam}';font-style:{style};font-weight:{weight};"
            f"src:url(data:font/woff2;base64,{font_b64(stem)}) format('woff2');}}"
        )
    return "".join(faces)


def mono_w(text: str, size: float, ls: float = 0.0) -> float:
    """JetBrains Mono has an exact 0.6em advance."""
    n = len(text)
    return n * size * 0.6 + max(n - 1, 0) * ls


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt(n: int) -> str:
    return f"{n:,}"


def compact(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:,}"


def rel_time(iso: str | None, now: datetime) -> str:
    if not iso:
        return "—"
    then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    s = int((now - then).total_seconds())
    if s < 3600:
        return f"{max(s // 60, 1)} MIN AGO"
    if s < 86400:
        return f"{s // 3600}H AGO"
    return f"{s // 86400}D AGO"


# Everything is fully visible with no animation applied; motion is a looping
# enhancement only. Renderers that freeze SVG at t=0 (image proxies, social
# previews) must still show the complete card, so entrance animations are
# deliberately absent.
BASE_CSS = """
text{-webkit-font-smoothing:antialiased}
@media (prefers-reduced-motion: no-preference){
 .pulse{animation:pulse 2.6s ease-in-out infinite}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
 .ring{transform-origin:center;transform-box:fill-box;animation:ring 2.6s ease-out infinite}
 @keyframes ring{0%{transform:scale(.5);opacity:.7}70%,100%{transform:scale(2.3);opacity:0}}
}
"""


def svg_shell(name: str, theme: dict, height: int, fonts: list[str], body: str,
              title: str, accent_border: bool = False) -> str:
    border = theme["accent_border"] if accent_border else theme["border"]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}"
  viewBox="0 0 {W} {height}" role="img" aria-label="{esc(title)}">
  <style>{font_css(fonts)}{BASE_CSS}</style>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{theme['card_top']}"/>
      <stop offset="1" stop-color="{theme['card_bottom']}"/>
    </linearGradient>
    <linearGradient id="sparkfill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{theme['accent']}" stop-opacity="0.22"/>
      <stop offset="1" stop-color="{theme['accent']}" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="corner" cx="1" cy="0" r="1.15">
      <stop offset="0" stop-color="{theme['accent']}" stop-opacity="0.10"/>
      <stop offset="0.55" stop-color="{theme['accent']}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect x="0.5" y="0.5" width="{W - 1}" height="{height - 1}" rx="13.5" fill="url(#bg)" stroke="{border}"/>
  {body}
</svg>
"""


def live_dot(x: float, y: float, theme: dict, color: str | None = None) -> str:
    c = color or theme["good"]
    return (
        f'<circle class="ring" cx="{x}" cy="{y}" r="3.5" fill="none" stroke="{c}" stroke-width="1" opacity="0.8"/>'
        f'<circle class="pulse" cx="{x}" cy="{y}" r="3" fill="{c}"/>'
    )


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------

def ledger_row(label: str, value: str, baseline: int, t: dict) -> str:
    """One accounting-ledger row: label, dotted leader, right-aligned exact value."""
    mono, monob = FONT_FAMILIES["mono"][0], FONT_FAMILIES["monobold"][0]
    label_w = mono_w(label, 10, 1.5)
    value_w = mono_w(value, 21, 0.2)
    x0 = 36 + label_w + 16
    x1 = W - 36 - value_w - 16
    leader = ""
    if x1 - x0 > 24:
        leader = (f'<line x1="{x0:.0f}" y1="{baseline - 4}" x2="{x1:.0f}" y2="{baseline - 4}" '
                  f'stroke="{t["muted"]}" stroke-opacity="0.35" stroke-width="1.2" '
                  f'stroke-dasharray="0.1 6" stroke-linecap="round"/>')
    return (
        f'<text x="36" y="{baseline}" font-family="{mono}" font-size="10" letter-spacing="1.5" '
        f'fill="{t["sub"]}">{esc(label)}</text>{leader}'
        f'<text x="{W - 36}" y="{baseline + 3}" text-anchor="end" font-family="{monob}" '
        f'font-weight="600" font-size="21" letter-spacing="0.2" fill="{t["ink"]}">{esc(value)}</text>'
    )


def card_activity(m: dict, t: dict) -> str:
    H = 318
    mono = FONT_FAMILIES["mono"][0]
    serif = FONT_FAMILIES["serif"][0]
    ink, sub, muted = t["ink"], t["sub"], t["muted"]
    push = rel_time(m["last_push"], m["now"])

    rows = [
        (f"COMMITS · {m['year']}", fmt(m["commits_y"])),
        (f"LINES OF CODE CHANGED · {m['year']}", fmt(m["lines_y"])),
        (f"PULL REQUESTS MERGED · {m['year']}", fmt(m["prs_merged_y"])),
        ("CONTRIBUTIONS · ALL-TIME", fmt(m["total_all_time"])),
    ]
    row_svg = "".join(ledger_row(lbl, val, 104 + i * 35, t) for i, (lbl, val) in enumerate(rows))

    # language bar
    by0, bh = 258, 8
    bar_w = W - 72
    gaps = 2
    lang_parts, legend_parts = "", ""
    x_cursor = 36.0
    n_langs = len(m["languages"])
    lx_cursor = 36.0
    for i, lang in enumerate(m["languages"]):
        color = t["lang_overrides"].get(lang["name"], lang["color"])
        seg_w = max(lang["share"] * (bar_w - gaps * (n_langs - 1)), 4.0)
        stroke = f' stroke="{t["seg_stroke"]}" stroke-width="0.5"' if t["seg_stroke"] != "none" else ""
        lang_parts += f'<rect x="{x_cursor:.1f}" y="{by0}" width="{seg_w:.1f}" height="{bh}" rx="2.5" fill="{color}"{stroke}/>'
        x_cursor += seg_w + gaps
        pct = f"{lang['share'] * 100:.1f}".rstrip("0").rstrip(".")
        label = f"{lang['name'].upper()} {pct}%"
        legend_parts += (
            f'<circle cx="{lx_cursor + 3:.1f}" cy="{by0 + 26}" r="3" fill="{color}"/>'
            f'<text x="{lx_cursor + 12:.1f}" y="{by0 + 29.5}" font-family="{mono}" font-size="9.5" letter-spacing="0.8" fill="{sub}">{label}</text>'
        )
        lx_cursor += 12 + mono_w(label, 9.5, 0.8) + 22

    updated = m["now"].strftime("%b %d · %H:%M UTC").upper()

    body = f"""
  <g>
    <text x="34" y="49" font-family="{serif}" font-style="italic" font-size="25" fill="{ink}">The ledger</text>
    {live_dot(180, 44.5, t)}
    <text x="192" y="47" font-family="{mono}" font-size="9" letter-spacing="1.5" fill="{sub}">LAST PUSH · {push}</text>
    <text x="{W - 36}" y="47" text-anchor="end" font-family="{mono}" font-size="9" letter-spacing="1.5" fill="{muted}">PUBLIC + PRIVATE · REFRESHED EVERY 30 MIN · {updated}</text>
  </g>
  <line x1="36" y1="66" x2="{W - 36}" y2="66" stroke="{t['hairline']}"/>
  {row_svg}
  <line x1="36" y1="232" x2="{W - 36}" y2="232" stroke="{t['hairline']}"/>
  <g>
    {lang_parts}
    {legend_parts}
    <text x="{W - 36}" y="{by0 + 29.5}" text-anchor="end" font-family="{mono}" font-size="9" letter-spacing="1" fill="{muted}">BY SOURCE BYTES · MARKUP EXCLUDED</text>
  </g>"""
    return svg_shell("activity", t, H, ["serif", "mono", "monobold"], body,
                     f"Engineering ledger {m['year']}: {fmt(m['commits_y'])} commits, "
                     f"{fmt(m['lines_y'])} lines of code changed, {fmt(m['prs_merged_y'])} "
                     f"pull requests merged, {fmt(m['total_all_time'])} contributions all-time, "
                     f"with language mix across public and private repositories")


def card_production(m: dict, t: dict) -> str:
    H = 300
    mono = FONT_FAMILIES["mono"][0]
    serif = FONT_FAMILIES["serif"][0]
    ink, muted = t["ink"], t["muted"]
    p = m["platform"]

    rows = [
        (f"EDGE REQUESTS · VERCEL · {p.get('vercel_window', '')}".rstrip(" ·"),
         fmt(int(p["vercel_requests"])) if p.get("vercel_requests") else "—"),
        ("API REQUESTS · SUPABASE · LAST 24 HOURS",
         fmt(int(p["supabase_requests_24h"])) if p.get("supabase_requests_24h") else "—"),
        ("AI DETECTION CHECKS PROCESSED · ALL-TIME",
         fmt(int(p["ai_checks_all_time"])) if p.get("ai_checks_all_time") else "—"),
        ("DISCORD COMMUNITY MEMBERS · LIVE",
         fmt(int(m["discord"])) if m["discord"] else "—"),
        ("REGISTERED USERS",
         fmt(int(p["registered_users"])) if p.get("registered_users") else "—"),
    ]
    row_svg = "".join(ledger_row(lbl, val, 104 + i * 35, t) for i, (lbl, val) in enumerate(rows))
    as_of = datetime.fromisoformat(p["as_of"]).strftime("%b %d").upper() if p.get("as_of") else "—"
    live_names = " · ".join(["DISCORD"] + p.get("live_fields", []))
    footnote = f"LIVE AT EACH RENDER: {live_names} — OTHER FIGURES AS OF {as_of}"

    body = f"""
  <rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="13" fill="url(#corner)"/>
  <g>
    <text x="34" y="49" font-family="{serif}" font-style="italic" font-size="25" fill="{ink}">Production</text>
    {live_dot(180, 44.5, t)}
    <text x="192" y="47" font-family="{mono}" font-size="9" letter-spacing="1.5" fill="{muted}">PULLED FROM LIVE SYSTEMS</text>
    <text x="{W - 36}" y="47" text-anchor="end" font-family="{mono}" font-size="9" letter-spacing="1.5" fill="{muted}">WHOLE NUMBERS · NO ESTIMATES</text>
  </g>
  <line x1="36" y1="66" x2="{W - 36}" y2="66" stroke="{t['hairline']}"/>
  {row_svg}
  <g>
    <text x="36" y="{H - 28}" font-family="{mono}" font-size="9" letter-spacing="1.2" fill="{muted}">{footnote}</text>
  </g>"""
    return svg_shell("production", t, H, ["serif", "mono", "monobold"], body,
                     "Production stats: tens of millions of edge requests, millions of "
                     "database requests per day, AI detection checks processed, Discord "
                     "community size, and registered users")


def card_product(m: dict, t: dict) -> str:
    H = 196
    mono, monob = FONT_FAMILIES["mono"][0], FONT_FAMILIES["monobold"][0]
    serif = FONT_FAMILIES["serif"][0]
    ink, sub, muted, accent = t["ink"], t["sub"], t["muted"], t["accent"]

    platform_line = f"{m['platform'].get('locales', 21)} LOCALES · EST 2024"

    chips = ["NEXT.JS", "TYPESCRIPT", "SUPABASE", "STRIPE", "OPENAI", "MODAL"]
    chip_svg, cx = "", 36.0
    for c in chips:
        cw = mono_w(c, 9, 1.2) + 20
        chip_svg += (
            f'<rect x="{cx:.1f}" y="140" width="{cw:.1f}" height="24" rx="6" '
            f'fill="{t["faint"]}" stroke="{t["hairline"]}"/>'
            f'<text x="{cx + cw / 2:.1f}" y="155.5" text-anchor="middle" font-family="{mono}" '
            f'font-size="9" letter-spacing="1.2" fill="{sub}">{c}</text>'
        )
        cx += cw + 10

    cta = "STUDYSOLUTIONS.APP"
    cta_w = mono_w(cta, 11, 1.4) + 46
    cta_x = W - 36 - cta_w

    body = f"""
  <rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="13" fill="url(#corner)"/>
  <g>
    {live_dot(42, 44.5, t)}
    <text x="56" y="48" font-family="{mono}" font-size="9.5" letter-spacing="1.8" fill="{t['good']}">IN PRODUCTION</text>
    <text x="{W - 36}" y="48" text-anchor="end" font-family="{mono}" font-size="9" letter-spacing="1.5" fill="{muted}">FLAGSHIP PRODUCT</text>
  </g>
  <g>
    <text x="34" y="97" font-family="{serif}" font-style="italic" font-size="40" fill="{ink}">StudySolutions</text>
    <text x="36" y="124" font-family="{mono}" font-size="10.5" letter-spacing="0.4" fill="{sub}">AI-powered study platform — document unlocks, AI-detection reports, humanization.</text>
    <text x="{W - 36}" y="124" text-anchor="end" font-family="{mono}" font-size="10" letter-spacing="0.8" fill="{t['accent']}">{platform_line}</text>
  </g>
  <g>
    {chip_svg}
    <rect x="{cta_x:.1f}" y="138" width="{cta_w:.1f}" height="28" rx="7" fill="{t['accent_soft']}" stroke="{t['accent_border']}"/>
    <text x="{cta_x + 18:.1f}" y="156" font-family="{monob}" font-weight="600" font-size="11" letter-spacing="1.4" fill="{accent}">{cta}</text>
    <path d="M{cta_x + cta_w - 20:.1f} 156 l7 -7 m0 0 h-6 m6 0 v6" stroke="{accent}" stroke-width="1.4" fill="none" stroke-linecap="round"/>
  </g>"""
    return svg_shell("studysolutions", t, H, ["serif", "mono", "monobold"], body,
                     "StudySolutions — AI-powered study platform, live at studysolutions.app",
                     accent_border=True)


def card_project(name: str, desc_lines: list[str], langs: list[tuple[str, str]],
                 note: str, t: dict) -> str:
    HW, H = 433, 150
    mono, monob = FONT_FAMILIES["mono"][0], FONT_FAMILIES["monobold"][0]
    ink, sub, muted = t["ink"], t["sub"], t["muted"]

    desc = "".join(
        f'<text x="30" y="{78 + i * 17}" font-family="{mono}" font-size="10" letter-spacing="0.2" fill="{sub}">{esc(line)}</text>'
        for i, line in enumerate(desc_lines)
    )
    lx = 30.0
    lang_svg = ""
    for lname, color in langs:
        lang_svg += (
            f'<circle cx="{lx + 3:.1f}" cy="{H - 29}" r="3" fill="{color}"/>'
            f'<text x="{lx + 11:.1f}" y="{H - 25.5}" font-family="{mono}" font-size="9" letter-spacing="1" fill="{muted}">{lname.upper()}</text>'
        )
        lx += 11 + mono_w(lname, 9, 1) + 12

    body = f"""
  <g>
    <text x="30" y="44" font-family="{monob}" font-weight="600" font-size="15" letter-spacing="0.2" fill="{ink}">{esc(name)}</text>
    <text x="{HW - 30}" y="43" text-anchor="end" font-family="{mono}" font-size="8.5" letter-spacing="1.4" fill="{muted}">{note}</text>
    <path d="M{HW - 40} 60 l0 0" stroke="none"/>
  </g>
  <g>{desc}</g>
  <g>{lang_svg}
    <text x="{HW - 30}" y="{H - 25.5}" text-anchor="end" font-family="{mono}" font-size="9" letter-spacing="1" fill="{t['accent']}">VIEW REPO →</text>
  </g>"""
    # narrower canvas: reuse shell but override width via direct construction
    svg = svg_shell("project", t, H, ["mono", "monobold"], body, f"{name} — public project")
    return svg.replace(f'width="{W}" height="{H}"', f'width="{HW}" height="{H}"') \
              .replace(f'viewBox="0 0 {W} {H}"', f'viewBox="0 0 {HW} {H}"') \
              .replace(f'x="0.5" y="0.5" width="{W - 1}"', f'x="0.5" y="0.5" width="{HW - 1}"')


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

FEATURED = [
    {
        "file": "meet2code",
        "name": "meet2code",
        "desc": ["Agentic meeting-to-PR pipeline — live transcription", "becomes issues, plans, code, and pull requests."],
        "langs": [("Python", "#3572A5"), ("AssemblyAI", "#4f68f0"),
                  ("Claude Code", "#d97757"), ("GitHub", "#8b949e")],
        "note": "HACKATHON BUILD · 2026",
    },
    {
        "file": "openstreet",
        "name": "OpenStreet",
        "desc": ["Civic data platform — one transparent view of local", "data for residents and city governments."],
        "langs": [("TypeScript", "#3178c6"), ("OpenAI", "#10a37f"),
                  ("Mapbox", "#4264fb"), ("Auth0", "#eb5424")],
        "note": "HACKATHON BUILD · 2025",
    },
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", help="path to a JSON cache of the raw fetch")
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    raw = None
    if args.cache and Path(args.cache).exists() and not args.refetch:
        raw = json.loads(Path(args.cache).read_text())
    if raw is None:
        token = os.environ.get("PROFILE_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not token:
            sys.exit("PROFILE_TOKEN (or GH_TOKEN) is required")
        raw = fetch(token)
        if args.cache:
            Path(args.cache).write_text(json.dumps(raw))

    m = compute(raw)
    ASSETS.mkdir(exist_ok=True)
    for theme in (DARK, LIGHT):
        (ASSETS / f"activity-{theme['id']}.svg").write_text(card_activity(m, theme))
        (ASSETS / f"production-{theme['id']}.svg").write_text(card_production(m, theme))
        (ASSETS / f"studysolutions-{theme['id']}.svg").write_text(card_product(m, theme))
        for proj in FEATURED:
            (ASSETS / f"{proj['file']}-{theme['id']}.svg").write_text(
                card_project(proj["name"], proj["desc"], proj["langs"], proj["note"], theme))
    print(f"Rendered {2 * (3 + len(FEATURED))} cards. "
          f"{fmt(m['total_all_time'])} contributions all-time, streak {m['streak']}.")


if __name__ == "__main__":
    main()
