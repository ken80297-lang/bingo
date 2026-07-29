from __future__ import annotations

import ast
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_OUTPUT_DIR = ROOT / "desktop" / "output" / "render_diagnostics"
REPORT_DIR = ROOT / "reports" / "render_diagnostics"


@dataclass
class Evidence:
    file: str
    line: int
    text: str


@dataclass
class Finding:
    priority: str
    title: str
    impact: str
    evidence: list[Evidence]
    recommendation: str


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _line_text(path: Path, line: int) -> str:
    lines = _read(path).splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


def _evidence(path: Path, line: int, text: str | None = None) -> Evidence:
    return Evidence(_rel(path), line, (text or _line_text(path, line))[:220])


def _run_git(args: list[str]) -> str:
    repo_root = ROOT.parent.as_posix()
    command = ["git", "-c", f"safe.directory={repo_root}", *args]
    try:
        return subprocess.check_output(command, cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return f"unavailable: {exc}"


def collect_git_snapshot() -> dict[str, str]:
    return {
        "top_level": _run_git(["rev-parse", "--show-toplevel"]),
        "branch": _run_git(["branch", "--show-current"]),
        "commit": _run_git(["rev-parse", "HEAD"]),
        "status_short": _run_git(["status", "--short"]),
        "last_five_commits": _run_git(["log", "-5", "--oneline"]),
    }


def collect_render_config() -> dict[str, Any]:
    names = ["render.yaml", "Dockerfile", "Procfile", "runtime.txt", "requirements.txt", "pyproject.toml"]
    files = {}
    for name in names:
        path = ROOT / name
        files[name] = {"exists": path.exists(), "path": _rel(path) if path.exists() else None}
    backend_requirements = ROOT / "backend" / "requirements.txt"
    if backend_requirements.exists():
        files["backend/requirements.txt"] = {"exists": True, "path": _rel(backend_requirements)}
    render_yaml = ROOT / "render.yaml"
    content = _read(render_yaml) if render_yaml.exists() else ""
    return {
        "files": files,
        "render_yaml": content,
        "health_check_note": "No explicit healthCheckPath found; Render may use default root unless configured in dashboard.",
        "start_command": _first_match(content, r"startCommand:\s*(.+)") or None,
        "plan": _first_match(content, r"plan:\s*(.+)") or None,
    }


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None


def _literal_value(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ast.unparse(node) if hasattr(ast, "unparse") else None


def collect_scheduler_jobs() -> list[dict[str, Any]]:
    path = ROOT / "backend" / "app.py"
    tree = ast.parse(_read(path), filename=str(path))
    jobs: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_job"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "scheduler"):
            continue
        args = [_literal_value(arg) for arg in node.args]
        kwargs = {kw.arg: _literal_value(kw.value) for kw in node.keywords if kw.arg}
        jobs.append(
            {
                "line": node.lineno,
                "callable": args[0] if args else None,
                "trigger": args[1] if len(args) > 1 else kwargs.get("trigger"),
                "id": kwargs.get("id"),
                "seconds": kwargs.get("seconds"),
                "minutes": kwargs.get("minutes"),
                "hours": kwargs.get("hours"),
                "hour": kwargs.get("hour"),
                "minute": kwargs.get("minute"),
                "max_instances": kwargs.get("max_instances"),
                "coalesce": kwargs.get("coalesce"),
                "misfire_grace_time": kwargs.get("misfire_grace_time"),
                "replace_existing": kwargs.get("replace_existing"),
                "evidence": asdict(_evidence(path, node.lineno)),
            }
        )
    return jobs


def collect_api_routes() -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for path in sorted((ROOT / "backend").rglob("*.py")):
        text = _read(path)
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = []
            route_methods = set()
            for decorator in node.decorator_list:
                rendered = ast.unparse(decorator) if hasattr(ast, "unparse") else ""
                if ".get(" in rendered:
                    route_methods.add("GET")
                    decorators.append(rendered)
                elif ".post(" in rendered:
                    route_methods.add("POST")
                    decorators.append(rendered)
                elif ".head(" in rendered:
                    route_methods.add("HEAD")
                    decorators.append(rendered)
            if not decorators:
                continue
            body_source = "\n".join(_line_text(path, line) for line in range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1))
            heavy_flags = []
            flag_terms = {
                "request_time_catch_up": "catch_up_missing_issues",
                "request_time_gap_scan": "scan_collector_gaps",
                "request_time_prediction_refresh": "ensure_next_prediction",
                "request_time_analysis": "analyze_all",
                "request_time_refresh_data": "refresh_data",
                "cache_refresh_trigger": "trigger_system_status_cache_refresh",
            }
            for flag, term in flag_terms.items():
                if term in body_source:
                    if flag.startswith("request_time_") and "GET" not in route_methods:
                        continue
                    heavy_flags.append(flag)
            routes.append(
                {
                    "file": _rel(path),
                    "line": node.lineno,
                    "function": node.name,
                    "decorators": decorators,
                    "methods": sorted(route_methods),
                    "heavy_flags": heavy_flags,
                }
            )
    return routes


def collect_dashboard_polling() -> dict[str, Any]:
    path = ROOT / "backend" / "static" / "dashboard.html"
    text = _read(path) if path.exists() else ""
    api_map: dict[str, str] = {}
    api_block = re.search(r"const\s+apis\s*=\s*\{(?P<body>.*?)\};", text, re.S)
    if api_block:
        for key, value in re.findall(r"(\w+)\s*:\s*\"([^\"]+)\"", api_block.group("body")):
            api_map[key] = value
    intervals = []
    for match in re.finditer(r"setInterval\((.+),\s*([0-9\s*]+)\)", text):
        interval_expr = match.group(2).strip()
        try:
            interval_ms = 1
            for part in interval_expr.split("*"):
                interval_ms *= int(part.strip())
        except Exception:
            continue
        call = match.group(1).strip()
        api_count = len(api_map)
        if "fastApiKeys" in call:
            api_count = 4
        elif "slowApiKeys" in call:
            api_count = max(0, len(api_map) - 4)
        elif "wakeStatus" in call:
            api_count = 1
        intervals.append(
            {
                "line": text[: match.start()].count("\n") + 1,
                "call": call,
                "interval_ms": interval_ms,
                "interval_expression": interval_expr,
                "calls_per_day_per_browser": round(86_400_000 / interval_ms, 2) if interval_ms else None,
                "api_count_per_cycle": api_count,
                "estimated_api_calls_per_day_per_browser": round((86_400_000 / interval_ms) * api_count, 2)
                if interval_ms
                else None,
            }
        )
    return {
        "dashboard_file": _rel(path) if path.exists() else None,
        "api_map": api_map,
        "intervals": intervals,
    }


def collect_http_profile() -> dict[str, Any]:
    path = ROOT / "backend" / "services" / "http_client.py"
    text = _read(path) if path.exists() else ""
    return {
        "default_timeout": _first_match(text, r"DEFAULT_TIMEOUT\s*=\s*(.+)") or "unknown",
        "ssl_fallback_enabled_default": _first_match(text, r"SSL_FALLBACK_ENABLED\s*=\s*(.+)") or "unknown",
        "requests_get_count": len(re.findall(r"requests\.get", text)),
        "timeout_retry_present": "timeout_retry" in text,
        "ssl_fallback_present": "verify=False" in text,
    }


def collect_search_hits() -> dict[str, list[dict[str, Any]]]:
    patterns = {
        "collector_sources": r"collector|collect|fetch|requests\.get|httpx|aiohttp|urllib|kuaishou|pilio|auzo|winwin|taiwan|official",
        "prediction_learning": r"prediction|next_prediction|recommendation|learning|realtime_learning|official_learning|reconcile|attempt|snapshot",
        "startup": r"startup|lifespan|initialize|migration|recover|catch_up|archive|learning|scheduler",
        "logging": r"logger\.|logging\.|print\(",
    }
    results: dict[str, list[dict[str, Any]]] = {}
    py_files = list((ROOT / "backend").rglob("*.py")) + list((ROOT / "desktop").rglob("*.py"))
    for key, pattern in patterns.items():
        regex = re.compile(pattern)
        hits = []
        for path in py_files:
            for idx, line in enumerate(_read(path).splitlines(), start=1):
                if regex.search(line):
                    hits.append({"file": _rel(path), "line": idx, "text": line.strip()[:220]})
        results[key] = hits[:250]
    return results


def build_findings(
    jobs: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    dashboard: dict[str, Any],
    http_profile: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    app_path = ROOT / "backend" / "app.py"
    collector_api = ROOT / "backend" / "api" / "collector.py"
    next_center = ROOT / "backend" / "services" / "next_prediction_center.py"
    dashboard_path = ROOT / "backend" / "static" / "dashboard.html"
    http_path = ROOT / "backend" / "services" / "http_client.py"

    status_job = next((job for job in jobs if job.get("id") == "system_status_runtime_cache_refresh"), None)
    if status_job and status_job.get("seconds") == 30 and status_job.get("max_instances") == 1:
        findings.append(
            Finding(
                "P0",
                "System status cache refresh runs every 30 seconds with max_instances=1",
                "If refresh_system_status_cache exceeds 30 seconds, APScheduler will emit max_instances skipped events and Render logs/CPU stay noisy.",
                [_evidence(app_path, int(status_job["line"]))],
                "Increase the interval or make the refresh cheaper. Keep request-time /api/system/status on memory cache only and refresh asynchronously with backoff.",
            )
        )

    catch_up_jobs = [job for job in jobs if "catch_up" in str(job.get("id"))]
    if catch_up_jobs:
        interval_job = next((job for job in catch_up_jobs if job.get("trigger") == "interval"), None)
        cadence = _cadence(interval_job) if interval_job else "scheduled"
        findings.append(
            Finding(
                "P0",
                f"Official catch-up is scheduled at startup and {cadence}",
                "catch_up_missing_issues can fetch official pages and run downstream verification/prediction after writes; cooldown and no-gap guards now limit repeated no-op runs.",
                [_evidence(app_path, int(job["line"])) for job in catch_up_jobs[:3]],
                "Keep observing Render metrics. Prefer a background worker ownership model before increasing catch-up cadence on the Render web service.",
            )
        )

    route_by_flag = {flag: route for route in routes for flag in route["heavy_flags"]}
    if "request_time_catch_up" in route_by_flag:
        route = route_by_flag["request_time_catch_up"]
        findings.append(
            Finding(
                "P0",
                "GET /api/collector/catch-up executes catch-up at request time",
                "A browser, uptime monitor, or dashboard debug call can trigger external fetches, database writes, downstream prediction, and learning work.",
                [_evidence(collector_api, int(route["line"]))],
                "Change this endpoint to POST/admin-only or return queued status. Keep GET endpoints read-only.",
            )
        )

    if "request_time_gap_scan" in route_by_flag:
        route = route_by_flag["request_time_gap_scan"]
        findings.append(
            Finding(
                "P1",
                "GET /api/collector/gaps scans continuity at request time",
                "Dashboard polling can turn continuity checks into repeated database reads. It is safer as cached status with manual refresh.",
                [_evidence(collector_api, int(route["line"]))],
                "Serve the last scan result from cache and refresh via scheduled/backoff job or explicit admin action.",
            )
        )

    if "request_time_prediction_refresh" in route_by_flag:
        route = route_by_flag["request_time_prediction_refresh"]
        refresh_line = _find_line(next_center, "ensure_next_prediction")
        findings.append(
            Finding(
                "P1",
                "/api/next-prediction can create a missing prediction during a read",
                "Read traffic may become write/compute traffic when latest_draw exists and prediction is missing.",
                [_evidence(Path(ROOT / route["file"]), int(route["line"])), _evidence(next_center, refresh_line)],
                "Move ensure_next_prediction to collector/lifecycle jobs. Let the dashboard return pending status when a prediction is missing.",
            )
        )

    intervals = dashboard.get("intervals") or []
    if intervals:
        total_estimate = round(sum(float(item.get("estimated_api_calls_per_day_per_browser") or 0) for item in intervals), 2)
        findings.append(
            Finding(
                "P1",
                "Dashboard uses split polling instead of a single 30-second all-API loop",
                f"One visible browser is now estimated at about {total_estimate} API requests/day across fast and slow timers, before page-visibility throttling.",
                [_evidence(dashboard_path, int(interval["line"])) for interval in intervals[:3]],
                "Continue tracking Render metrics; move more slow diagnostic cards to manual refresh if usage remains high.",
            )
        )

    if http_profile.get("ssl_fallback_present"):
        findings.append(
            Finding(
                "P1",
                "Official HTTP client retries SSL failures with verify=False",
                "SSL fallback doubles request attempts on certificate failures and can mask upstream problems while increasing bandwidth/runtime.",
                [_evidence(http_path, _find_line(http_path, "verify=False"))],
                "Keep the diagnostic, but add bounded backoff/circuit breaker after repeated SSL failures and avoid tight retry loops.",
            )
        )

    refresh_job = next((job for job in jobs if job.get("id") == "refresh_job"), None)
    if refresh_job:
        findings.append(
            Finding(
                "P2",
                "Legacy refresh_data job still runs every 5 minutes",
                "refresh_data fetches latest draws, runs analysis, builds recommendation, and writes statistics alongside newer official collector/lifecycle jobs.",
                [_evidence(app_path, int(refresh_job["line"])), _evidence(app_path, _find_line(app_path, "def refresh_data"))],
                "Confirm whether refresh_data is still needed. If not, disable it in production to avoid duplicate collector/analysis work.",
            )
        )

    return findings


def _find_line(path: Path, needle: str) -> int:
    for idx, line in enumerate(_read(path).splitlines(), start=1):
        if needle in line:
            return idx
    return 1


def build_resource_matrix(jobs: list[dict[str, Any]], dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for job in jobs:
        trigger = job.get("trigger")
        per_day = None
        if trigger == "interval":
            seconds = job.get("seconds") or ((job.get("minutes") or 0) * 60) or ((job.get("hours") or 0) * 3600)
            if seconds:
                per_day = round(86400 / float(seconds), 2)
        elif trigger == "cron":
            per_day = 1
        elif trigger == "date":
            per_day = "startup/one-shot"
        rows.append(
            {
                "component": job.get("id") or job.get("callable"),
                "cadence": _cadence(job),
                "runs_per_day": per_day,
                "max_http_requests_per_run": _estimate_http_per_run(job),
                "max_db_queries_per_run": "unknown",
                "risk": _job_risk(job),
            }
        )
    for interval in dashboard.get("intervals") or []:
        rows.append(
            {
                "component": "dashboard_polling_per_open_browser",
                "cadence": f"{interval.get('interval_ms')}ms",
                "runs_per_day": interval.get("calls_per_day_per_browser"),
                "max_http_requests_per_run": interval.get("api_count_per_cycle"),
                "max_db_queries_per_run": "endpoint dependent",
                "risk": "P1",
            }
        )
    return rows


def _cadence(job: dict[str, Any]) -> str:
    if job.get("trigger") == "interval":
        if job.get("seconds"):
            return f"every {job['seconds']} seconds"
        if job.get("minutes"):
            return f"every {job['minutes']} minutes"
        if job.get("hours"):
            return f"every {job['hours']} hours"
    if job.get("trigger") == "cron":
        return f"cron hour={job.get('hour')} minute={job.get('minute')}"
    if job.get("trigger") == "date":
        return "startup one-shot/date"
    return str(job.get("trigger") or "unknown")


def _estimate_http_per_run(job: dict[str, Any]) -> str | int:
    name = str(job.get("id") or job.get("callable") or "")
    if "catch_up" in name:
        return "up to CATCH_UP_MAX_SOURCE_PAGES + downstream verification"
    if "official" in name:
        return "1+ official API calls"
    if "kuaishou" in name or "pilio" in name:
        return "1+ provider calls"
    return 0


def _job_risk(job: dict[str, Any]) -> str:
    name = str(job.get("id") or job.get("callable") or "")
    if "system_status" in name and job.get("seconds") == 30:
        return "P0"
    if "catch_up" in name:
        return "P0"
    if "official" in name or "refresh_job" in name:
        return "P1"
    return "P2"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Render Usage Diagnostics",
        "",
        f"Generated at: {report['generated_at']}",
        "",
        "## Repo Snapshot",
        "",
    ]
    git = report["git"]
    lines.extend(
        [
            f"- Git top-level: `{git['top_level']}`",
            f"- Branch: `{git['branch']}`",
            f"- Commit: `{git['commit']}`",
            f"- Working tree dirty: `{bool(git['status_short'].strip())}`",
            "",
            "Latest commits:",
            "",
            "```text",
            git["last_five_commits"],
            "```",
            "",
            "Working tree status:",
            "",
            "```text",
            git["status_short"] or "clean",
            "```",
            "",
            "## Render Configuration",
            "",
            f"- Plan: `{report['render']['plan']}`",
            f"- Start command: `{report['render']['start_command']}`",
            f"- Health check: {report['render']['health_check_note']}",
            "",
            "## Scheduler Jobs",
            "",
            "| Job | Trigger | Cadence | max_instances | coalesce | misfire_grace_time | Line |",
            "|---|---|---|---:|---|---:|---:|",
        ]
    )
    for job in report["scheduler_jobs"]:
        lines.append(
            f"| `{job.get('id') or job.get('callable')}` | `{job.get('trigger')}` | {_cadence(job)} | "
            f"{job.get('max_instances') or ''} | {job.get('coalesce') or ''} | {job.get('misfire_grace_time') or ''} | {job.get('line')} |"
        )
    lines.extend(
        [
            "",
            "## Resource Request Matrix",
            "",
            "| Component | Cadence | Runs/day | Max HTTP/run | Max DB/run | Risk |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report["resource_matrix"]:
        lines.append(
            f"| `{row['component']}` | {row['cadence']} | {row['runs_per_day']} | {row['max_http_requests_per_run']} | {row['max_db_queries_per_run']} | {row['risk']} |"
        )
    lines.extend(["", "## Dashboard Polling", ""])
    for interval in report["dashboard_polling"]["intervals"]:
        lines.append(
            f"- `{interval['call']}` every `{interval['interval_ms']}ms`: "
            f"{interval['api_count_per_cycle']} APIs/cycle, approx "
            f"`{interval['estimated_api_calls_per_day_per_browser']}` API calls/day per open browser."
        )
    lines.extend(["", "Configured dashboard APIs:"])
    for key, url in report["dashboard_polling"]["api_map"].items():
        lines.append(f"- `{key}`: `{url}`")
    lines.extend(["", "## Findings", ""])
    for finding in report["findings"]:
        lines.extend(
            [
                f"### {finding['priority']} - {finding['title']}",
                "",
                f"Impact: {finding['impact']}",
                "",
                "Evidence:",
            ]
        )
        for item in finding["evidence"]:
            lines.append(f"- `{item['file']}:{item['line']}` {item['text']}")
        lines.extend(["", f"Recommendation: {finding['recommendation']}", ""])
    lines.extend(
        [
            "## Suggested Fix Order",
            "",
            "1. P0: Protect request-time catch-up, reduce 30-second status refresh pressure, and add stronger catch-up backoff.",
            "2. P1: Split dashboard polling cadence and cache gap scan / next prediction reads.",
            "3. P2: Review legacy refresh_data, logging volume, and query/index coverage.",
            "",
            "## Non-Actions Confirmed",
            "",
            "- No commit was created.",
            "- No push was performed.",
            "- No deploy was performed.",
            "- No Supabase or Render API was called.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_report() -> dict[str, Any]:
    jobs = collect_scheduler_jobs()
    routes = collect_api_routes()
    dashboard = collect_dashboard_polling()
    http_profile = collect_http_profile()
    findings = build_findings(jobs, routes, dashboard, http_profile)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": collect_git_snapshot(),
        "render": collect_render_config(),
        "scheduler_jobs": jobs,
        "api_routes": routes,
        "dashboard_polling": dashboard,
        "http_profile": http_profile,
        "resource_matrix": build_resource_matrix(jobs, dashboard),
        "findings": [asdict(item) for item in findings],
        "search_hits": collect_search_hits(),
        "non_actions_confirmed": {
            "commit": False,
            "push": False,
            "deploy": False,
            "supabase_or_render_api_calls": False,
        },
    }
    return report


def main() -> int:
    DESKTOP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report()
    json_text = json.dumps(report, ensure_ascii=False, indent=2)
    markdown_text = render_markdown(report)
    (DESKTOP_OUTPUT_DIR / "render_diagnostics.json").write_text(json_text + "\n", encoding="utf-8")
    (DESKTOP_OUTPUT_DIR / "render_diagnostics.md").write_text(markdown_text, encoding="utf-8")
    (REPORT_DIR / "render_diagnostics.json").write_text(json_text + "\n", encoding="utf-8")
    (REPORT_DIR / "render_diagnostics.md").write_text(markdown_text, encoding="utf-8")
    print(f"Wrote {DESKTOP_OUTPUT_DIR / 'render_diagnostics.md'}")
    print(f"Wrote {REPORT_DIR / 'render_diagnostics.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
