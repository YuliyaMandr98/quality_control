"""Workflows UI (triage_bugs only)."""

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from apps.app.database import WorkflowRunModel
from apps.app.database import get_session_factory
from apps.app.config import get_settings
from apps.app.workflows import enqueue_workflow
from packages.common import BugBacklogAuditRunRequest
from packages.common import ReviewCommentFixesRunRequest
from packages.common import ReviewPullRequestRunRequest
from packages.common import ReviewTestCasesRunRequest
from packages.common import SkippedTestsAuditRunRequest
from packages.common import TriageBugTicketsRunRequest
from packages.common import UatBugTestCasesRunRequest
from packages.common import WorkflowType
from packages.workflows import bug_backlog_audit as bug_backlog_audit_workflow
from packages.workflows import review_test_cases as review_test_cases_workflow
from packages.workflows import skipped_tests as skipped_tests_workflow
from packages.workflows import uat_bug_test_cases as uat_bug_test_cases_workflow
from packages.workflows import upload_test_cases as upload_workflow

router = APIRouter()


def get_db(request: Request) -> Session:
    """Get database session."""
    settings = get_settings()
    SessionLocal = get_session_factory(settings.database_url)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _bool_from_form(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "on", "yes"}


def _render_triage_bugs_page(
    *,
    form_values: dict[str, str] | None = None,
    validation_error: str | None = None,
    run_id: str | None = None,
) -> str:
    default_jql = 'issuetype in ("BE BUG", "Mobile bug", Bug, "FE bug") AND status = Backlog'
    values = {
        "jql": default_jql,
        "max_results": "50",
        "apply": "",
        "add_comment": "",
        "severity_field_id": "customfield_10865",
        "impact_field_id": "customfield_10004",
        "target_status": "Triage",
        "batch_delay_seconds": "10",
    }
    if form_values:
        values.update(form_values)

    error_block = (
        f'<div class="error">Validation error: {validation_error}</div>'
        if validation_error
        else ""
    )

    run_panel = ""
    if run_id:
        run_panel = f"""
        <div class="result-card">
            <h3>Run Monitor</h3>
            <p class="run-id-row">Run ID: <code>{run_id}</code><button id="btnCancelRun" class="btn-cancel-run" onclick="cancelRun()">⛔ Остановить</button><span id="cancelStatus" class="hint"></span></p>
            <div id="dryRunBadge" class="badge">Pending...</div>
            <div id="runProgress" class="progress-line">Preparing monitor...</div>
            <div id="currentStep" class="step-line">Current step: waiting...</div>
            <div class="counters">
                <div>Fetched: <strong id="countFetched">0</strong></div>
                <div>Triaged: <strong id="countTriaged">0</strong></div>
                <div>Not Real Bug: <strong id="countNotReal">0</strong></div>
                <div>Errors: <strong id="countErrors">0</strong></div>
            </div>
            <h4>Status</h4>
            <pre id="runStatus">Loading...</pre>
            <h4>Terminal-Like Stream</h4>
            <div class="logs-toolbar">
                <span id="logMeta">Logs: 0</span>
                <label><input type="checkbox" id="autoScrollLogs" checked /> Auto-scroll</label>
            </div>
            <pre id="liveLogs">Loading logs...</pre>
            <h4>Per-Issue Results</h4>
            <div id="bulkApplyBar" style="display:none; margin-bottom:8px;">
                <button id="btnApplyAll" class="btn-apply-all" onclick="applyAll()">✅ Apply All Real Bugs to Jira</button>
                <label style="margin-left:12px; font-size:13px;"><input type="checkbox" id="addCommentApply" /> Add Jira triage comments</label>
                <span id="bulkApplyStatus" style="margin-left:12px; font-size:13px;"></span>
            </div>
            <div class="table-scroll">
            <table>
                <thead>
                    <tr><th>Key</th><th>Summary</th><th>Real Bug</th><th>Severity</th><th>Impact</th><th>Priority</th><th>Outcome</th><th>Reasoning</th><th id="applyColHeader"></th></tr>
                </thead>
                <tbody id="resultsTableBody"><tr><td colspan="9">Waiting for results...</td></tr></tbody>
            </table>
            </div>
            <h4>Artifacts</h4>
            <ul id="artifactLinks"><li>Waiting for artifacts...</li></ul>
        </div>
        <script>
            const runId = "{run_id}";

            async function cancelRun() {{
                if (!confirm("Остановить выполнение? Текущий шаг доработает, затем воркфлоу остановится.")) return;
                const btn = document.getElementById("btnCancelRun");
                const statusEl = document.getElementById("cancelStatus");
                btn.disabled = true;
                statusEl.textContent = "Остановка запрошена…";
                try {{
                    const resp = await fetch(`/api/runs/${{runId}}/cancel`, {{ method: "POST" }});
                    const data = await resp.json();
                    if (!resp.ok) {{
                        statusEl.textContent = `Ошибка: ${{data.detail || resp.status}}`;
                        btn.disabled = false;
                        return;
                    }}
                    statusEl.textContent = data.cancel_requested
                        ? "Остановка запрошена — ждём завершения текущего шага…"
                        : (data.message || "Запуск уже завершён.");
                    if (!data.cancel_requested) btn.style.display = "none";
                }} catch (e) {{
                    statusEl.textContent = "Не удалось отправить запрос на остановку.";
                    btn.disabled = false;
                }}
            }}
            let done = false;
            let isDryRun = true;
            let runFinished = false;
            let lastRows = [];
            const monitorStartedAt = Date.now();
            let lastLogTimestamp = null;
            const POLL_INTERVAL_MS = 1000;
            const ARTIFACT_POLL_EVERY_TICKS = 5;
            let tickCount = 0;

            function outcomeClass(outcome) {{
                if (outcome === 'triaged') return 'outcome-ok';
                if (outcome === 'dry_run') return 'outcome-dry';
                if (outcome === 'error' || outcome === 'partial_update') return 'outcome-err';
                return 'outcome-skip';
            }}

            function fmtElapsed(ms) {{
                const sec = Math.floor(ms / 1000);
                const min = Math.floor(sec / 60);
                const rem = sec % 60;
                return `${{min}}m ${{rem}}s`;
            }}

            async function refreshRun() {{
                const runResp = await fetch(`/api/runs/${{runId}}`);
                if (!runResp.ok) {{
                    const msg = `Failed to load run status (HTTP ${{runResp.status}})`;
                    document.getElementById("runStatus").textContent = msg;
                    document.getElementById("runProgress").textContent = msg;
                    return;
                }}
                const run = await runResp.json();
                document.getElementById("runStatus").textContent = JSON.stringify(run, null, 2);

                const elapsed = fmtElapsed(Date.now() - monitorStartedAt);
                const statusUpper = String(run.status || "unknown").toUpperCase();
                document.getElementById("runProgress").textContent = `Status: ${{statusUpper}} | Elapsed: ${{elapsed}}`;

                const dryRunBadge = document.getElementById("dryRunBadge");
                if (run.dry_run) {{
                    isDryRun = true;
                    dryRunBadge.textContent = "DRY-RUN (preview only — no Jira changes)";
                    dryRunBadge.className = "badge dry";
                }} else {{
                    isDryRun = false;
                    dryRunBadge.textContent = "APPLY MODE (writes enabled — Jira will be updated)";
                    dryRunBadge.className = "badge apply";
                }}

                const logsResp = await fetch(`/api/runs/${{runId}}/logs`);
                if (logsResp.ok) {{
                    const logsData = await logsResp.json();
                    const logs = logsData.logs || [];
                    const stripLogPrefix = (msg) => msg.replace(/^\[[\dT:.+\-Z]+\]\s*\[cid=[^\]]+\]\s*/, "");
                    const lines = logs.map((l) => `${{l.timestamp}} [${{l.level}}] ${{stripLogPrefix(l.message)}}`);
                    const logsEl = document.getElementById("liveLogs");
                    logsEl.textContent = lines.join("\\n") || "No logs yet";

                    if (logs.length > 0) {{
                        lastLogTimestamp = logs[logs.length - 1].timestamp;
                    }}
                    document.getElementById("logMeta").textContent =
                        `Logs: ${{logs.length}} | Last update: ${{lastLogTimestamp || "n/a"}}`;

                    const currentStepEl = document.getElementById("currentStep");
                    if (logs.length > 0) {{
                        const last = logs[logs.length - 1];
                        currentStepEl.textContent = `Current step: [${{last.level}}] ${{stripLogPrefix(last.message)}}`;
                    }} else {{
                        currentStepEl.textContent = "Current step: waiting for first log line...";
                    }}

                    if (document.getElementById("autoScrollLogs").checked) {{
                        logsEl.scrollTop = logsEl.scrollHeight;
                    }}

                    if (run.status === "running" || run.status === "queued") {{
                        const waitHint = logs.length === 0
                            ? "Waiting for worker logs..."
                            : "Workflow is running. Claude calls may take time; logs update as each issue is processed.";
                        document.getElementById("runProgress").textContent =
                            `Status: ${{statusUpper}} | Elapsed: ${{elapsed}} | ${{waitHint}}`;
                    }}
                }}

                const isTerminal = ["succeeded", "failed", "canceled"].includes(run.status);
                if (isTerminal) {{
                    runFinished = true;
                    document.getElementById("btnCancelRun").style.display = "none";
                }}

                const shouldPollArtifacts =
                    (tickCount % ARTIFACT_POLL_EVERY_TICKS === 0) ||
                    isTerminal;

                if (shouldPollArtifacts) {{
                    const artifactResp = await fetch(`/api/artifacts/run/${{runId}}`);
                    if (artifactResp.ok) {{
                        const artifacts = await artifactResp.json();
                        const linksEl = document.getElementById("artifactLinks");
                        linksEl.innerHTML = "";
                        for (const item of artifacts) {{
                            const li = document.createElement("li");
                            const a = document.createElement("a");
                            a.href = item.download_url;
                            a.textContent = item.filename;
                            li.appendChild(a);
                            linksEl.appendChild(li);
                        }}

                        const summaryArtifact = artifacts.find((a) => a.filename === "triage_summary.json");
                        if (summaryArtifact) {{
                            const summaryResp = await fetch(summaryArtifact.download_url);
                            if (summaryResp.ok) {{
                                const summary = await summaryResp.json();
                                document.getElementById("countFetched").textContent = summary.bugs_fetched ?? 0;
                                document.getElementById("countTriaged").textContent = summary.bugs_triaged ?? 0;
                                document.getElementById("countNotReal").textContent = summary.skipped_not_real_bug ?? 0;
                                document.getElementById("countErrors").textContent = summary.errors ?? 0;
                            }}
                        }}

                        const perIssueArtifact = artifacts.find((a) => a.filename === "per_issue_results.json");
                        if (perIssueArtifact) {{
                            const resultResp = await fetch(perIssueArtifact.download_url);
                            if (resultResp.ok) {{
                                const rows = await resultResp.json();
                                lastRows = rows;
                                const body = document.getElementById("resultsTableBody");
                                body.innerHTML = "";
                                const showApplyCol = isDryRun && runFinished;
                                document.getElementById("applyColHeader").textContent = showApplyCol ? "Apply" : "";
                                for (const row of rows) {{
                                    const tr = document.createElement("tr");
                                    tr.id = `row-${{row.key}}`;
                                    tr.className = outcomeClass(row.outcome);
                                    const isReal = row.is_real_bug === null ? "-" : row.is_real_bug ? "✅ Yes" : "❌ No";
                                    const sev = row.severity || "-";
                                    const imp = row.impact || "-";
                                    const pri = row.priority || "-";
                                    const applyCell = (showApplyCol && row.is_real_bug)
                                        ? `<td><button class="btn-apply-row" onclick="applyOne('${{row.key}}', this)">Apply</button></td>`
                                        : `<td></td>`;
                                    tr.innerHTML = `
                                        <td><a href="/ui/runs" target="_blank">${{row.key || ""}}</a></td>
                                        <td>${{row.summary || ""}}</td>
                                        <td>${{isReal}}</td>
                                        <td>${{sev}}</td>
                                        <td>${{imp}}</td>
                                        <td>${{pri}}</td>
                                        <td><span class="outcome-badge ${{row.outcome || ""}}">${{row.outcome || ""}}</span></td>
                                        <td>${{row.reasoning || row.reason || ""}}</td>
                                        ${{applyCell}}
                                    `;
                                    body.appendChild(tr);
                                }}
                                if (showApplyCol && rows.some(r => r.is_real_bug)) {{
                                    document.getElementById("bulkApplyBar").style.display = "";
                                }}
                            }}
                        }}
                    }}
                }}

                if (isTerminal) {{
                    done = true;
                }}
            }}

            async function _callApply(issueKeys) {{
                const statusEl = document.getElementById("bulkApplyStatus");
                const addComment = !!document.getElementById("addCommentApply")?.checked;
                const resp = await fetch("/api/workflows/triage-bugs/apply-issues", {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{ run_id: runId, issue_keys: issueKeys, add_comment: addComment }}),
                }});
                if (!resp.ok) {{
                    const err = await resp.text();
                    statusEl.textContent = `Error: ${{err}}`;
                    return null;
                }}
                return await resp.json();
            }}

            async function applyOne(issueKey, btn) {{
                const addComment = !!document.getElementById("addCommentApply")?.checked;
                const commentSuffix = addComment ? " and post a triage comment" : "";
                if (!confirm(`Apply triage to ${{issueKey}} in Jira${{commentSuffix}}?`)) return;
                btn.disabled = true;
                btn.textContent = "...";
                const result = await _callApply([issueKey]);
                if (!result) {{ btn.textContent = "Error"; return; }}
                if (result.applied && result.applied.includes(issueKey)) {{
                    btn.textContent = "✅ Applied";
                    btn.style.background = "#198754";
                }} else if (result.errors && result.errors.length) {{
                    btn.textContent = "❌ Failed";
                    btn.title = result.errors[0]?.error || "";
                    btn.style.background = "#dc3545";
                }}
            }}

            async function applyAll() {{
                const realBugKeys = lastRows.filter(r => r.is_real_bug).map(r => r.key);
                if (!realBugKeys.length) return;
                const addComment = !!document.getElementById("addCommentApply")?.checked;
                const commentSuffix = addComment ? " and add triage comments" : "";
                if (!confirm(`Apply triage to ${{realBugKeys.length}} real bug(s) in Jira? This will update severity, impact, priority and transition status${{commentSuffix}}.`)) return;
                const statusEl = document.getElementById("bulkApplyStatus");
                const allBtn = document.getElementById("btnApplyAll");
                allBtn.disabled = true;
                statusEl.textContent = "Applying...";
                const result = await _callApply(null);
                if (!result) {{ statusEl.textContent = "Request failed."; allBtn.disabled = false; return; }}
                statusEl.textContent = `✅ Applied ${{result.applied_count}} | ❌ Errors: ${{result.error_count}}`;
                document.querySelectorAll(".btn-apply-row").forEach(b => {{
                    const key = b.closest("tr")?.id?.replace("row-", "");
                    if (result.applied && result.applied.includes(key)) {{
                        b.textContent = "✅ Applied"; b.disabled = true; b.style.background = "#198754";
                    }} else if (result.errors && result.errors.some(e => e.key === key)) {{
                        b.textContent = "❌ Failed"; b.disabled = true; b.style.background = "#dc3545";
                    }}
                }});
            }}

            async function tick() {{
                tickCount += 1;
                try {{ await refreshRun(); }} catch (e) {{}}
                if (!done) setTimeout(tick, POLL_INTERVAL_MS);
            }}
            tick();
        </script>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Run Triage Bugs - Triage Bugs Tool (Claude)</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f5; }}
            nav {{ background: #0066cc; color: white; padding: 15px 30px; }}
            nav h1 {{ margin: 0; font-size: 24px; }}
            nav ul {{ list-style: none; display: flex; gap: 20px; margin-top: 10px; }}
            nav a {{ color: white; text-decoration: none; }}
            .container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
            h2 {{ color: #333; margin: 20px 0 10px 0; }}
            form, .result-card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 16px; }}
            .form-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
            .form-group {{ margin-bottom: 14px; }}
            label {{ display: block; font-weight: bold; margin-bottom: 6px; color: #333; }}
            input, select, textarea {{ width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; font-family: inherit; }}
            textarea {{ font-family: monospace; font-size: 13px; resize: vertical; }}
            .checkbox {{ display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }}
            .checkbox input {{ width: auto; }}
            .checkbox label {{ font-weight: normal; margin: 0; }}
            button {{ background: #0066cc; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }}
            button:hover {{ background: #0052a3; }}
            .error {{ background: #ffe7e7; color: #8b0000; border: 1px solid #f5baba; padding: 10px; margin-bottom: 12px; border-radius: 6px; }}
            .badge {{ display: inline-block; padding: 4px 8px; border-radius: 12px; font-weight: 600; margin: 8px 0; }}
            .badge.dry {{ background: #fff3cd; color: #664d03; }}
            .badge.apply {{ background: #d1e7dd; color: #0f5132; }}
            .progress-line {{ margin: 6px 0 12px 0; font-size: 14px; color: #333; }}
            .step-line {{ margin: 0 0 10px 0; font-size: 13px; color: #555; }}
            .logs-toolbar {{ display: flex; justify-content: space-between; align-items: center; margin: 6px 0; font-size: 13px; color: #444; }}
            .logs-toolbar label {{ font-weight: normal; margin: 0; }}
            .table-scroll {{ overflow-x: auto; }}
            table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
            th, td {{ padding: 8px; border-bottom: 1px solid #eee; text-align: left; font-size: 13px; word-wrap: break-word; overflow-wrap: anywhere; }}
            th {{ background: #f9f9f9; font-weight: bold; }}
            .counters {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 10px 0; }}
            .counters div {{ background: #f5f5f5; border-radius: 6px; padding: 10px; text-align: center; font-size: 13px; }}
            pre {{ background: #1e1e1e; color: #d4d4d4; border-radius: 4px; padding: 10px; overflow: auto; max-height: 260px; font-size: 12px; }}
            .outcome-badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }}
            .triaged td, tr.outcome-ok td {{ background: #f0fff4; }}
            tr.outcome-dry td {{ background: #fffbea; }}
            tr.outcome-err td {{ background: #fff5f5; }}
            tr.outcome-skip td {{ background: #fafafa; }}
            .outcome-badge.triaged {{ background: #d1e7dd; color: #0f5132; }}
            .outcome-badge.dry_run {{ background: #fff3cd; color: #664d03; }}
            .outcome-badge.error, .outcome-badge.partial_update {{ background: #f8d7da; color: #842029; }}
            .outcome-badge.skipped, .outcome-badge.not_real_bug {{ background: #e2e3e5; color: #41464b; }}
            h4 {{ margin: 16px 0 6px 0; color: #333; }}
            .hint {{ font-size: 12px; color: #666; margin-top: 4px; }}
            .btn-apply-row {{ background: #0066cc; color: white; border: none; border-radius: 4px; padding: 3px 10px; font-size: 12px; cursor: pointer; white-space: nowrap; }}
            .btn-apply-row:hover {{ background: #0052a3; }}
            .btn-apply-row:disabled {{ opacity: 0.7; cursor: default; }}
            .btn-apply-all {{ background: #198754; color: white; border: none; border-radius: 4px; padding: 8px 16px; font-size: 13px; cursor: pointer; font-weight: 600; }}
            .btn-apply-all:hover {{ background: #157347; }}
            .btn-apply-all:disabled {{ opacity: 0.7; cursor: default; }}
            .btn-cancel-run {{ background: #dc3545; color: white; border: none; border-radius: 4px; padding: 6px 14px; font-size: 13px; cursor: pointer; margin-left: 12px; }}
            .btn-cancel-run:hover {{ background: #bb2d3b; }}
            .btn-cancel-run:disabled {{ opacity: 0.6; cursor: default; }}
            .run-id-row {{ display: flex; align-items: center; gap: 4px; }}
        </style>
    </head>
    <body>
        <nav>
            <h1>🐛 Triage Bugs Tool (Claude)</h1>
            <ul>
                <li><a href="/ui">Dashboard</a></li>
                <li><a href="/ui/runs">Runs</a></li>
                <li><a href="/ui/integrations">Integrations</a></li>
            </ul>
        </nav>

        <div class="container">
            <h2>Triage Bug Tickets</h2>
            <p style="margin-bottom: 16px; color: #555;">Fetches bug issues from Jira, looks up their User Story spec in Confluence, and uses Claude to classify severity and impact. In dry-run mode no changes are made to Jira.</p>
            {error_block}
            <form method="post" action="/ui/workflows/triage_bugs/run" id="triageBugsForm">
                <div class="form-group">
                    <label for="jql">JQL Query</label>
                    <textarea id="jql" name="jql" rows="3">{values.get('jql', '')}</textarea>
                    <p class="hint">Jira Query Language filter — only issues matching this query will be triaged.</p>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label for="max_results">Max Results</label>
                        <input id="max_results" type="number" min="1" max="500" name="max_results" value="{values.get('max_results', '50')}" />
                    </div>
                    <div class="form-group">
                        <label for="batch_delay_seconds">Delay Between Claude Calls (seconds)</label>
                        <input id="batch_delay_seconds" type="number" min="0" max="60" name="batch_delay_seconds" value="{values.get('batch_delay_seconds', '10')}" />
                    </div>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label for="severity_field_id">Jira Severity Field ID</label>
                        <input id="severity_field_id" name="severity_field_id" value="{values.get('severity_field_id', 'customfield_10865')}" />
                    </div>
                    <div class="form-group">
                        <label for="impact_field_id">Jira Impact Field ID</label>
                        <input id="impact_field_id" name="impact_field_id" value="{values.get('impact_field_id', 'customfield_10004')}" />
                    </div>
                </div>

                <div class="form-group">
                    <label for="target_status">Target Jira Status (after triage)</label>
                    <input id="target_status" name="target_status" value="{values.get('target_status', 'Triage')}" />
                </div>

                <div class="checkbox">
                    <input type="checkbox" id="apply" name="apply" {"checked" if _bool_from_form(values.get('apply')) else ""} />
                    <label for="apply">Apply (update Jira fields and transition status)</label>
                </div>

                <div class="checkbox">
                    <input type="checkbox" id="add_comment" name="add_comment" {"checked" if _bool_from_form(values.get('add_comment')) else ""} />
                    <label for="add_comment">Add Jira triage comment (optional, off by default)</label>
                </div>

                <button type="submit">Run Triage</button>
            </form>

            {run_panel}
        </div>
        <script>
            document.getElementById("triageBugsForm").addEventListener("submit", (e) => {{
                const applyEl = document.getElementById("apply");
                if (applyEl.checked) {{
                    const ok = window.confirm("You are about to APPLY triage changes. Severity, impact, and status will be written to Jira. Continue?");
                    if (!ok) e.preventDefault();
                }}
            }});
        </script>
    </body></html>
    """


_REVIEW_NAV = """
<nav>
    <h1>🐛 Triage Bugs Tool (Claude)</h1>
    <ul>
        <li><a href="/ui">Dashboard</a></li>
        <li><a href="/ui/runs">Runs</a></li>
        <li><a href="/ui/integrations">Integrations</a></li>
    </ul>
</nav>
"""

_REVIEW_STYLE = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f5; }
nav { background: #0066cc; color: white; padding: 15px 30px; }
nav h1 { margin: 0; font-size: 24px; }
nav ul { list-style: none; display: flex; gap: 20px; margin-top: 10px; }
nav a { color: white; text-decoration: none; }
.container { max-width: 1100px; margin: 0 auto; padding: 20px; }
h2 { color: #333; margin: 20px 0 10px 0; }
form, .result-card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 16px; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.form-group { margin-bottom: 14px; }
label { display: block; font-weight: bold; margin-bottom: 6px; color: #333; }
input, select, textarea { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; font-family: inherit; }
.checkbox { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.checkbox input { width: auto; }
.checkbox label { font-weight: normal; margin: 0; }
button { background: #0066cc; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
button:hover { background: #0052a3; }
.error { background: #ffe7e7; color: #8b0000; border: 1px solid #f5baba; padding: 10px; margin-bottom: 12px; border-radius: 6px; }
.badge { display: inline-block; padding: 4px 8px; border-radius: 12px; font-weight: 600; margin: 8px 0; }
.progress-line { margin: 6px 0 12px 0; font-size: 14px; color: #333; }
.logs-toolbar { display: flex; justify-content: space-between; align-items: center; margin: 6px 0; font-size: 13px; color: #444; }
.logs-toolbar label { font-weight: normal; margin: 0; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
th, td { padding: 8px; border-bottom: 1px solid #eee; text-align: left; font-size: 13px; vertical-align: top; word-wrap: break-word; overflow-wrap: anywhere; }
th { background: #f9f9f9; font-weight: bold; }
pre { background: #1e1e1e; color: #d4d4d4; border-radius: 4px; padding: 10px; overflow: auto; max-height: 260px; font-size: 12px; }
h4 { margin: 16px 0 6px 0; color: #333; }
.hint { font-size: 12px; color: #666; margin-top: 4px; }
.sev-critical { background: #f8d7da; }
.sev-major { background: #fff3cd; }
.sev-minor { background: #fafafa; }
.verdict-fixed { background: #f0fff4; }
.verdict-not_fixed { background: #fff5f5; }
.verdict-unclear { background: #fafafa; }
.badge-pill { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
.badge-pill.critical, .badge-pill.not_fixed { background: #f8d7da; color: #842029; }
.badge-pill.major { background: #fff3cd; color: #664d03; }
.badge-pill.minor, .badge-pill.unclear { background: #e2e3e5; color: #41464b; }
.badge-pill.fixed { background: #d1e7dd; color: #0f5132; }
.btn-apply-row { background: #0066cc; color: white; border: none; border-radius: 4px; padding: 3px 10px; font-size: 12px; cursor: pointer; white-space: nowrap; }
.btn-apply-row:hover { background: #0052a3; }
.btn-apply-row:disabled { opacity: 0.7; cursor: default; }
.btn-apply-all { background: #198754; color: white; border: none; border-radius: 4px; padding: 8px 16px; font-size: 13px; cursor: pointer; font-weight: 600; }
.btn-apply-all:hover { background: #157347; }
.btn-apply-all:disabled { opacity: 0.7; cursor: default; }
.btn-cancel-run { background: #dc3545; color: white; border: none; border-radius: 4px; padding: 6px 14px; font-size: 13px; cursor: pointer; margin-left: 12px; }
.btn-cancel-run:hover { background: #bb2d3b; }
.btn-cancel-run:disabled { opacity: 0.6; cursor: default; }
.run-id-row { display: flex; align-items: center; gap: 4px; }
"""


def _render_review_pull_request_page(
    *,
    form_values: dict[str, str] | None = None,
    validation_error: str | None = None,
    run_id: str | None = None,
) -> str:
    values = {"repo": "", "pr_id": "", "project": "", "no_anonymize": ""}
    if form_values:
        values.update(form_values)

    error_block = (
        f'<div class="error">Validation error: {validation_error}</div>' if validation_error else ""
    )

    run_panel = ""
    if run_id:
        run_panel = f"""
        <div class="result-card">
            <h3>Run Monitor</h3>
            <p class="run-id-row">Run ID: <code>{run_id}</code><button id="btnCancelRun" class="btn-cancel-run" onclick="cancelRun()">⛔ Остановить</button><span id="cancelStatus" class="hint"></span></p>
            <div class="progress-line" id="runProgress">Preparing monitor...</div>
            <h4>Status</h4>
            <pre id="runStatus">Loading...</pre>
            <h4>Terminal-Like Stream</h4>
            <div class="logs-toolbar">
                <span id="logMeta">Logs: 0</span>
                <label><input type="checkbox" id="autoScrollLogs" checked /> Auto-scroll</label>
            </div>
            <pre id="liveLogs">Loading logs...</pre>
            <h4>Summary</h4>
            <p id="reviewSummary">Waiting for results...</p>
            <h4>Findings</h4>
            <div id="bulkApplyBar" style="display:none; margin-bottom:8px;">
                <button id="btnApplyAll" class="btn-apply-all" onclick="postAll()">📝 Post All Findings to PR</button>
                <span id="bulkApplyStatus" style="margin-left:12px; font-size:13px;"></span>
            </div>
            <div class="table-scroll">
            <table>
                <thead><tr><th>File</th><th>Line</th><th>Severity</th><th>Comment</th><th id="applyColHeader"></th></tr></thead>
                <tbody id="resultsTableBody"><tr><td colspan="5">Waiting for results...</td></tr></tbody>
            </table>
            </div>
            <h4>Skipped / Dropped Files</h4>
            <ul id="skippedList"><li>Waiting for results...</li></ul>
        </div>
        <script>
            const runId = "{run_id}";

            async function cancelRun() {{
                if (!confirm("Остановить выполнение? Текущий шаг доработает, затем воркфлоу остановится.")) return;
                const btn = document.getElementById("btnCancelRun");
                const statusEl = document.getElementById("cancelStatus");
                btn.disabled = true;
                statusEl.textContent = "Остановка запрошена…";
                try {{
                    const resp = await fetch(`/api/runs/${{runId}}/cancel`, {{ method: "POST" }});
                    const data = await resp.json();
                    if (!resp.ok) {{
                        statusEl.textContent = `Ошибка: ${{data.detail || resp.status}}`;
                        btn.disabled = false;
                        return;
                    }}
                    statusEl.textContent = data.cancel_requested
                        ? "Остановка запрошена — ждём завершения текущего шага…"
                        : (data.message || "Запуск уже завершён.");
                    if (!data.cancel_requested) btn.style.display = "none";
                }} catch (e) {{
                    statusEl.textContent = "Не удалось отправить запрос на остановку.";
                    btn.disabled = false;
                }}
            }}
            let done = false;
            let runFinished = false;
            let lastFindings = [];
            const monitorStartedAt = Date.now();
            const POLL_INTERVAL_MS = 1000;
            const ARTIFACT_POLL_EVERY_TICKS = 5;
            let tickCount = 0;

            function fmtElapsed(ms) {{
                const sec = Math.floor(ms / 1000);
                return `${{Math.floor(sec / 60)}}m ${{sec % 60}}s`;
            }}

            async function refreshRun() {{
                const runResp = await fetch(`/api/runs/${{runId}}`);
                if (!runResp.ok) return;
                const run = await runResp.json();
                document.getElementById("runStatus").textContent = JSON.stringify(run, null, 2);
                const elapsed = fmtElapsed(Date.now() - monitorStartedAt);
                document.getElementById("runProgress").textContent = `Status: ${{String(run.status || "unknown").toUpperCase()}} | Elapsed: ${{elapsed}}`;

                const logsResp = await fetch(`/api/runs/${{runId}}/logs`);
                if (logsResp.ok) {{
                    const logsData = await logsResp.json();
                    const logs = logsData.logs || [];
                    const lines = logs.map((l) => `${{l.timestamp}} [${{l.level}}] ${{l.message}}`);
                    const logsEl = document.getElementById("liveLogs");
                    logsEl.textContent = lines.join("\\n") || "No logs yet";
                    document.getElementById("logMeta").textContent = `Logs: ${{logs.length}}`;
                    if (document.getElementById("autoScrollLogs").checked) logsEl.scrollTop = logsEl.scrollHeight;
                }}

                const isTerminal = ["succeeded", "failed", "canceled"].includes(run.status);
                if (isTerminal) {{
                    runFinished = true;
                    document.getElementById("btnCancelRun").style.display = "none";
                }}

                if ((tickCount % ARTIFACT_POLL_EVERY_TICKS === 0) || isTerminal) {{
                    const artifactResp = await fetch(`/api/artifacts/run/${{runId}}`);
                    if (artifactResp.ok) {{
                        const artifacts = await artifactResp.json();
                        const resultArtifact = artifacts.find((a) => a.filename === "workflow_result.json");
                        if (resultArtifact) {{
                            const resultResp = await fetch(resultArtifact.download_url);
                            if (resultResp.ok) {{
                                const data = await resultResp.json();
                                document.getElementById("reviewSummary").textContent = data.summary || "(no summary)";
                                lastFindings = data.findings || [];
                                const body = document.getElementById("resultsTableBody");
                                body.innerHTML = "";
                                const showApplyCol = runFinished && lastFindings.length > 0;
                                document.getElementById("applyColHeader").textContent = showApplyCol ? "Action" : "";
                                if (lastFindings.length === 0) {{
                                    body.innerHTML = '<tr><td colspan="5">No findings.</td></tr>';
                                }}
                                lastFindings.forEach((f, idx) => {{
                                    const tr = document.createElement("tr");
                                    tr.id = `finding-${{idx}}`;
                                    tr.className = `sev-${{f.severity || "minor"}}`;
                                    const applyCell = showApplyCol
                                        ? `<td><button class="btn-apply-row" onclick="postOne(${{idx}}, this)">Post</button></td>`
                                        : `<td></td>`;
                                    tr.innerHTML = `
                                        <td>${{f.file || ""}}</td>
                                        <td>${{f.line ?? ""}}</td>
                                        <td><span class="badge-pill ${{f.severity || ""}}">${{f.severity || ""}}</span></td>
                                        <td>${{f.comment || ""}}</td>
                                        ${{applyCell}}
                                    `;
                                    body.appendChild(tr);
                                }});
                                if (showApplyCol && lastFindings.length > 0) {{
                                    document.getElementById("bulkApplyBar").style.display = "";
                                }}
                                const skippedEl = document.getElementById("skippedList");
                                const skipped = [...(data.skipped || []), ...(data.dropped || [])];
                                skippedEl.innerHTML = skipped.length
                                    ? skipped.map((s) => `<li>${{s}}</li>`).join("")
                                    : "<li>None</li>";
                            }}
                        }}
                    }}
                }}
                if (isTerminal) done = true;
            }}

            async function _callApply(findings) {{
                const statusEl = document.getElementById("bulkApplyStatus");
                const resp = await fetch("/api/workflows/review-pull-request/apply-comments", {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{ run_id: runId, findings: findings }}),
                }});
                if (!resp.ok) {{ statusEl.textContent = `Error: ${{await resp.text()}}`; return null; }}
                return await resp.json();
            }}

            async function postOne(idx, btn) {{
                if (!confirm("Post this finding as a PR comment?")) return;
                btn.disabled = true; btn.textContent = "...";
                const result = await _callApply([lastFindings[idx]]);
                if (!result) {{ btn.textContent = "Error"; return; }}
                if (result.posted_count > 0) {{ btn.textContent = "✅ Posted"; btn.style.background = "#198754"; }}
                else {{ btn.textContent = "❌ Failed"; btn.style.background = "#dc3545"; btn.title = (result.errors[0] || {{}}).error || ""; }}
            }}

            async function postAll() {{
                if (!confirm(`Post all ${{lastFindings.length}} finding(s) as PR comments?`)) return;
                const statusEl = document.getElementById("bulkApplyStatus");
                const allBtn = document.getElementById("btnApplyAll");
                allBtn.disabled = true;
                statusEl.textContent = "Posting...";
                const result = await _callApply(null);
                if (!result) {{ statusEl.textContent = "Request failed."; allBtn.disabled = false; return; }}
                statusEl.textContent = `✅ Posted ${{result.posted_count}} | ❌ Errors: ${{result.error_count}}`;
            }}

            async function tick() {{
                tickCount += 1;
                try {{ await refreshRun(); }} catch (e) {{}}
                if (!done) setTimeout(tick, POLL_INTERVAL_MS);
            }}
            tick();
        </script>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Review Pull Request - Triage Bugs Tool (Claude)</title>
        <style>{_REVIEW_STYLE}</style>
    </head>
    <body>
        {_REVIEW_NAV}
        <div class="container">
            <h2>Review Pull Request</h2>
            <p style="margin-bottom: 16px; color: #555;">Builds a line-numbered diff for the PR's latest iteration and asks Claude to review it for bugs, inconsistencies and other functional issues. Findings are analyzed here first (dry-run) — you choose which ones to post as PR comments.</p>
            {error_block}
            <form method="post" action="/ui/workflows/review_pull_request/run" id="reviewPrForm">
                <div class="form-row">
                    <div class="form-group">
                        <label for="repo">Repository (name or ID)</label>
                        <input id="repo" name="repo" value="{values.get('repo', '')}" required />
                    </div>
                    <div class="form-group">
                        <label for="pr_id">Pull Request ID</label>
                        <input id="pr_id" type="number" min="1" name="pr_id" value="{values.get('pr_id', '')}" required />
                    </div>
                </div>
                <div class="form-group">
                    <label for="project">Azure DevOps Project (optional override)</label>
                    <input id="project" name="project" value="{values.get('project', '')}" placeholder="Defaults to the configured project" />
                </div>
                <div class="checkbox">
                    <input type="checkbox" id="no_anonymize" name="no_anonymize" {"checked" if _bool_from_form(values.get('no_anonymize')) else ""} />
                    <label for="no_anonymize">Skip anonymization (send raw diff content to Claude)</label>
                </div>
                <button type="submit">Run Review</button>
            </form>
            {run_panel}
        </div>
    </body></html>
    """


def _render_review_comment_fixes_page(
    *,
    form_values: dict[str, str] | None = None,
    validation_error: str | None = None,
    run_id: str | None = None,
) -> str:
    values = {"repo": "", "pr_id": "", "project": "", "no_anonymize": ""}
    if form_values:
        values.update(form_values)

    error_block = (
        f'<div class="error">Validation error: {validation_error}</div>' if validation_error else ""
    )

    run_panel = ""
    if run_id:
        run_panel = f"""
        <div class="result-card">
            <h3>Run Monitor</h3>
            <p class="run-id-row">Run ID: <code>{run_id}</code><button id="btnCancelRun" class="btn-cancel-run" onclick="cancelRun()">⛔ Остановить</button><span id="cancelStatus" class="hint"></span></p>
            <div class="progress-line" id="runProgress">Preparing monitor...</div>
            <h4>Status</h4>
            <pre id="runStatus">Loading...</pre>
            <h4>Terminal-Like Stream</h4>
            <div class="logs-toolbar">
                <span id="logMeta">Logs: 0</span>
                <label><input type="checkbox" id="autoScrollLogs" checked /> Auto-scroll</label>
            </div>
            <pre id="liveLogs">Loading logs...</pre>
            <h4>Thread Verdicts</h4>
            <div id="bulkApplyBar" style="display:none; margin-bottom:8px;">
                <button id="btnApplyAll" class="btn-apply-all" onclick="applyAll()">↩️ Reply + Reopen All "Not Fixed"</button>
                <span id="bulkApplyStatus" style="margin-left:12px; font-size:13px;"></span>
            </div>
            <div class="table-scroll">
            <table>
                <thead><tr><th>Thread</th><th>File:Line</th><th>ADO Status</th><th>Verdict</th><th>Reasoning</th><th id="applyColHeader"></th></tr></thead>
                <tbody id="resultsTableBody"><tr><td colspan="6">Waiting for results...</td></tr></tbody>
            </table>
            </div>
        </div>
        <script>
            const runId = "{run_id}";

            async function cancelRun() {{
                if (!confirm("Остановить выполнение? Текущий шаг доработает, затем воркфлоу остановится.")) return;
                const btn = document.getElementById("btnCancelRun");
                const statusEl = document.getElementById("cancelStatus");
                btn.disabled = true;
                statusEl.textContent = "Остановка запрошена…";
                try {{
                    const resp = await fetch(`/api/runs/${{runId}}/cancel`, {{ method: "POST" }});
                    const data = await resp.json();
                    if (!resp.ok) {{
                        statusEl.textContent = `Ошибка: ${{data.detail || resp.status}}`;
                        btn.disabled = false;
                        return;
                    }}
                    statusEl.textContent = data.cancel_requested
                        ? "Остановка запрошена — ждём завершения текущего шага…"
                        : (data.message || "Запуск уже завершён.");
                    if (!data.cancel_requested) btn.style.display = "none";
                }} catch (e) {{
                    statusEl.textContent = "Не удалось отправить запрос на остановку.";
                    btn.disabled = false;
                }}
            }}
            let done = false;
            let runFinished = false;
            let lastResults = [];
            let lastContexts = [];
            const monitorStartedAt = Date.now();
            const POLL_INTERVAL_MS = 1000;
            const ARTIFACT_POLL_EVERY_TICKS = 5;
            let tickCount = 0;

            function fmtElapsed(ms) {{
                const sec = Math.floor(ms / 1000);
                return `${{Math.floor(sec / 60)}}m ${{sec % 60}}s`;
            }}

            async function refreshRun() {{
                const runResp = await fetch(`/api/runs/${{runId}}`);
                if (!runResp.ok) return;
                const run = await runResp.json();
                document.getElementById("runStatus").textContent = JSON.stringify(run, null, 2);
                const elapsed = fmtElapsed(Date.now() - monitorStartedAt);
                document.getElementById("runProgress").textContent = `Status: ${{String(run.status || "unknown").toUpperCase()}} | Elapsed: ${{elapsed}}`;

                const logsResp = await fetch(`/api/runs/${{runId}}/logs`);
                if (logsResp.ok) {{
                    const logsData = await logsResp.json();
                    const logs = logsData.logs || [];
                    const lines = logs.map((l) => `${{l.timestamp}} [${{l.level}}] ${{l.message}}`);
                    const logsEl = document.getElementById("liveLogs");
                    logsEl.textContent = lines.join("\\n") || "No logs yet";
                    document.getElementById("logMeta").textContent = `Logs: ${{logs.length}}`;
                    if (document.getElementById("autoScrollLogs").checked) logsEl.scrollTop = logsEl.scrollHeight;
                }}

                const isTerminal = ["succeeded", "failed", "canceled"].includes(run.status);
                if (isTerminal) {{
                    runFinished = true;
                    document.getElementById("btnCancelRun").style.display = "none";
                }}

                if ((tickCount % ARTIFACT_POLL_EVERY_TICKS === 0) || isTerminal) {{
                    const artifactResp = await fetch(`/api/artifacts/run/${{runId}}`);
                    if (artifactResp.ok) {{
                        const artifacts = await artifactResp.json();
                        const resultArtifact = artifacts.find((a) => a.filename === "workflow_result.json");
                        if (resultArtifact) {{
                            const resultResp = await fetch(resultArtifact.download_url);
                            if (resultResp.ok) {{
                                const data = await resultResp.json();
                                lastResults = data.results || [];
                                lastContexts = data.contexts || [];
                                const ctxById = Object.fromEntries(lastContexts.map((c) => [c.thread_id, c]));
                                const body = document.getElementById("resultsTableBody");
                                body.innerHTML = "";
                                const notFixed = lastResults.filter((r) => r.verdict === "not_fixed");
                                const showApplyCol = runFinished && notFixed.length > 0;
                                document.getElementById("applyColHeader").textContent = showApplyCol ? "Action" : "";
                                if (lastResults.length === 0) {{
                                    const emptyMsg = data.message || "Нет комментариев для проверки.";
                                    body.innerHTML = `<tr><td colspan="6">${{emptyMsg}}</td></tr>`;
                                }}
                                lastResults.forEach((r) => {{
                                    const ctx = ctxById[r.thread_id] || {{}};
                                    const tr = document.createElement("tr");
                                    tr.id = `thread-${{r.thread_id}}`;
                                    tr.className = `verdict-${{r.verdict || "unclear"}}`;
                                    const applyCell = (showApplyCol && r.verdict === "not_fixed")
                                        ? `<td><button class="btn-apply-row" onclick="applyOne(${{r.thread_id}}, this)">Reply</button></td>`
                                        : `<td></td>`;
                                    tr.innerHTML = `
                                        <td>#${{r.thread_id}}</td>
                                        <td>${{ctx.path || ""}}:${{ctx.line ?? ""}}</td>
                                        <td>${{ctx.status || ""}}</td>
                                        <td><span class="badge-pill ${{r.verdict || ""}}">${{r.verdict || ""}}</span></td>
                                        <td>${{r.reasoning || ""}}</td>
                                        ${{applyCell}}
                                    `;
                                    body.appendChild(tr);
                                }});
                                if (showApplyCol) document.getElementById("bulkApplyBar").style.display = "";
                            }}
                        }}
                    }}
                }}
                if (isTerminal) done = true;
            }}

            async function _callApply(threadIds) {{
                const statusEl = document.getElementById("bulkApplyStatus");
                const resp = await fetch("/api/workflows/review-comment-fixes/apply-replies", {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{ run_id: runId, thread_ids: threadIds }}),
                }});
                if (!resp.ok) {{ statusEl.textContent = `Error: ${{await resp.text()}}`; return null; }}
                return await resp.json();
            }}

            async function applyOne(threadId, btn) {{
                if (!confirm(`Post the suggested reply and reopen thread #${{threadId}} if needed?`)) return;
                btn.disabled = true; btn.textContent = "...";
                const result = await _callApply([threadId]);
                if (!result) {{ btn.textContent = "Error"; return; }}
                if (result.handled.includes(threadId)) {{ btn.textContent = "✅ Done"; btn.style.background = "#198754"; }}
                else {{ btn.textContent = "❌ Failed"; btn.style.background = "#dc3545"; }}
            }}

            async function applyAll() {{
                const notFixedIds = lastResults.filter((r) => r.verdict === "not_fixed").map((r) => r.thread_id);
                if (!notFixedIds.length) return;
                if (!confirm(`Reply to and reopen ${{notFixedIds.length}} not-fixed thread(s)?`)) return;
                const statusEl = document.getElementById("bulkApplyStatus");
                const allBtn = document.getElementById("btnApplyAll");
                allBtn.disabled = true;
                statusEl.textContent = "Applying...";
                const result = await _callApply(null);
                if (!result) {{ statusEl.textContent = "Request failed."; allBtn.disabled = false; return; }}
                statusEl.textContent = `✅ Handled ${{result.handled_count}} | ❌ Errors: ${{result.error_count}}`;
            }}

            async function tick() {{
                tickCount += 1;
                try {{ await refreshRun(); }} catch (e) {{}}
                if (!done) setTimeout(tick, POLL_INTERVAL_MS);
            }}
            tick();
        </script>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Review Comment Fixes - Triage Bugs Tool (Claude)</title>
        <style>{_REVIEW_STYLE}</style>
    </head>
    <body>
        {_REVIEW_NAV}
        <div class="container">
            <h2>Review Comment Fixes</h2>
            <p style="margin-bottom: 16px; color: #555;">Looks at PR comment threads a colleague has already responded to (marked fixed/closed, or with a reply) and asks Claude whether the underlying code actually addresses what was raised. Results are analyzed here first (dry-run) — you choose which "not fixed" threads get a reply and get reopened.</p>
            {error_block}
            <form method="post" action="/ui/workflows/review_comment_fixes/run" id="reviewFixesForm">
                <div class="form-row">
                    <div class="form-group">
                        <label for="repo">Repository (name or ID)</label>
                        <input id="repo" name="repo" value="{values.get('repo', '')}" required />
                    </div>
                    <div class="form-group">
                        <label for="pr_id">Pull Request ID</label>
                        <input id="pr_id" type="number" min="1" name="pr_id" value="{values.get('pr_id', '')}" required />
                    </div>
                </div>
                <div class="form-group">
                    <label for="project">Azure DevOps Project (optional override)</label>
                    <input id="project" name="project" value="{values.get('project', '')}" placeholder="Defaults to the configured project" />
                </div>
                <div class="checkbox">
                    <input type="checkbox" id="no_anonymize" name="no_anonymize" {"checked" if _bool_from_form(values.get('no_anonymize')) else ""} />
                    <label for="no_anonymize">Skip anonymization (send raw code/comments to Claude)</label>
                </div>
                <button type="submit">Run Verification</button>
            </form>
            {run_panel}
        </div>
    </body></html>
    """


def _render_upload_test_cases_page(
    *,
    form_values: dict[str, str] | None = None,
    validation_error: str | None = None,
    run_id: str | None = None,
) -> str:
    values = {
        "plan_key": "web",
        "us": "",
        "specs_folder": upload_workflow.DEFAULT_SPECS_FOLDER_TITLE,
        "admin_specs_folder_id": upload_workflow.DEFAULT_ADMIN_SPECS_FOLDER_ID,
        "admin_group_title": upload_workflow.DEFAULT_ADMIN_GROUP_SUITE_TITLE,
        "epic_suite_name": "",
        "us_suite_name": "",
        "state": upload_workflow.DEFAULT_STATE,
        "apply": "",
        "existing_mode": "add",
    }
    if form_values:
        values.update(form_values)

    error_block = (
        f'<div class="error">Ошибка: {validation_error}</div>' if validation_error else ""
    )

    plan_options = ""
    for key, plan in upload_workflow.TEST_PLANS.items():
        selected = "selected" if values.get("plan_key") == key else ""
        plan_options += f'<option value="{key}" {selected}>{plan["label"]}</option>'

    run_panel = ""
    if run_id:
        run_panel = f"""
        <div class="result-card">
            <h3>Монитор запуска</h3>
            <p class="run-id-row">Run ID: <code>{run_id}</code><button id="btnCancelRun" class="btn-cancel-run" onclick="cancelRun()">⛔ Остановить</button><span id="cancelStatus" class="hint"></span></p>
            <div class="progress-line" id="runProgress">Подготовка монитора...</div>
            <h4>Статус</h4>
            <pre id="runStatus">Загрузка...</pre>
            <h4>Поток логов</h4>
            <div class="logs-toolbar">
                <span id="logMeta">Логов: 0</span>
                <label><input type="checkbox" id="autoScrollLogs" checked /> Автопрокрутка</label>
            </div>
            <pre id="liveLogs">Загрузка логов...</pre>
            <h4>Цепочка Suite</h4>
            <ul id="suiteChainList"><li>Ожидание результатов...</li></ul>
            <h4 id="resultsHeading">Тест-кейсы</h4>
            <div class="table-scroll">
            <table>
                <thead><tr><th>Название</th><th>Приоритет</th><th>Шагов</th><th id="statusColHeader">Статус</th></tr></thead>
                <tbody id="resultsTableBody"><tr><td colspan="4">Ожидание результатов...</td></tr></tbody>
            </table>
            </div>
        </div>
        <script>
            const runId = "{run_id}";

            async function cancelRun() {{
                if (!confirm("Остановить выполнение? Текущий шаг доработает, затем воркфлоу остановится.")) return;
                const btn = document.getElementById("btnCancelRun");
                const statusEl = document.getElementById("cancelStatus");
                btn.disabled = true;
                statusEl.textContent = "Остановка запрошена…";
                try {{
                    const resp = await fetch(`/api/runs/${{runId}}/cancel`, {{ method: "POST" }});
                    const data = await resp.json();
                    if (!resp.ok) {{
                        statusEl.textContent = `Ошибка: ${{data.detail || resp.status}}`;
                        btn.disabled = false;
                        return;
                    }}
                    statusEl.textContent = data.cancel_requested
                        ? "Остановка запрошена — ждём завершения текущего шага…"
                        : (data.message || "Запуск уже завершён.");
                    if (!data.cancel_requested) btn.style.display = "none";
                }} catch (e) {{
                    statusEl.textContent = "Не удалось отправить запрос на остановку.";
                    btn.disabled = false;
                }}
            }}
            let done = false;
            const monitorStartedAt = Date.now();
            const POLL_INTERVAL_MS = 1000;
            const ARTIFACT_POLL_EVERY_TICKS = 5;
            let tickCount = 0;

            function fmtElapsed(ms) {{
                const sec = Math.floor(ms / 1000);
                return `${{Math.floor(sec / 60)}}m ${{sec % 60}}s`;
            }}

            async function refreshRun() {{
                const runResp = await fetch(`/api/runs/${{runId}}`);
                if (!runResp.ok) return;
                const run = await runResp.json();
                document.getElementById("runStatus").textContent = JSON.stringify(run, null, 2);
                const elapsed = fmtElapsed(Date.now() - monitorStartedAt);
                document.getElementById("runProgress").textContent = `Статус: ${{String(run.status || "unknown").toUpperCase()}} | Прошло: ${{elapsed}}`;

                const logsResp = await fetch(`/api/runs/${{runId}}/logs`);
                if (logsResp.ok) {{
                    const logsData = await logsResp.json();
                    const logs = logsData.logs || [];
                    const lines = logs.map((l) => `${{l.timestamp}} [${{l.level}}] ${{l.message}}`);
                    const logsEl = document.getElementById("liveLogs");
                    logsEl.textContent = lines.join("\\n") || "Логов пока нет";
                    document.getElementById("logMeta").textContent = `Логов: ${{logs.length}}`;
                    if (document.getElementById("autoScrollLogs").checked) logsEl.scrollTop = logsEl.scrollHeight;
                }}

                const isTerminal = ["succeeded", "failed", "canceled"].includes(run.status);
                if (isTerminal) document.getElementById("btnCancelRun").style.display = "none";

                if ((tickCount % ARTIFACT_POLL_EVERY_TICKS === 0) || isTerminal) {{
                    const artifactResp = await fetch(`/api/artifacts/run/${{runId}}`);
                    if (artifactResp.ok) {{
                        const artifacts = await artifactResp.json();
                        const resultArtifact = artifacts.find((a) => a.filename === "workflow_result.json");
                        if (resultArtifact) {{
                            const resultResp = await fetch(resultArtifact.download_url);
                            if (resultResp.ok) {{
                                const data = await resultResp.json();

                                const statusRu = {{ found: "найден", created: "создан", would_create: "будет создан" }};
                                const chainEl = document.getElementById("suiteChainList");
                                const levels = data.chain_levels || [];
                                chainEl.innerHTML = levels.length
                                    ? levels.map((l) => `<li>${{l.title}} (id=${{l.id ?? "—"}}) — ${{statusRu[l.status] || l.status}}</li>`).join("")
                                    : "<li>Цепочка suite не определена.</li>";

                                const isDryRun = !!data.dry_run;
                                document.getElementById("resultsHeading").textContent = isDryRun
                                    ? `Предпросмотр (${{data.test_cases_total ?? 0}} тест-кейс(ов) — в Azure DevOps ничего не записано)`
                                    : `Результаты загрузки (создано: ${{data.created_count ?? 0}}, пропущено: ${{data.skipped_count ?? 0}}, ошибок: ${{data.failed_count ?? 0}})`;
                                document.getElementById("statusColHeader").textContent = isDryRun ? "Дубликат?" : "Результат";

                                const body = document.getElementById("resultsTableBody");
                                body.innerHTML = "";
                                if (isDryRun) {{
                                    const rows = data.preview || [];
                                    if (!rows.length) body.innerHTML = '<tr><td colspan="4">Не удалось разобрать тест-кейсы из CSV.</td></tr>';
                                    rows.forEach((r) => {{
                                        const tr = document.createElement("tr");
                                        tr.innerHTML = `
                                            <td>${{r.title || ""}}</td>
                                            <td>${{r.priority || ""}}</td>
                                            <td>${{r.steps_count ?? ""}}</td>
                                            <td>${{r.duplicate ? "⚠️ уже существует" : ""}}</td>
                                        `;
                                        body.appendChild(tr);
                                    }});
                                }} else {{
                                    const rows = data.results || [];
                                    if (!rows.length) body.innerHTML = '<tr><td colspan="4">Результатов пока нет.</td></tr>';
                                    rows.forEach((r) => {{
                                        const res = r.result || {{}};
                                        const tr = document.createElement("tr");
                                        let statusText = "";
                                        if (res.skipped) statusText = `⏭️ пропущен (id=${{res.case_id}})`;
                                        else if (res.success) statusText = `✅ создан (id=${{res.case_id}})`;
                                        else statusText = `❌ ошибка: ${{res.error || ""}}`;
                                        tr.innerHTML = `
                                            <td>${{r.title || ""}}</td>
                                            <td></td>
                                            <td></td>
                                            <td>${{statusText}}</td>
                                        `;
                                        body.appendChild(tr);
                                    }});
                                }}
                            }}
                        }}
                    }}
                }}
                if (isTerminal) done = true;
            }}

            async function tick() {{
                tickCount += 1;
                try {{ await refreshRun(); }} catch (e) {{}}
                if (!done) setTimeout(tick, POLL_INTERVAL_MS);
            }}
            tick();
        </script>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Upload Test Cases - Triage Bugs Tool (Claude)</title>
        <style>{_REVIEW_STYLE}</style>
    </head>
    <body>
        {_REVIEW_NAV}
        <div class="container">
            <h2>Загрузка тест-кейсов в Azure DevOps</h2>
            <p style="margin-bottom: 16px; color: #555;">Загружает провалидированный CSV с тест-кейсами для одной User Story в соответствующий suite Test Plan в Azure DevOps, автоматически определяя (или создавая) цепочку root → [Админ Панель] → Epic → User Story по родительским страницам User Story в Confluence. По умолчанию — только предпросмотр, в Azure DevOps ничего не записывается, пока не отмечен чекбокс «Применить».</p>
            {error_block}
            <form method="post" action="/ui/workflows/upload_test_cases/run" enctype="multipart/form-data" id="uploadTestCasesForm">
                <div class="form-row">
                    <div class="form-group">
                        <label for="plan_key">Тест-план</label>
                        <select id="plan_key" name="plan_key">{plan_options}</select>
                    </div>
                    <div class="form-group">
                        <label for="us">Номер User Story</label>
                        <input id="us" name="us" value="{values.get('us', '')}" placeholder="20.1.1 или US-20.1.1 или AUS-7.2" required />
                    </div>
                </div>
                <div class="form-group">
                    <label for="csv_file">CSV с провалидированными тест-кейсами</label>
                    <input id="csv_file" type="file" name="csv_file" accept=".csv" required />
                    <p class="hint">Экспорт Azure DevOps Test Plan в CSV (9-10 колонок), уже прошедший ревью. Колонки "ID"/"State"/"Area Path" (если есть) игнорируются — используются только для поиска дублей по названию.</p>
                </div>
                <details>
                    <summary style="cursor:pointer; margin-bottom: 10px; font-weight: bold; color: #333;">Дополнительные настройки</summary>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="specs_folder">Папка спецификаций в Confluence</label>
                            <input id="specs_folder" name="specs_folder" value="{values.get('specs_folder', '')}" />
                        </div>
                        <div class="form-group">
                            <label for="admin_specs_folder_id">ID папки спецификаций админ-панели (fallback)</label>
                            <input id="admin_specs_folder_id" name="admin_specs_folder_id" value="{values.get('admin_specs_folder_id', '')}" />
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="admin_group_title">Название группирующего suite админ-панели</label>
                            <input id="admin_group_title" name="admin_group_title" value="{values.get('admin_group_title', '')}" />
                        </div>
                        <div class="form-group">
                            <label for="state">Azure DevOps State</label>
                            <input id="state" name="state" value="{values.get('state', '')}" />
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="epic_suite_name">Название Epic suite (переопределить)</label>
                            <input id="epic_suite_name" name="epic_suite_name" value="{values.get('epic_suite_name', '')}" placeholder="По умолчанию - название родительской страницы в Confluence" />
                        </div>
                        <div class="form-group">
                            <label for="us_suite_name">Название US suite (переопределить)</label>
                            <input id="us_suite_name" name="us_suite_name" value="{values.get('us_suite_name', '')}" placeholder="По умолчанию - название страницы User Story" />
                        </div>
                    </div>
                </details>
                <div class="checkbox">
                    <input type="checkbox" id="apply" name="apply" {"checked" if _bool_from_form(values.get('apply')) else ""} />
                    <label for="apply">Применить (реально создать тест-кейсы в Azure DevOps — если не отмечено, будет только предпросмотр)</label>
                </div>
                <div class="form-group">
                    <label>Если в целевом suite уже есть тест-кейсы</label>
                    <div class="checkbox">
                        <input type="radio" name="existing_mode" id="existing_mode_add" value="add" {"checked" if values.get('existing_mode', 'add') != 'replace' else ""} />
                        <label for="existing_mode_add">Добавить новые к уже существующим (тест-кейсы с совпадающим названием пропускаются — можно запускать повторно без риска дублей)</label>
                    </div>
                    <div class="checkbox">
                        <input type="radio" name="existing_mode" id="existing_mode_replace" value="replace" {"checked" if values.get('existing_mode') == 'replace' else ""} />
                        <label for="existing_mode_replace">Убрать ВСЕ существующие тест-кейсы из этого suite (не удаляются навсегда, только отвязываются от suite) и загрузить всё заново из CSV</label>
                    </div>
                </div>
                <button type="submit">Запустить загрузку</button>
            </form>
            {run_panel}
        </div>
        <script>
            document.getElementById("uploadTestCasesForm").addEventListener("submit", (e) => {{
                const applyEl = document.getElementById("apply");
                const replaceMode = document.getElementById("existing_mode_replace").checked;
                if (replaceMode) {{
                    const ok = window.confirm("Это уберёт ВСЕ существующие тест-кейсы из целевого suite перед загрузкой (тест-кейсы не удаляются навсегда, только отвязываются от suite). Продолжить?");
                    if (!ok) {{ e.preventDefault(); return; }}
                }}
                if (applyEl.checked) {{
                    const ok = window.confirm("Вы собираетесь СОЗДАТЬ тест-кейсы в Azure DevOps. Продолжить?");
                    if (!ok) e.preventDefault();
                }}
            }});
        </script>
    </body></html>
    """


def _render_skipped_tests_audit_page(
    *,
    form_values: dict[str, str] | None = None,
    validation_error: str | None = None,
    run_id: str | None = None,
) -> str:
    values = {"tests_root": "/Users/oadmin/PROJECTS/FINCA/qa-api-tests/tests"}
    if form_values:
        values.update(form_values)

    error_block = (
        f'<div class="error">Validation error: {validation_error}</div>' if validation_error else ""
    )

    run_panel = ""
    if run_id:
        run_panel = f"""
        <div class="result-card">
            <h3>Run Monitor</h3>
            <p class="run-id-row">Run ID: <code>{run_id}</code><button id="btnCancelRun" class="btn-cancel-run" onclick="cancelRun()">⛔ Остановить</button><span id="cancelStatus" class="hint"></span></p>
            <div class="progress-line" id="runProgress">Preparing monitor...</div>
            <h4>Status</h4>
            <pre id="runStatus">Loading...</pre>
            <h4>Terminal-Like Stream</h4>
            <div class="logs-toolbar">
                <span id="logMeta">Logs: 0</span>
                <label><input type="checkbox" id="autoScrollLogs" checked /> Auto-scroll</label>
            </div>
            <pre id="liveLogs">Loading logs...</pre>
            <h4>Сводка по категориям</h4>
            <p id="totalSummary" class="hint"></p>
            <div class="counters" id="categoryCounters" style="grid-template-columns: repeat(3, 1fr);"></div>
            <h4>Результаты</h4>
            <div class="table-scroll">
            <table>
                <colgroup>
                    <col style="width: 14%" /><col style="width: 16%" /><col style="width: 24%" /><col style="width: 16%" /><col style="width: 30%" />
                </colgroup>
                <thead><tr><th>Категория</th><th>Файл:Строка</th><th>Тест</th><th>Баг(и) в Jira</th><th>Причина</th></tr></thead>
                <tbody id="resultsTableBody"><tr><td colspan="5">Waiting for results...</td></tr></tbody>
            </table>
            </div>
            <h4>Артефакты</h4>
            <ul id="artifactLinks"><li>Waiting for artifacts...</li></ul>
        </div>
        <script>
            const runId = "{run_id}";

            async function cancelRun() {{
                if (!confirm("Остановить выполнение? Текущий шаг доработает, затем воркфлоу остановится.")) return;
                const btn = document.getElementById("btnCancelRun");
                const statusEl = document.getElementById("cancelStatus");
                btn.disabled = true;
                statusEl.textContent = "Остановка запрошена…";
                try {{
                    const resp = await fetch(`/api/runs/${{runId}}/cancel`, {{ method: "POST" }});
                    const data = await resp.json();
                    if (!resp.ok) {{
                        statusEl.textContent = `Ошибка: ${{data.detail || resp.status}}`;
                        btn.disabled = false;
                        return;
                    }}
                    statusEl.textContent = data.cancel_requested
                        ? "Остановка запрошена — ждём завершения текущего шага…"
                        : (data.message || "Запуск уже завершён.");
                    if (!data.cancel_requested) btn.style.display = "none";
                }} catch (e) {{
                    statusEl.textContent = "Не удалось отправить запрос на остановку.";
                    btn.disabled = false;
                }}
            }}
            let done = false;
            const monitorStartedAt = Date.now();
            const POLL_INTERVAL_MS = 1000;
            const ARTIFACT_POLL_EVERY_TICKS = 5;
            let tickCount = 0;

            function fmtElapsed(ms) {{
                const sec = Math.floor(ms / 1000);
                return `${{Math.floor(sec / 60)}}m ${{sec % 60}}s`;
            }}

            function bugCell(row) {{
                if (!row.jira || !row.jira.length) return "";
                return row.jira.map((j) => {{
                    if (j.not_found) return `${{j.key}} (не найден)`;
                    const res = j.resolution ? ` / ${{j.resolution}}` : "";
                    return `${{j.key}} [${{j.status}}${{res}}]`;
                }}).join(", ");
            }}

            async function refreshRun() {{
                const runResp = await fetch(`/api/runs/${{runId}}`);
                if (!runResp.ok) return;
                const run = await runResp.json();
                document.getElementById("runStatus").textContent = JSON.stringify(run, null, 2);
                const elapsed = fmtElapsed(Date.now() - monitorStartedAt);
                document.getElementById("runProgress").textContent = `Status: ${{String(run.status || "unknown").toUpperCase()}} | Elapsed: ${{elapsed}}`;

                const logsResp = await fetch(`/api/runs/${{runId}}/logs`);
                if (logsResp.ok) {{
                    const logsData = await logsResp.json();
                    const logs = logsData.logs || [];
                    const lines = logs.map((l) => `${{l.timestamp}} [${{l.level}}] ${{l.message}}`);
                    const logsEl = document.getElementById("liveLogs");
                    logsEl.textContent = lines.join("\\n") || "No logs yet";
                    document.getElementById("logMeta").textContent = `Logs: ${{logs.length}}`;
                    if (document.getElementById("autoScrollLogs").checked) logsEl.scrollTop = logsEl.scrollHeight;
                }}

                const isTerminal = ["succeeded", "failed", "canceled"].includes(run.status);
                if (isTerminal) document.getElementById("btnCancelRun").style.display = "none";

                if ((tickCount % ARTIFACT_POLL_EVERY_TICKS === 0) || isTerminal) {{
                    const artifactResp = await fetch(`/api/artifacts/run/${{runId}}`);
                    if (artifactResp.ok) {{
                        const artifacts = await artifactResp.json();
                        const linksEl = document.getElementById("artifactLinks");
                        linksEl.innerHTML = "";
                        for (const item of artifacts) {{
                            const li = document.createElement("li");
                            const a = document.createElement("a");
                            a.href = item.download_url;
                            a.textContent = item.filename;
                            li.appendChild(a);
                            linksEl.appendChild(li);
                        }}

                        const resultArtifact = artifacts.find((a) => a.filename === "workflow_result.json");
                        if (resultArtifact) {{
                            const resultResp = await fetch(resultArtifact.download_url);
                            if (resultResp.ok) {{
                                const data = await resultResp.json();
                                const labels = data.category_labels || {{}};
                                const categories = data.categories || {{}};
                                const summary = data.summary || {{}};

                                document.getElementById("totalSummary").textContent =
                                    `Всего найдено: ${{summary.total_candidates ?? 0}} (файлов просканировано: ${{summary.files_scanned ?? 0}})`;

                                const countersEl = document.getElementById("categoryCounters");
                                countersEl.innerHTML = "";
                                for (const [key, rows] of Object.entries(categories)) {{
                                    const div = document.createElement("div");
                                    div.innerHTML = `<strong>${{rows.length}}</strong><br/>${{labels[key] || key}}`;
                                    countersEl.appendChild(div);
                                }}

                                const body = document.getElementById("resultsTableBody");
                                body.innerHTML = "";
                                let anyRows = false;
                                for (const [key, rows] of Object.entries(categories)) {{
                                    for (const row of rows) {{
                                        anyRows = true;
                                        const tr = document.createElement("tr");
                                        const suite = row.suite_path ? `${{row.suite_path}} › ` : "";
                                        tr.innerHTML = `
                                            <td><span class="badge-pill">${{labels[key] || key}}</span></td>
                                            <td>${{row.file}}:${{row.line}}</td>
                                            <td>${{suite}}${{row.test_name}}</td>
                                            <td>${{bugCell(row)}}</td>
                                            <td>${{row.reason_summary || ""}}</td>
                                        `;
                                        body.appendChild(tr);
                                    }}
                                }}
                                if (!anyRows) {{
                                    body.innerHTML = '<tr><td colspan="5">Кандидатов не найдено.</td></tr>';
                                }}
                            }}
                        }}
                    }}
                }}
                if (isTerminal) done = true;
            }}

            async function tick() {{
                tickCount += 1;
                try {{ await refreshRun(); }} catch (e) {{}}
                if (!done) setTimeout(tick, POLL_INTERVAL_MS);
            }}
            tick();
        </script>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Skipped Tests Audit - Triage Bugs Tool (Claude)</title>
        <style>{_REVIEW_STYLE}</style>
    </head>
    <body>
        {_REVIEW_NAV}
        <div class="container">
            <h2>Аудит skip-тестов</h2>
            <p style="margin-bottom: 16px; color: #555;">Сканирует автотесты (*.spec.ts) на предмет `.skip`, `todo`, `жду` или ссылок на баги (MB-XXXX/МВ-ХХХХ), просит Claude разобраться в настоящей причине каждого пропуска и проверяет упомянутые баги в Jira. Только чтение — ничего не пишет ни в Jira, ни в файлы тестов.</p>
            {error_block}
            <form method="post" action="/ui/workflows/skipped_tests_audit/run" id="skippedTestsAuditForm">
                <div class="form-group">
                    <label for="tests_root">Корневая папка тестов</label>
                    <input id="tests_root" name="tests_root" value="{values.get('tests_root', '')}" required />
                    <p class="hint">Абсолютный путь к папке с *.spec.ts (например, .../qa-api-tests/tests)</p>
                </div>
                <button type="submit">Запустить аудит</button>
            </form>
            {run_panel}
        </div>
    </body></html>
    """


def _render_review_test_cases_page(
    *,
    form_values: dict[str, str] | None = None,
    validation_error: str | None = None,
    run_id: str | None = None,
) -> str:
    values = {"test_type": "web", "us": "", "spec_url": "", "tech_impl_url": ""}
    if form_values:
        values.update(form_values)

    error_block = (
        f'<div class="error">Ошибка: {validation_error}</div>' if validation_error else ""
    )

    type_labels = {"web": "Web", "mobile": "Mobile", "api": "API"}
    type_options = ""
    for key in review_test_cases_workflow.TEST_TYPES:
        selected = "selected" if values.get("test_type") == key else ""
        type_options += f'<option value="{key}" {selected}>{type_labels.get(key, key)}</option>'

    run_panel = ""
    if run_id:
        run_panel = f"""
        <div class="result-card">
            <h3>Монитор запуска</h3>
            <p class="run-id-row">Run ID: <code>{run_id}</code><button id="btnCancelRun" class="btn-cancel-run" onclick="cancelRun()">⛔ Остановить</button><span id="cancelStatus" class="hint"></span></p>
            <div class="progress-line" id="runProgress">Подготовка монитора...</div>
            <h4>Статус</h4>
            <pre id="runStatus">Загрузка...</pre>
            <h4>Поток логов</h4>
            <div class="logs-toolbar">
                <span id="logMeta">Логов: 0</span>
                <label><input type="checkbox" id="autoScrollLogs" checked /> Автопрокрутка</label>
            </div>
            <pre id="liveLogs">Загрузка логов...</pre>
            <h4>Результат ревью</h4>
            <div id="reviewReport"><p class="hint">Ожидание результатов...</p></div>
            <h4>Артефакты</h4>
            <ul id="artifactLinks"><li>Ожидание артефактов...</li></ul>
        </div>
        <script>
            const runId = "{run_id}";

            async function cancelRun() {{
                if (!confirm("Остановить выполнение? Текущий шаг доработает, затем воркфлоу остановится.")) return;
                const btn = document.getElementById("btnCancelRun");
                const statusEl = document.getElementById("cancelStatus");
                btn.disabled = true;
                statusEl.textContent = "Остановка запрошена…";
                try {{
                    const resp = await fetch(`/api/runs/${{runId}}/cancel`, {{ method: "POST" }});
                    const data = await resp.json();
                    if (!resp.ok) {{
                        statusEl.textContent = `Ошибка: ${{data.detail || resp.status}}`;
                        btn.disabled = false;
                        return;
                    }}
                    statusEl.textContent = data.cancel_requested
                        ? "Остановка запрошена — ждём завершения текущего шага…"
                        : (data.message || "Запуск уже завершён.");
                    if (!data.cancel_requested) btn.style.display = "none";
                }} catch (e) {{
                    statusEl.textContent = "Не удалось отправить запрос на остановку.";
                    btn.disabled = false;
                }}
            }}
            let done = false;
            const monitorStartedAt = Date.now();
            const POLL_INTERVAL_MS = 1000;
            const ARTIFACT_POLL_EVERY_TICKS = 5;
            let tickCount = 0;

            function fmtElapsed(ms) {{
                const sec = Math.floor(ms / 1000);
                return `${{Math.floor(sec / 60)}}m ${{sec % 60}}s`;
            }}

            function escapeHtml(s) {{
                const d = document.createElement("div");
                d.textContent = s == null ? "" : String(s);
                return d.innerHTML;
            }}

            function renderReport(data) {{
                const review = data.review || {{}};
                const order = data.category_order || [];
                const labels = data.category_labels || {{}};
                let html = "";

                html += `<p><strong>Спецификация:</strong> ${{escapeHtml((data.spec_page || {{}}).title)}} (id=${{escapeHtml((data.spec_page || {{}}).id)}})</p>`;
                if (data.tech_impl_page) {{
                    html += `<p><strong>Техническая реализация:</strong> ${{escapeHtml(data.tech_impl_page.title)}} (id=${{escapeHtml(data.tech_impl_page.id)}})</p>`;
                }}

                if (review.overall_assessment) {{
                    html += `<h4>Итоговая оценка</h4><p>${{escapeHtml(review.overall_assessment)}}</p>`;
                }}

                for (const key of order) {{
                    const items = review[key] || [];
                    const label = labels[key] || key;
                    html += `<h4>${{escapeHtml(label)}} (${{items.length}})</h4>`;
                    if (!items.length) {{
                        html += `<p class="hint">Пунктов нет.</p>`;
                        continue;
                    }}
                    html += "<ul>";
                    if (key === "well_covered") {{
                        for (const item of items) {{
                            html += `<li>${{escapeHtml(item)}}</li>`;
                        }}
                    }} else {{
                        for (const item of items) {{
                            const title = item.title || "";
                            const desc = item.description || "";
                            html += `<li>${{title ? `<strong>${{escapeHtml(title)}}</strong>` : ""}}${{title && desc ? ": " : ""}}${{escapeHtml(desc)}}</li>`;
                        }}
                    }}
                    html += "</ul>";
                }}
                document.getElementById("reviewReport").innerHTML = html;
            }}

            async function refreshRun() {{
                const runResp = await fetch(`/api/runs/${{runId}}`);
                if (!runResp.ok) return;
                const run = await runResp.json();
                document.getElementById("runStatus").textContent = JSON.stringify(run, null, 2);
                const elapsed = fmtElapsed(Date.now() - monitorStartedAt);
                document.getElementById("runProgress").textContent = `Статус: ${{String(run.status || "unknown").toUpperCase()}} | Прошло: ${{elapsed}}`;

                const logsResp = await fetch(`/api/runs/${{runId}}/logs`);
                if (logsResp.ok) {{
                    const logsData = await logsResp.json();
                    const logs = logsData.logs || [];
                    const lines = logs.map((l) => `${{l.timestamp}} [${{l.level}}] ${{l.message}}`);
                    const logsEl = document.getElementById("liveLogs");
                    logsEl.textContent = lines.join("\\n") || "Логов пока нет";
                    document.getElementById("logMeta").textContent = `Логов: ${{logs.length}}`;
                    if (document.getElementById("autoScrollLogs").checked) logsEl.scrollTop = logsEl.scrollHeight;
                }}

                const isTerminal = ["succeeded", "failed", "canceled"].includes(run.status);
                if (isTerminal) document.getElementById("btnCancelRun").style.display = "none";

                if ((tickCount % ARTIFACT_POLL_EVERY_TICKS === 0) || isTerminal) {{
                    const artifactResp = await fetch(`/api/artifacts/run/${{runId}}`);
                    if (artifactResp.ok) {{
                        const artifacts = await artifactResp.json();
                        const linksEl = document.getElementById("artifactLinks");
                        linksEl.innerHTML = "";
                        for (const item of artifacts) {{
                            const li = document.createElement("li");
                            const a = document.createElement("a");
                            a.href = item.download_url;
                            a.textContent = item.filename;
                            li.appendChild(a);
                            linksEl.appendChild(li);
                        }}

                        const resultArtifact = artifacts.find((a) => a.filename === "workflow_result.json");
                        if (resultArtifact) {{
                            const resultResp = await fetch(resultArtifact.download_url);
                            if (resultResp.ok) {{
                                renderReport(await resultResp.json());
                            }}
                        }}
                    }}
                }}
                if (isTerminal) done = true;
            }}

            async function tick() {{
                tickCount += 1;
                try {{ await refreshRun(); }} catch (e) {{}}
                if (!done) setTimeout(tick, POLL_INTERVAL_MS);
            }}
            tick();
        </script>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Review Test Cases - Triage Bugs Tool (Claude)</title>
        <style>{_REVIEW_STYLE}</style>
    </head>
    <body>
        {_REVIEW_NAV}
        <div class="container">
            <h2>Ревью тест-кейсов на полноту покрытия</h2>
            <p style="margin-bottom: 16px; color: #555;">Проверяет тест-кейсы из приложенного файла для одной User Story
            на полноту покрытия требований, валидаций, альтернативных сценариев и edge-кейсов — сверяясь со
            спецификацией в Confluence (и, для API, с технической реализацией), которые вы указываете ссылкой
            или ID страницы. Только чтение — ничего не пишет ни в Confluence, ни куда-либо ещё.</p>
            {error_block}
            <form method="post" action="/ui/workflows/review_test_cases/run" enctype="multipart/form-data" id="reviewTestCasesForm">
                <div class="form-row">
                    <div class="form-group">
                        <label for="test_type">Тип тест-кейсов</label>
                        <select id="test_type" name="test_type">{type_options}</select>
                    </div>
                    <div class="form-group">
                        <label for="us">Номер User Story</label>
                        <input id="us" name="us" value="{values.get('us', '')}" placeholder="20.1.1 или US-20.1.1" required />
                    </div>
                </div>
                <div class="form-group">
                    <label for="spec_url">Ссылка (или ID страницы) на спецификацию в Confluence</label>
                    <input id="spec_url" name="spec_url" value="{values.get('spec_url', '')}" placeholder="https://.../wiki/spaces/SPACE/pages/123456789/US-20.1.1" required />
                </div>
                <div class="form-group" id="techImplGroup">
                    <label for="tech_impl_url">Ссылка (или ID страницы) на техническую реализацию в Confluence <span id="techImplRequiredHint">(обязательно для API)</span></label>
                    <input id="tech_impl_url" name="tech_impl_url" value="{values.get('tech_impl_url', '')}" placeholder="https://.../wiki/spaces/SPACE/pages/987654321/..." />
                </div>
                <div class="form-group">
                    <label for="test_cases_file">Файл с тест-кейсами для проверки</label>
                    <input id="test_cases_file" type="file" name="test_cases_file" accept=".txt,.csv,.md" required />
                    <p class="hint">Любой текстовый формат (список, CSV-экспорт из Azure DevOps и т.д.), UTF-8 — содержимое передаётся в LLM как есть.</p>
                </div>
                <button type="submit">Запустить ревью</button>
            </form>
            {run_panel}
        </div>
        <script>
            const testTypeEl = document.getElementById("test_type");
            const techImplInput = document.getElementById("tech_impl_url");
            const techImplHint = document.getElementById("techImplRequiredHint");
            function syncTechImplRequired() {{
                const isApi = testTypeEl.value === "api";
                techImplInput.required = isApi;
                techImplHint.style.display = isApi ? "inline" : "none";
            }}
            testTypeEl.addEventListener("change", syncTechImplRequired);
            syncTechImplRequired();
        </script>
    </body></html>
    """


def _render_bug_backlog_audit_page(
    *,
    form_values: dict[str, str] | None = None,
    validation_error: str | None = None,
    run_id: str | None = None,
) -> str:
    values = {"jql": bug_backlog_audit_workflow.DEFAULT_JQL, "max_results": "100"}
    if form_values:
        values.update(form_values)

    error_block = (
        f'<div class="error">Ошибка: {validation_error}</div>' if validation_error else ""
    )

    run_panel = ""
    if run_id:
        run_panel = f"""
        <div class="result-card">
            <h3>Монитор запуска</h3>
            <p class="run-id-row">Run ID: <code>{run_id}</code><button id="btnCancelRun" class="btn-cancel-run" onclick="cancelRun()">⛔ Остановить</button><span id="cancelStatus" class="hint"></span></p>
            <div class="progress-line" id="runProgress">Подготовка монитора...</div>
            <h4>Статус</h4>
            <pre id="runStatus">Загрузка...</pre>
            <h4>Поток логов</h4>
            <div class="logs-toolbar">
                <span id="logMeta">Логов: 0</span>
                <label><input type="checkbox" id="autoScrollLogs" checked /> Автопрокрутка</label>
            </div>
            <pre id="liveLogs">Загрузка логов...</pre>
            <h4>Сводка</h4>
            <p class="hint" id="totalSummary"></p>
            <div class="counters" id="auditCounters" style="grid-template-columns: repeat(4, 1fr);"></div>
            <h4>Результаты</h4>
            <div class="table-scroll">
            <table>
                <thead><tr><th>Баг</th><th>Название</th><th>Тип</th><th>Автор</th><th>Недостающие поля</th><th>Недостающие связи</th><th>Статус</th></tr></thead>
                <tbody id="resultsTableBody"><tr><td colspan="7">Ожидание результатов...</td></tr></tbody>
            </table>
            </div>
            <h4>Артефакты</h4>
            <ul id="artifactLinks"><li>Ожидание артефактов...</li></ul>
        </div>
        <script>
            const runId = "{run_id}";

            async function cancelRun() {{
                if (!confirm("Остановить выполнение? Текущий шаг доработает, затем воркфлоу остановится.")) return;
                const btn = document.getElementById("btnCancelRun");
                const statusEl = document.getElementById("cancelStatus");
                btn.disabled = true;
                statusEl.textContent = "Остановка запрошена…";
                try {{
                    const resp = await fetch(`/api/runs/${{runId}}/cancel`, {{ method: "POST" }});
                    const data = await resp.json();
                    if (!resp.ok) {{
                        statusEl.textContent = `Ошибка: ${{data.detail || resp.status}}`;
                        btn.disabled = false;
                        return;
                    }}
                    statusEl.textContent = data.cancel_requested
                        ? "Остановка запрошена — ждём завершения текущего шага…"
                        : (data.message || "Запуск уже завершён.");
                    if (!data.cancel_requested) btn.style.display = "none";
                }} catch (e) {{
                    statusEl.textContent = "Не удалось отправить запрос на остановку.";
                    btn.disabled = false;
                }}
            }}

            let done = false;
            const monitorStartedAt = Date.now();
            const POLL_INTERVAL_MS = 1000;
            const ARTIFACT_POLL_EVERY_TICKS = 5;
            let tickCount = 0;

            function fmtElapsed(ms) {{
                const sec = Math.floor(ms / 1000);
                return `${{Math.floor(sec / 60)}}m ${{sec % 60}}s`;
            }}

            function escapeHtml(s) {{
                const d = document.createElement("div");
                d.textContent = s == null ? "" : String(s);
                return d.innerHTML;
            }}

            async function refreshRun() {{
                const runResp = await fetch(`/api/runs/${{runId}}`);
                if (!runResp.ok) return;
                const run = await runResp.json();
                document.getElementById("runStatus").textContent = JSON.stringify(run, null, 2);
                const elapsed = fmtElapsed(Date.now() - monitorStartedAt);
                document.getElementById("runProgress").textContent = `Статус: ${{String(run.status || "unknown").toUpperCase()}} | Прошло: ${{elapsed}}`;

                const logsResp = await fetch(`/api/runs/${{runId}}/logs`);
                if (logsResp.ok) {{
                    const logsData = await logsResp.json();
                    const logs = logsData.logs || [];
                    const lines = logs.map((l) => `${{l.timestamp}} [${{l.level}}] ${{l.message}}`);
                    const logsEl = document.getElementById("liveLogs");
                    logsEl.textContent = lines.join("\\n") || "Логов пока нет";
                    document.getElementById("logMeta").textContent = `Логов: ${{logs.length}}`;
                    if (document.getElementById("autoScrollLogs").checked) logsEl.scrollTop = logsEl.scrollHeight;
                }}

                const isTerminal = ["succeeded", "failed", "canceled"].includes(run.status);
                if (isTerminal) document.getElementById("btnCancelRun").style.display = "none";

                if ((tickCount % ARTIFACT_POLL_EVERY_TICKS === 0) || isTerminal) {{
                    const artifactResp = await fetch(`/api/artifacts/run/${{runId}}`);
                    if (artifactResp.ok) {{
                        const artifacts = await artifactResp.json();
                        const linksEl = document.getElementById("artifactLinks");
                        linksEl.innerHTML = "";
                        for (const item of artifacts) {{
                            const li = document.createElement("li");
                            const a = document.createElement("a");
                            a.href = item.download_url;
                            a.textContent = item.filename;
                            li.appendChild(a);
                            linksEl.appendChild(li);
                        }}

                        const resultArtifact = artifacts.find((a) => a.filename === "workflow_result.json");
                        if (resultArtifact) {{
                            const resultResp = await fetch(resultArtifact.download_url);
                            if (resultResp.ok) {{
                                const data = await resultResp.json();
                                const summary = data.summary || {{}};
                                document.getElementById("totalSummary").textContent =
                                    `JQL: ${{summary.jql || ""}}`;

                                const countersEl = document.getElementById("auditCounters");
                                countersEl.innerHTML = `
                                    <div><strong>${{summary.bugs_fetched ?? 0}}</strong><br/>Найдено</div>
                                    <div><strong>${{summary.bugs_checked ?? 0}}</strong><br/>Проверено</div>
                                    <div><strong>${{summary.valid_count ?? 0}}</strong><br/>Без замечаний</div>
                                    <div><strong>${{summary.invalid_count ?? 0}}</strong><br/>С замечаниями</div>
                                `;

                                const rows = data.results || [];
                                const body = document.getElementById("resultsTableBody");
                                body.innerHTML = "";
                                if (!rows.length) {{
                                    body.innerHTML = '<tr><td colspan="7">Багов не найдено.</td></tr>';
                                }}
                                // Show bugs with issues first.
                                const sorted = [...rows].sort((a, b) => (a.is_valid === b.is_valid) ? 0 : (a.is_valid ? 1 : -1));
                                for (const row of sorted) {{
                                    const tr = document.createElement("tr");
                                    tr.className = row.is_valid ? "outcome-ok" : "outcome-err";
                                    const badge = row.is_valid
                                        ? '<span class="badge-pill fixed">без замечаний</span>'
                                        : '<span class="badge-pill not_fixed">есть замечания</span>';
                                    tr.innerHTML = `
                                        <td><a href="${{row.url}}" target="_blank">${{escapeHtml(row.key)}}</a></td>
                                        <td>${{escapeHtml(row.summary)}}</td>
                                        <td>${{escapeHtml(row.issuetype)}}</td>
                                        <td>${{escapeHtml(row.reporter)}}</td>
                                        <td>${{escapeHtml((row.missing_fields || []).join(", "))}}</td>
                                        <td>${{escapeHtml((row.missing_links || []).join(", "))}}</td>
                                        <td>${{badge}}</td>
                                    `;
                                    body.appendChild(tr);
                                }}
                            }}
                        }}
                    }}
                }}
                if (isTerminal) done = true;
            }}

            async function tick() {{
                tickCount += 1;
                try {{ await refreshRun(); }} catch (e) {{}}
                if (!done) setTimeout(tick, POLL_INTERVAL_MS);
            }}
            tick();
        </script>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bug Backlog Audit - Triage Bugs Tool (Claude)</title>
        <style>{_REVIEW_STYLE}</style>
        <style>
            tr.outcome-ok td {{ background: #f0fff4; }}
            tr.outcome-err td {{ background: #fff5f5; }}
        </style>
    </head>
    <body>
        {_REVIEW_NAV}
        <div class="container">
            <h2>Аудит багов из Backlog</h2>
            <p style="margin-bottom: 16px; color: #555;">Проверяет каждый баг, попадающий под JQL, на заполненность обязательных
            полей (Фаза, Метки, Компоненты, ENV (Полигон), Team) и на наличие связей «is Bug for» или «blocks» как минимум с одной задачей
            типа «История» и как минимум с одной задачей типа «QA». Пример корректно оформленного бага —
            <a href="https://fincabank-kg.atlassian.net/browse/MB-6419" target="_blank">MB-6419</a>. Только чтение — ничего не
            меняет и не пишет в Jira.</p>
            {error_block}
            <form method="post" action="/ui/workflows/bug_backlog_audit/run" id="bugBacklogAuditForm">
                <div class="form-group">
                    <label for="jql">JQL Query</label>
                    <textarea id="jql" name="jql" rows="3">{values.get('jql', '')}</textarea>
                    <p class="hint">Jira Query Language — только тикеты, подходящие под этот запрос, будут проверены.</p>
                </div>
                <div class="form-group">
                    <label for="max_results">Максимум багов</label>
                    <input id="max_results" type="number" min="1" max="500" name="max_results" value="{values.get('max_results', '100')}" />
                </div>
                <button type="submit">Запустить аудит</button>
            </form>
            {run_panel}
        </div>
    </body></html>
    """


@router.get("", response_class=HTMLResponse)
async def workflows_page(request: Request) -> str:
    """Workflows page"""
    workflow_rows = ""

    for workflow in WorkflowType:
        workflow_rows += f"""
        <tr>
            <td>{workflow.value}</td>
            <td>{workflow.value.replace('_', ' ').title()}</td>
            <td><a href="/ui/workflows/{workflow.value}/run">Run Workflow</a></td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Workflows - Triage Bugs Tool (Claude)</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f5; }}
            nav {{ background: #0066cc; color: white; padding: 15px 30px; }}
            nav h1 {{ margin: 0; font-size: 24px; }}
            nav ul {{ list-style: none; display: flex; gap: 20px; margin-top: 10px; }}
            nav a {{ color: white; text-decoration: none; }}
            .container {{ max-width: 1000px; margin: 0 auto; padding: 20px; }}
            h2 {{ color: #333; margin: 20px 0 10px 0; }}
            table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #eee; }}
            th {{ background: #f9f9f9; font-weight: bold; }}
            a {{ color: #0066cc; text-decoration: none; }}
        </style>
    </head>
    <body>
        <nav>
            <h1>🐛 Triage Bugs Tool (Claude)</h1>
            <ul>
                <li><a href="/ui">Dashboard</a></li>
                <li><a href="/ui/runs">Runs</a></li>
                <li><a href="/ui/integrations">Integrations</a></li>
            </ul>
        </nav>

        <div class="container">
            <h2>Available Workflows</h2>
            <p>Select a workflow to run</p>

            <table>
                <thead>
                    <tr>
                        <th>Key</th>
                        <th>Name</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    {workflow_rows}
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """


def _render_uat_bug_test_cases_page(
    *,
    form_values: dict[str, str] | None = None,
    validation_error: str | None = None,
    run_id: str | None = None,
) -> str:
    values = {
        "jql": uat_bug_test_cases_workflow.DEFAULT_JQL,
        "plan_id": uat_bug_test_cases_workflow.DEFAULT_PLAN_ID,
        "root_suite_id": uat_bug_test_cases_workflow.DEFAULT_ROOT_SUITE_ID,
        "sprint_number": "",
        "max_results": "200",
        "priority": "Medium",
        "apply": "",
    }
    if form_values:
        values.update(form_values)

    error_block = (
        f'<div class="error">Ошибка: {validation_error}</div>' if validation_error else ""
    )

    priority_options = ""
    for p in ("High", "Medium", "Low"):
        selected = "selected" if values.get("priority") == p else ""
        priority_options += f'<option value="{p}" {selected}>{p}</option>'

    run_panel = ""
    if run_id:
        run_panel = f"""
        <div class="result-card">
            <h3>Монитор запуска</h3>
            <p class="run-id-row">Run ID: <code>{run_id}</code><button id="btnCancelRun" class="btn-cancel-run" onclick="cancelRun()">⛔ Остановить</button><span id="cancelStatus" class="hint"></span></p>
            <div class="progress-line" id="runProgress">Подготовка монитора...</div>
            <h4>Статус</h4>
            <pre id="runStatus">Загрузка...</pre>
            <h4>Поток логов</h4>
            <div class="logs-toolbar">
                <span id="logMeta">Логов: 0</span>
                <label><input type="checkbox" id="autoScrollLogs" checked /> Автопрокрутка</label>
            </div>
            <pre id="liveLogs">Загрузка логов...</pre>
            <h4>Сводка</h4>
            <p class="hint" id="totalSummary"></p>
            <div class="counters" id="uatCounters" style="grid-template-columns: repeat(4, 1fr);"></div>
            <h4 id="resultsHeading">Тест-кейсы</h4>
            <div id="bulkApplyBar" style="display:none; margin-bottom:8px;">
                <button id="btnApplyAll" class="btn-apply-all" onclick="applyAll()">✅ Применить всё (создать в Azure DevOps)</button>
                <span id="bulkApplyStatus" style="margin-left:12px; font-size:13px;"></span>
            </div>
            <div class="table-scroll">
            <table>
                <thead><tr><th>Баг</th><th>Название</th><th>Шагов</th><th>Статус</th><th id="applyColHeader"></th></tr></thead>
                <tbody id="resultsTableBody"><tr><td colspan="5">Ожидание результатов...</td></tr></tbody>
            </table>
            </div>
            <h4>Артефакты</h4>
            <ul id="artifactLinks"><li>Ожидание артефактов...</li></ul>
        </div>
        <script>
            const runId = "{run_id}";

            async function cancelRun() {{
                if (!confirm("Остановить выполнение? Текущий шаг доработает, затем воркфлоу остановится.")) return;
                const btn = document.getElementById("btnCancelRun");
                const statusEl = document.getElementById("cancelStatus");
                btn.disabled = true;
                statusEl.textContent = "Остановка запрошена…";
                try {{
                    const resp = await fetch(`/api/runs/${{runId}}/cancel`, {{ method: "POST" }});
                    const data = await resp.json();
                    if (!resp.ok) {{
                        statusEl.textContent = `Ошибка: ${{data.detail || resp.status}}`;
                        btn.disabled = false;
                        return;
                    }}
                    statusEl.textContent = data.cancel_requested
                        ? "Остановка запрошена — ждём завершения текущего шага…"
                        : (data.message || "Запуск уже завершён.");
                    if (!data.cancel_requested) btn.style.display = "none";
                }} catch (e) {{
                    statusEl.textContent = "Не удалось отправить запрос на остановку.";
                    btn.disabled = false;
                }}
            }}

            let done = false;
            let runFinished = false;
            let lastRows = [];
            const monitorStartedAt = Date.now();
            const POLL_INTERVAL_MS = 1000;
            const ARTIFACT_POLL_EVERY_TICKS = 5;
            let tickCount = 0;

            function fmtElapsed(ms) {{
                const sec = Math.floor(ms / 1000);
                return `${{Math.floor(sec / 60)}}m ${{sec % 60}}s`;
            }}

            function escapeHtml(s) {{
                const d = document.createElement("div");
                d.textContent = s == null ? "" : String(s);
                return d.innerHTML;
            }}

            async function _callApply(bugKeys) {{
                const statusEl = document.getElementById("bulkApplyStatus");
                const resp = await fetch("/api/workflows/uat-bug-test-cases/apply-cases", {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{ run_id: runId, bug_keys: bugKeys }}),
                }});
                if (!resp.ok) {{
                    const err = await resp.text();
                    statusEl.textContent = `Ошибка: ${{err}}`;
                    return null;
                }}
                return await resp.json();
            }}

            async function applyOne(bugKey, btn) {{
                if (!confirm(`Создать тест-кейс в Azure DevOps для ${{bugKey}}?`)) return;
                btn.disabled = true;
                btn.textContent = "...";
                const result = await _callApply([bugKey]);
                if (!result) {{ btn.textContent = "Ошибка"; btn.disabled = false; return; }}
                if (result.applied && result.applied.includes(bugKey)) {{
                    btn.textContent = "✅ Создан";
                    btn.style.background = "#198754";
                }} else {{
                    const err = (result.errors || []).find(e => e.key === bugKey);
                    btn.textContent = "❌ Ошибка";
                    btn.title = err?.error || "";
                    btn.style.background = "#dc3545";
                    btn.disabled = false;
                }}
            }}

            async function applyAll() {{
                const keys = lastRows
                    .filter(r => !(r.result && r.result.success))
                    .map(r => r.key);
                if (!keys.length) return;
                if (!confirm(`Создать ${{keys.length}} тест-кейс(ов) в Azure DevOps?`)) return;
                const statusEl = document.getElementById("bulkApplyStatus");
                const allBtn = document.getElementById("btnApplyAll");
                allBtn.disabled = true;
                statusEl.textContent = "Применяю...";
                const result = await _callApply(null);
                if (!result) {{ statusEl.textContent = "Запрос не выполнен."; allBtn.disabled = false; return; }}
                statusEl.textContent = `✅ Создано: ${{result.applied_count}} | ❌ Ошибок: ${{result.error_count}}`;
                document.querySelectorAll(".btn-apply-row").forEach(b => {{
                    const key = b.closest("tr")?.dataset?.key;
                    if (result.applied && result.applied.includes(key)) {{
                        b.textContent = "✅ Создан"; b.disabled = true; b.style.background = "#198754";
                    }} else if (result.errors && result.errors.some(e => e.key === key)) {{
                        b.textContent = "❌ Ошибка"; b.disabled = true; b.style.background = "#dc3545";
                        allBtn.disabled = false;
                    }}
                }});
            }}

            async function refreshRun() {{
                const runResp = await fetch(`/api/runs/${{runId}}`);
                if (!runResp.ok) return;
                const run = await runResp.json();
                document.getElementById("runStatus").textContent = JSON.stringify(run, null, 2);
                const elapsed = fmtElapsed(Date.now() - monitorStartedAt);
                document.getElementById("runProgress").textContent = `Статус: ${{String(run.status || "unknown").toUpperCase()}} | Прошло: ${{elapsed}}`;

                const logsResp = await fetch(`/api/runs/${{runId}}/logs`);
                if (logsResp.ok) {{
                    const logsData = await logsResp.json();
                    const logs = logsData.logs || [];
                    const lines = logs.map((l) => `${{l.timestamp}} [${{l.level}}] ${{l.message}}`);
                    const logsEl = document.getElementById("liveLogs");
                    logsEl.textContent = lines.join("\\n") || "Логов пока нет";
                    document.getElementById("logMeta").textContent = `Логов: ${{logs.length}}`;
                    if (document.getElementById("autoScrollLogs").checked) logsEl.scrollTop = logsEl.scrollHeight;
                }}

                const isTerminal = ["succeeded", "failed", "canceled"].includes(run.status);
                if (isTerminal) {{
                    document.getElementById("btnCancelRun").style.display = "none";
                    runFinished = true;
                }}

                if ((tickCount % ARTIFACT_POLL_EVERY_TICKS === 0) || isTerminal) {{
                    const artifactResp = await fetch(`/api/artifacts/run/${{runId}}`);
                    if (artifactResp.ok) {{
                        const artifacts = await artifactResp.json();
                        const linksEl = document.getElementById("artifactLinks");
                        linksEl.innerHTML = "";
                        for (const item of artifacts) {{
                            const li = document.createElement("li");
                            const a = document.createElement("a");
                            a.href = item.download_url;
                            a.textContent = item.filename;
                            li.appendChild(a);
                            linksEl.appendChild(li);
                        }}

                        const resultArtifact = artifacts.find((a) => a.filename === "workflow_result.json");
                        if (resultArtifact) {{
                            const resultResp = await fetch(resultArtifact.download_url);
                            if (resultResp.ok) {{
                                const data = await resultResp.json();
                                const summary = data.summary || {{}};
                                const target = summary.target_suite || {{}};
                                const isDryRun = !!summary.dry_run;

                                document.getElementById("totalSummary").textContent =
                                    `Целевой suite: ${{target.sprint_name || "—"}} → ${{target.suite_name || "—"}}`;

                                document.getElementById("uatCounters").innerHTML = `
                                    <div><strong>${{summary.passed_bugs ?? 0}}</strong><br/>В статусе PASSED</div>
                                    <div><strong>${{summary.existing_in_suite ?? 0}}</strong><br/>Уже в suite</div>
                                    <div><strong>${{summary.new_bugs ?? 0}}</strong><br/>Новых</div>
                                    <div><strong>${{isDryRun ? (summary.processed ?? 0) : (summary.created_count ?? 0)}}</strong><br/>${{isDryRun ? "В предпросмотре" : "Создано"}}</div>
                                `;

                                document.getElementById("resultsHeading").textContent = isDryRun
                                    ? `Предпросмотр (${{summary.processed ?? 0}} тест-кейс(ов) — в Azure DevOps ничего не записано)`
                                    : `Результаты (создано: ${{summary.created_count ?? 0}}, ошибок: ${{summary.failed_count ?? 0}})`;

                                const rows = data.results || [];
                                lastRows = rows;
                                const showApplyCol = isDryRun && runFinished;
                                document.getElementById("applyColHeader").textContent = showApplyCol ? "Действие" : "";

                                const body = document.getElementById("resultsTableBody");
                                body.innerHTML = "";
                                if (!rows.length) {{
                                    body.innerHTML = '<tr><td colspan="5">Новых багов без тест-кейса не найдено.</td></tr>';
                                }}
                                for (const row of rows) {{
                                    const res = row.result || {{}};
                                    let statusText = "";
                                    if (res.success) statusText = `✅ создан (id=${{res.case_id}})`;
                                    else if (res.success === false) statusText = `❌ ошибка: ${{res.error || ""}}`;
                                    else if (isDryRun) statusText = "будет создан";
                                    const tr = document.createElement("tr");
                                    tr.dataset.key = row.key;
                                    const applyCell = (showApplyCol && !res.success)
                                        ? `<td><button class="btn-apply-row" onclick="applyOne('${{row.key}}', this)">Применить</button></td>`
                                        : `<td></td>`;
                                    tr.innerHTML = `
                                        <td><a href="${{row.url}}" target="_blank">${{escapeHtml(row.key)}}</a></td>
                                        <td>${{escapeHtml(row.title)}}</td>
                                        <td>${{(row.steps || []).length}}</td>
                                        <td>${{statusText}}</td>
                                        ${{applyCell}}
                                    `;
                                    body.appendChild(tr);
                                }}
                                if (showApplyCol && rows.some(r => !(r.result && r.result.success))) {{
                                    document.getElementById("bulkApplyBar").style.display = "";
                                }}
                            }}
                        }}
                    }}
                }}
                if (isTerminal) done = true;
            }}

            async function tick() {{
                tickCount += 1;
                try {{ await refreshRun(); }} catch (e) {{}}
                if (!done) setTimeout(tick, POLL_INTERVAL_MS);
            }}
            tick();
        </script>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>UAT Bug Test Cases - Triage Bugs Tool</title>
        <style>{_REVIEW_STYLE}</style>
    </head>
    <body>
        {_REVIEW_NAV}
        <div class="container">
            <h2>Тест-кейсы из UAT-багов</h2>
            <p style="margin-bottom: 16px; color: #555;">Берёт баги заказчика по JQL в статусе «PASSED» (только подтверждённо
            исправленные и готовые к регрессу) и создаёт для каждого нового бага (ещё не представленного в целевом suite)
            тест-кейс в Azure DevOps — с номером бага в названии и описании, кликабельной ссылкой на баг и шагами
            воспроизведения из бага. Дубликаты не создаются — уже существующие в suite баги пропускаются. По умолчанию —
            только предпросмотр, ничего не записывается в Azure DevOps, пока не отмечен чекбокс «Применить».</p>
            {error_block}
            <form method="post" action="/ui/workflows/uat_bug_test_cases/run" id="uatBugTestCasesForm">
                <div class="form-group">
                    <label for="jql">JQL Query</label>
                    <textarea id="jql" name="jql" rows="2">{values.get('jql', '')}</textarea>
                    <p class="hint">Только баги, попадающие под этот запрос и находящиеся в статусе «PASSED», будут рассмотрены.</p>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label for="sprint_number">Спринт (папка Sprint N)</label>
                        <select id="sprint_number" name="sprint_number">
                            <option value="">Загрузка списка спринтов...</option>
                        </select>
                        <p class="hint">Тест-кейсы добавляются в suite «UAT Bugs» внутри выбранного спринта. Папку для нового
                        спринта нужно склонировать вручную из предыдущего перед запуском.</p>
                    </div>
                    <div class="form-group">
                        <label for="priority">Приоритет тест-кейсов</label>
                        <select id="priority" name="priority">{priority_options}</select>
                    </div>
                </div>
                <div class="form-group">
                    <label for="max_results">Максимум багов из Jira</label>
                    <input id="max_results" type="number" min="1" max="500" name="max_results" value="{values.get('max_results', '200')}" />
                </div>
                <details>
                    <summary style="cursor:pointer; margin-bottom: 10px; font-weight: bold; color: #333;">Дополнительные настройки</summary>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="plan_id">Azure DevOps Test Plan ID</label>
                            <input id="plan_id" name="plan_id" value="{values.get('plan_id', '')}" />
                        </div>
                        <div class="form-group">
                            <label for="root_suite_id">ID корневого suite (Regression)</label>
                            <input id="root_suite_id" name="root_suite_id" value="{values.get('root_suite_id', '')}" />
                        </div>
                    </div>
                </details>
                <div class="checkbox">
                    <input type="checkbox" id="apply" name="apply" {"checked" if _bool_from_form(values.get('apply')) else ""} />
                    <label for="apply">Применить (реально создать тест-кейсы в Azure DevOps — если не отмечено, будет только предпросмотр)</label>
                </div>
                <button type="submit">Запустить</button>
            </form>
            {run_panel}
        </div>
        <script>
            async function loadSprints() {{
                const select = document.getElementById("sprint_number");
                const preselect = "{values.get('sprint_number', '')}";
                try {{
                    const params = new URLSearchParams({{
                        plan_id: document.getElementById("plan_id").value,
                        root_suite_id: document.getElementById("root_suite_id").value,
                    }});
                    const resp = await fetch(`/api/workflows/uat-bug-test-cases/sprints?${{params}}`);
                    const data = await resp.json();
                    const sprints = data.sprints || [];
                    select.innerHTML = "";
                    if (!sprints.length) {{
                        select.innerHTML = '<option value="">Спринты не найдены</option>';
                        return;
                    }}
                    for (const s of sprints) {{
                        const opt = document.createElement("option");
                        opt.value = String(s.sprint_number);
                        opt.textContent = s.has_uat_bugs_suite
                            ? s.sprint_name
                            : `${{s.sprint_name}} (нет suite "UAT Bugs")`;
                        if (!s.has_uat_bugs_suite) opt.disabled = true;
                        select.appendChild(opt);
                    }}
                    if (preselect) {{
                        select.value = preselect;
                    }} else {{
                        const firstReady = sprints.find((s) => s.has_uat_bugs_suite);
                        if (firstReady) select.value = String(firstReady.sprint_number);
                    }}
                }} catch (e) {{
                    select.innerHTML = '<option value="">Не удалось загрузить список спринтов</option>';
                }}
            }}
            loadSprints();
            document.getElementById("uatBugTestCasesForm").addEventListener("submit", (e) => {{
                const applyEl = document.getElementById("apply");
                if (applyEl.checked) {{
                    const ok = window.confirm("Вы собираетесь СОЗДАТЬ тест-кейсы в Azure DevOps. Продолжить?");
                    if (!ok) e.preventDefault();
                }}
            }});
        </script>
    </body></html>
    """


@router.get("/{workflow_key}/run", response_class=HTMLResponse)
async def run_workflow_page(request: Request, workflow_key: str) -> str:
    """Workflow run form"""
    try:
        WorkflowType(workflow_key)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown workflow: {workflow_key}")

    run_id = request.query_params.get("run_id")
    if workflow_key == "review_pull_request":
        return _render_review_pull_request_page(run_id=run_id)
    if workflow_key == "review_comment_fixes":
        return _render_review_comment_fixes_page(run_id=run_id)
    if workflow_key == "upload_test_cases":
        return _render_upload_test_cases_page(run_id=run_id)
    if workflow_key == "skipped_tests_audit":
        return _render_skipped_tests_audit_page(run_id=run_id)
    if workflow_key == "review_test_cases":
        return _render_review_test_cases_page(run_id=run_id)
    if workflow_key == "bug_backlog_audit":
        return _render_bug_backlog_audit_page(run_id=run_id)
    if workflow_key == "uat_bug_test_cases":
        return _render_uat_bug_test_cases_page(run_id=run_id)
    return _render_triage_bugs_page(run_id=run_id)


@router.post("/triage_bugs/run", response_class=HTMLResponse)
async def run_triage_bugs_submit(request: Request, db: Session = Depends(get_db)):
    """Form submit endpoint for triage_bugs workflow UI."""
    form = await request.form()
    form_values = {k: str(v) for k, v in form.items()}

    payload = {
        "jql": str(form.get("jql") or "").strip() or 'issuetype in ("BE BUG", "Mobile bug", Bug, "FE bug") AND status = Backlog',
        "max_results": int(str(form.get("max_results") or "50").strip() or "50"),
        "apply": _bool_from_form(form.get("apply")),
        "add_comment": _bool_from_form(form.get("add_comment")),
        "severity_field_id": str(form.get("severity_field_id") or "customfield_10865").strip(),
        "impact_field_id": str(form.get("impact_field_id") or "customfield_10004").strip(),
        "target_status": str(form.get("target_status") or "Triage").strip(),
        "batch_delay_seconds": int(str(form.get("batch_delay_seconds") or "10").strip() or "10"),
    }

    try:
        validated = TriageBugTicketsRunRequest(**payload)
    except (ValidationError, ValueError) as exc:
        return _render_triage_bugs_page(
            form_values=form_values,
            validation_error=str(exc),
        )

    run_id = str(uuid4())
    run = WorkflowRunModel(
        id=run_id,
        workflow_key="triage_bugs",
        parameters=validated.model_dump(),
        dry_run="0" if validated.apply else "1",
        status="queued",
        created_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    enqueue_workflow(run_id, "triage_bugs")
    return RedirectResponse(url=f"/ui/workflows/triage_bugs/run?run_id={run_id}", status_code=303)


@router.post("/review_pull_request/run", response_class=HTMLResponse)
async def run_review_pull_request_submit(request: Request, db: Session = Depends(get_db)):
    """Form submit endpoint for review_pull_request workflow UI."""
    form = await request.form()
    form_values = {k: str(v) for k, v in form.items()}

    payload = {
        "repo": str(form.get("repo") or "").strip(),
        "pr_id": int(str(form.get("pr_id") or "0").strip() or "0"),
        "project": str(form.get("project") or "").strip() or None,
        "no_anonymize": _bool_from_form(form.get("no_anonymize")),
    }

    try:
        validated = ReviewPullRequestRunRequest(**payload)
    except (ValidationError, ValueError) as exc:
        return _render_review_pull_request_page(form_values=form_values, validation_error=str(exc))

    run_id = str(uuid4())
    run = WorkflowRunModel(
        id=run_id,
        workflow_key="review_pull_request",
        parameters=validated.model_dump(),
        dry_run="1",
        status="queued",
        created_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    enqueue_workflow(run_id, "review_pull_request")
    return RedirectResponse(url=f"/ui/workflows/review_pull_request/run?run_id={run_id}", status_code=303)


@router.post("/review_comment_fixes/run", response_class=HTMLResponse)
async def run_review_comment_fixes_submit(request: Request, db: Session = Depends(get_db)):
    """Form submit endpoint for review_comment_fixes workflow UI."""
    form = await request.form()
    form_values = {k: str(v) for k, v in form.items()}

    payload = {
        "repo": str(form.get("repo") or "").strip(),
        "pr_id": int(str(form.get("pr_id") or "0").strip() or "0"),
        "project": str(form.get("project") or "").strip() or None,
        "no_anonymize": _bool_from_form(form.get("no_anonymize")),
    }

    try:
        validated = ReviewCommentFixesRunRequest(**payload)
    except (ValidationError, ValueError) as exc:
        return _render_review_comment_fixes_page(form_values=form_values, validation_error=str(exc))

    run_id = str(uuid4())
    run = WorkflowRunModel(
        id=run_id,
        workflow_key="review_comment_fixes",
        parameters=validated.model_dump(),
        dry_run="1",
        status="queued",
        created_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    enqueue_workflow(run_id, "review_comment_fixes")
    return RedirectResponse(url=f"/ui/workflows/review_comment_fixes/run?run_id={run_id}", status_code=303)


@router.post("/upload_test_cases/run", response_class=HTMLResponse)
async def run_upload_test_cases_submit(request: Request, db: Session = Depends(get_db)):
    """Form submit endpoint for upload_test_cases workflow UI (multipart: includes a CSV file)."""
    form = await request.form()
    form_values = {k: str(v) for k, v in form.items() if k != "csv_file"}

    plan_key = str(form.get("plan_key") or "web")
    plan = upload_workflow.TEST_PLANS.get(plan_key)
    if not plan:
        return _render_upload_test_cases_page(
            form_values=form_values, validation_error=f"Unknown test plan: {plan_key}"
        )

    csv_upload = form.get("csv_file")
    if csv_upload is None or not hasattr(csv_upload, "read"):
        return _render_upload_test_cases_page(
            form_values=form_values, validation_error="Приложите CSV-файл с тест-кейсами."
        )
    csv_bytes = await csv_upload.read()
    try:
        csv_text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _render_upload_test_cases_page(
            form_values=form_values, validation_error="CSV-файл должен быть в кодировке UTF-8."
        )
    if not csv_text.strip():
        return _render_upload_test_cases_page(
            form_values=form_values, validation_error="CSV-файл пустой."
        )

    us = str(form.get("us") or "").strip()
    if not us:
        return _render_upload_test_cases_page(
            form_values=form_values, validation_error="Укажите номер User Story."
        )

    dry_run = not _bool_from_form(form.get("apply"))
    parameters = {
        "us": us,
        "plan_id": plan["plan_id"],
        "csv_text": csv_text,
        "specs_folder": str(form.get("specs_folder") or upload_workflow.DEFAULT_SPECS_FOLDER_TITLE).strip(),
        "admin_specs_folder_id": str(form.get("admin_specs_folder_id") or upload_workflow.DEFAULT_ADMIN_SPECS_FOLDER_ID).strip(),
        "admin_group_title": str(form.get("admin_group_title") or upload_workflow.DEFAULT_ADMIN_GROUP_SUITE_TITLE).strip(),
        "epic_suite_name": str(form.get("epic_suite_name") or "").strip() or None,
        "us_suite_name": str(form.get("us_suite_name") or "").strip() or None,
        "state": str(form.get("state") or upload_workflow.DEFAULT_STATE).strip(),
        "force": str(form.get("existing_mode") or "add").strip() == "replace",
        "dry_run": dry_run,
    }

    run_id = str(uuid4())
    run = WorkflowRunModel(
        id=run_id,
        workflow_key="upload_test_cases",
        parameters=parameters,
        dry_run="1" if dry_run else "0",
        status="queued",
        created_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    enqueue_workflow(run_id, "upload_test_cases")
    return RedirectResponse(url=f"/ui/workflows/upload_test_cases/run?run_id={run_id}", status_code=303)


@router.post("/skipped_tests_audit/run", response_class=HTMLResponse)
async def run_skipped_tests_audit_submit(request: Request, db: Session = Depends(get_db)):
    """Form submit endpoint for skipped_tests_audit workflow UI."""
    form = await request.form()
    form_values = {k: str(v) for k, v in form.items()}

    payload = {
        "tests_root": str(form.get("tests_root") or "").strip() or skipped_tests_workflow.DEFAULT_TESTS_ROOT,
    }

    try:
        validated = SkippedTestsAuditRunRequest(**payload)
    except (ValidationError, ValueError) as exc:
        return _render_skipped_tests_audit_page(form_values=form_values, validation_error=str(exc))

    run_id = str(uuid4())
    run = WorkflowRunModel(
        id=run_id,
        workflow_key="skipped_tests_audit",
        parameters=validated.model_dump(),
        dry_run="1",
        status="queued",
        created_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    enqueue_workflow(run_id, "skipped_tests_audit")
    return RedirectResponse(url=f"/ui/workflows/skipped_tests_audit/run?run_id={run_id}", status_code=303)


@router.post("/review_test_cases/run", response_class=HTMLResponse)
async def run_review_test_cases_submit(request: Request, db: Session = Depends(get_db)):
    """Form submit endpoint for review_test_cases workflow UI (multipart: includes a test cases file)."""
    form = await request.form()
    form_values = {k: str(v) for k, v in form.items() if k != "test_cases_file"}

    test_cases_upload = form.get("test_cases_file")
    if test_cases_upload is None or not hasattr(test_cases_upload, "read"):
        return _render_review_test_cases_page(
            form_values=form_values, validation_error="Приложите файл с тест-кейсами."
        )
    file_bytes = await test_cases_upload.read()
    try:
        test_cases_text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _render_review_test_cases_page(
            form_values=form_values, validation_error="Файл с тест-кейсами должен быть в кодировке UTF-8."
        )
    if not test_cases_text.strip():
        return _render_review_test_cases_page(form_values=form_values, validation_error="Файл с тест-кейсами пустой.")

    payload = {
        "us": str(form.get("us") or "").strip(),
        "test_type": str(form.get("test_type") or "web").strip(),
        "spec_url": str(form.get("spec_url") or "").strip(),
        "tech_impl_url": str(form.get("tech_impl_url") or "").strip() or None,
        "test_cases_text": test_cases_text,
    }

    try:
        validated = ReviewTestCasesRunRequest(**payload)
    except (ValidationError, ValueError) as exc:
        return _render_review_test_cases_page(form_values=form_values, validation_error=str(exc))

    run_id = str(uuid4())
    run = WorkflowRunModel(
        id=run_id,
        workflow_key="review_test_cases",
        parameters=validated.model_dump(),
        dry_run="1",
        status="queued",
        created_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    enqueue_workflow(run_id, "review_test_cases")
    return RedirectResponse(url=f"/ui/workflows/review_test_cases/run?run_id={run_id}", status_code=303)


@router.post("/bug_backlog_audit/run", response_class=HTMLResponse)
async def run_bug_backlog_audit_submit(request: Request, db: Session = Depends(get_db)):
    """Form submit endpoint for bug_backlog_audit workflow UI."""
    form = await request.form()
    form_values = {k: str(v) for k, v in form.items()}

    payload = {
        "jql": str(form.get("jql") or "").strip() or bug_backlog_audit_workflow.DEFAULT_JQL,
        "max_results": int(str(form.get("max_results") or "100").strip() or "100"),
    }

    try:
        validated = BugBacklogAuditRunRequest(**payload)
    except (ValidationError, ValueError) as exc:
        return _render_bug_backlog_audit_page(form_values=form_values, validation_error=str(exc))

    run_id = str(uuid4())
    run = WorkflowRunModel(
        id=run_id,
        workflow_key="bug_backlog_audit",
        parameters=validated.model_dump(),
        dry_run="1",
        status="queued",
        created_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    enqueue_workflow(run_id, "bug_backlog_audit")
    return RedirectResponse(url=f"/ui/workflows/bug_backlog_audit/run?run_id={run_id}", status_code=303)


@router.post("/uat_bug_test_cases/run", response_class=HTMLResponse)
async def run_uat_bug_test_cases_submit(request: Request, db: Session = Depends(get_db)):
    """Form submit endpoint for uat_bug_test_cases workflow UI."""
    form = await request.form()
    form_values = {k: str(v) for k, v in form.items()}

    sprint_raw = str(form.get("sprint_number") or "").strip()
    payload = {
        "jql": str(form.get("jql") or "").strip() or uat_bug_test_cases_workflow.DEFAULT_JQL,
        "plan_id": str(form.get("plan_id") or "").strip() or uat_bug_test_cases_workflow.DEFAULT_PLAN_ID,
        "root_suite_id": str(form.get("root_suite_id") or "").strip() or uat_bug_test_cases_workflow.DEFAULT_ROOT_SUITE_ID,
        "sprint_number": int(sprint_raw) if sprint_raw else None,
        "max_results": int(str(form.get("max_results") or "200").strip() or "200"),
        "priority": str(form.get("priority") or "Medium").strip(),
        "dry_run": not _bool_from_form(form.get("apply")),
    }

    try:
        validated = UatBugTestCasesRunRequest(**payload)
    except (ValidationError, ValueError) as exc:
        return _render_uat_bug_test_cases_page(form_values=form_values, validation_error=str(exc))

    run_id = str(uuid4())
    run = WorkflowRunModel(
        id=run_id,
        workflow_key="uat_bug_test_cases",
        parameters=validated.model_dump(),
        dry_run="1" if validated.dry_run else "0",
        status="queued",
        created_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    enqueue_workflow(run_id, "uat_bug_test_cases")
    return RedirectResponse(url=f"/ui/workflows/uat_bug_test_cases/run?run_id={run_id}", status_code=303)
