#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from typing import Dict, Iterable, List

API_BASE = "https://api.github.com"
DEFAULT_USERNAME = "jingyi-zhao-01"


def iso_z(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_shift(dt: datetime, months: int) -> datetime:
    year = dt.year
    month = dt.month + months
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    return dt.replace(year=year, month=month)


def month_labels_12(now: datetime) -> List[str]:
    start = month_shift(month_start(now), -11)
    return [month_shift(start, i).strftime("%Y-%m") for i in range(12)]


class GitHubClient:
    def __init__(self, token: str | None):
        self.token = token

    def _request(self, path: str, params: Dict[str, str] | None = None):
        url = f"{API_BASE}{path}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "profile-dashboard-generator",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            message = f"GitHub API error {exc.code} for {url}"
            if body:
                message = f"{message}: {body}"
            raise RuntimeError(message) from exc

    def paginate(self, path: str, params: Dict[str, str] | None = None) -> Iterable[dict]:
        page = 1
        while True:
            query = dict(params or {})
            query["per_page"] = "100"
            query["page"] = str(page)
            data = self._request(path, query)
            if not data:
                return
            if not isinstance(data, list):
                raise RuntimeError(f"Expected list response for {path}, got {type(data)}")
            for item in data:
                yield item
            if len(data) < 100:
                return
            page += 1



def aggregate_dashboard(username: str, client: GitHubClient) -> dict:
    now = datetime.now(timezone.utc)
    months = month_labels_12(now)
    since = month_shift(month_start(now), -11)

    monthly_add = defaultdict(int)
    monthly_del = defaultdict(int)
    language_totals = defaultdict(int)

    repos = [
        repo
        for repo in client.paginate(
            f"/users/{username}/repos",
            {"type": "owner", "sort": "updated", "direction": "desc"},
        )
        if not repo.get("fork") and not repo.get("archived") and not repo.get("disabled")
    ]

    for repo in repos:
        owner = repo.get("owner", {}).get("login")
        name = repo.get("name")
        if not owner or not name:
            continue

        try:
            langs = client._request(f"/repos/{owner}/{name}/languages")
        except RuntimeError as exc:
            print(f"[warn] skipping languages for {owner}/{name}: {exc}", file=sys.stderr)
            langs = {}

        if isinstance(langs, dict):
            for lang, value in langs.items():
                if isinstance(value, int):
                    language_totals[lang] += value

        try:
            commits = list(
                client.paginate(
                    f"/repos/{owner}/{name}/commits",
                    {
                        "author": username,
                        "since": iso_z(since),
                    },
                )
            )
        except RuntimeError as exc:
            print(f"[warn] skipping commits for {owner}/{name}: {exc}", file=sys.stderr)
            commits = []

        for commit in commits:
            sha = commit.get("sha")
            if not sha:
                continue

            try:
                detail = client._request(f"/repos/{owner}/{name}/commits/{sha}")
            except RuntimeError as exc:
                print(f"[warn] skipping commit {owner}/{name}@{sha[:7]}: {exc}", file=sys.stderr)
                continue

            stats = detail.get("stats") or {}
            additions = int(stats.get("additions") or 0)
            deletions = int(stats.get("deletions") or 0)

            authored_date = (
                (detail.get("commit") or {}).get("author") or {}
            ).get("date")
            if not authored_date:
                continue

            try:
                dt = datetime.fromisoformat(authored_date.replace("Z", "+00:00"))
            except ValueError:
                continue

            month = dt.strftime("%Y-%m")
            if month in months:
                monthly_add[month] += additions
                monthly_del[month] += deletions

    additions = [monthly_add[m] for m in months]
    deletions = [monthly_del[m] for m in months]
    net = [a - d for a, d in zip(additions, deletions)]

    language_totals_sorted = dict(
        sorted(language_totals.items(), key=lambda item: item[1], reverse=True)
    )

    return {
        "metadata": {
            "username": username,
            "generated_at": iso_z(now),
            "window_start": iso_z(since),
            "window_end": iso_z(now),
            "included_public_repositories": len(repos),
            "filters": {
                "repository_owner": username,
                "exclude_forks": True,
                "exclude_archived": True,
                "exclude_disabled": True,
                "commit_author": username,
            },
        },
        "loc": {
            "months": months,
            "additions": additions,
            "deletions": deletions,
            "net": net,
            "totals": {
                "additions": sum(additions),
                "deletions": sum(deletions),
                "net": sum(net),
            },
        },
        "languages": {
            "bytes": language_totals_sorted,
            "total_bytes": sum(language_totals_sorted.values()),
        },
    }



def render_loc_trend_svg(months: List[str], additions: List[int], deletions: List[int], net: List[int]) -> str:
    width = 1100
    height = 380
    left = 70
    right = 30
    top = 50
    bottom = 70
    chart_w = width - left - right
    chart_h = height - top - bottom

    series_min = min([0] + [-d for d in deletions] + net)
    series_max = max([1] + additions + net)
    span = series_max - series_min
    padding = max(1, int(span * 0.1))
    y_min = series_min - padding
    y_max = series_max + padding

    def y_scale(value: float) -> float:
        ratio = (value - y_min) / max(1, (y_max - y_min))
        return top + chart_h - (ratio * chart_h)

    def x_center(i: int) -> float:
        return left + (i + 0.5) * chart_w / max(1, len(months))

    bar_group_w = chart_w / max(1, len(months))
    bar_w = max(3, bar_group_w * 0.28)

    zero_y = y_scale(0)

    y_ticks = 5
    grid_lines = []
    for i in range(y_ticks + 1):
        value = y_min + (i * (y_max - y_min) / y_ticks)
        y = y_scale(value)
        grid_lines.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}" stroke="#30363d" stroke-width="1" />'
        )
        grid_lines.append(
            f'<text x="{left-10}" y="{y+4:.2f}" font-size="10" text-anchor="end" fill="#8b949e">{int(value)}</text>'
        )

    bars = []
    for i, (add, dele) in enumerate(zip(additions, deletions)):
        x = x_center(i)

        add_top = y_scale(add)
        add_h = abs(zero_y - add_top)
        bars.append(
            f'<rect x="{x - bar_w - 2:.2f}" y="{min(add_top, zero_y):.2f}" width="{bar_w:.2f}" height="{add_h:.2f}" fill="#3fb950" rx="2" />'
        )

        del_top = y_scale(-dele)
        del_h = abs(zero_y - del_top)
        bars.append(
            f'<rect x="{x + 2:.2f}" y="{min(del_top, zero_y):.2f}" width="{bar_w:.2f}" height="{del_h:.2f}" fill="#f85149" rx="2" />'
        )

    net_points = " ".join(f"{x_center(i):.2f},{y_scale(v):.2f}" for i, v in enumerate(net))
    month_labels = "".join(
        f'<text x="{x_center(i):.2f}" y="{height-22}" text-anchor="middle" font-size="10" fill="#8b949e">{escape(month)}</text>'
        for i, month in enumerate(months)
    )

    total_add = sum(additions)
    total_del = sum(deletions)
    total_net = sum(net)

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" role="img" aria-label="Last 12 months lines of code changes">
  <rect width="100%" height="100%" fill="#0d1117" />
  <text x="{width/2:.1f}" y="28" text-anchor="middle" fill="#c9d1d9" font-size="18" font-weight="600">Last 12 Months LOC Changes</text>
  <text x="{width/2:.1f}" y="45" text-anchor="middle" fill="#8b949e" font-size="11">Additions (+), Deletions (-), Net (line)</text>
  {''.join(grid_lines)}
  <line x1="{left}" y1="{zero_y:.2f}" x2="{width-right}" y2="{zero_y:.2f}" stroke="#6e7681" stroke-width="1.5" />
  {''.join(bars)}
  <polyline fill="none" stroke="#58a6ff" stroke-width="2.5" points="{net_points}" />
  {month_labels}
  <text x="{left}" y="{height-6}" fill="#3fb950" font-size="11">■ Additions: {total_add}</text>
  <text x="{left+170}" y="{height-6}" fill="#f85149" font-size="11">■ Deletions: {total_del}</text>
  <text x="{left+350}" y="{height-6}" fill="#58a6ff" font-size="11">— Net: {total_net}</text>
</svg>
'''



def render_language_breakdown_svg(language_bytes: Dict[str, int]) -> str:
    width = 1100
    height = 420
    left = 60
    top = 64
    row_h = 30
    row_gap = 14
    bar_w = 760
    palette = [
        "#58a6ff",
        "#3fb950",
        "#f85149",
        "#d29922",
        "#a371f7",
        "#ff7b72",
        "#7ee787",
        "#ffa657",
        "#79c0ff",
    ]

    total = sum(language_bytes.values())
    items = list(language_bytes.items())
    if len(items) > 8:
        top_items = items[:8]
        other = sum(v for _, v in items[8:])
        if other:
            top_items.append(("Other", other))
        items = top_items

    rows = []
    for idx, (lang, value) in enumerate(items):
        pct = (value / total * 100.0) if total else 0.0
        y = top + idx * (row_h + row_gap)
        fill_w = bar_w * (pct / 100.0)
        color = palette[idx % len(palette)]
        rows.append(
            f'<text x="{left}" y="{y-8}" font-size="12" fill="#c9d1d9">{escape(lang)}</text>'
            f'<rect x="{left}" y="{y}" width="{bar_w}" height="{row_h}" rx="6" fill="#21262d" />'
            f'<rect x="{left}" y="{y}" width="{fill_w:.2f}" height="{row_h}" rx="6" fill="{color}" />'
            f'<text x="{left + bar_w + 14}" y="{y + 20}" font-size="12" fill="#c9d1d9">{pct:.1f}%</text>'
        )

    if not items:
        rows.append(
            f'<text x="{left}" y="{top+20}" font-size="13" fill="#8b949e">No language data found for filtered repositories.</text>'
        )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" role="img" aria-label="Language breakdown across public repositories">
  <rect width="100%" height="100%" fill="#0d1117" />
  <text x="{width/2:.1f}" y="30" text-anchor="middle" fill="#c9d1d9" font-size="18" font-weight="600">Language Breakdown Across Public Repositories</text>
  <text x="{width/2:.1f}" y="48" text-anchor="middle" fill="#8b949e" font-size="11">Aggregated from repository language bytes</text>
  {''.join(rows)}
</svg>
'''



def write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)



def main() -> int:
    username = (
        os.getenv("DASHBOARD_USERNAME")
        or os.getenv("GITHUB_REPOSITORY_OWNER")
        or DEFAULT_USERNAME
    )
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")

    client = GitHubClient(token=token)
    try:
        dashboard = aggregate_dashboard(username, client)
    except RuntimeError as exc:
        now = datetime.now(timezone.utc)
        months = month_labels_12(now)
        dashboard = {
            "metadata": {
                "username": username,
                "generated_at": iso_z(now),
                "window_start": iso_z(month_shift(month_start(now), -11)),
                "window_end": iso_z(now),
                "included_public_repositories": 0,
                "warning": f"Data collection failed: {exc}",
                "filters": {
                    "repository_owner": username,
                    "exclude_forks": True,
                    "exclude_archived": True,
                    "exclude_disabled": True,
                    "commit_author": username,
                },
            },
            "loc": {
                "months": months,
                "additions": [0] * len(months),
                "deletions": [0] * len(months),
                "net": [0] * len(months),
                "totals": {"additions": 0, "deletions": 0, "net": 0},
            },
            "languages": {"bytes": {}, "total_bytes": 0},
        }
        print(f"[warn] dashboard data collection failed; writing empty fallback data: {exc}", file=sys.stderr)

    months = dashboard["loc"]["months"]
    additions = dashboard["loc"]["additions"]
    deletions = dashboard["loc"]["deletions"]
    net = dashboard["loc"]["net"]
    language_bytes = dashboard["languages"]["bytes"]

    loc_svg = render_loc_trend_svg(months, additions, deletions, net)
    lang_svg = render_language_breakdown_svg(language_bytes)

    write_file("assets/loc-trend.svg", loc_svg)
    write_file("assets/language-breakdown.svg", lang_svg)
    write_file("data/dashboard.json", json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n")

    print(
        f"Generated dashboard for {username}: "
        f"{dashboard['metadata']['included_public_repositories']} repos, "
        f"{dashboard['loc']['totals']['additions']} additions, "
        f"{dashboard['loc']['totals']['deletions']} deletions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
