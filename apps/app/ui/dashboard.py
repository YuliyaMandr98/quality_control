"""Dashboard UI"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from apps.app.database import get_session_factory, WorkflowRunModel
from apps.app.config import get_settings
from packages.common import get_logger

logger = get_logger(__name__)

router = APIRouter()


def get_db(request: Request) -> Session:
    """Get database session"""
    settings = get_settings()
    SessionLocal = get_session_factory(settings.database_url)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@router.get("/ui", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)) -> str:
    """Dashboard homepage"""
    correlation_id = getattr(request.state, "correlation_id", None)

    # Get recent runs
    recent_runs = (
        db.query(WorkflowRunModel)
        .order_by(WorkflowRunModel.created_at.desc())
        .limit(5)
        .all()
    )

    runs_html = ""
    for run in recent_runs:
        status_color = {
            "queued": "blue",
            "running": "orange",
            "succeeded": "green",
            "failed": "red",
            "canceled": "gray",
        }.get(run.status.value, "gray")

        runs_html += f"""
        <div style="padding: 10px; border-bottom: 1px solid #eee;">
            <strong>{run.workflow_key}</strong>
            <span style="background: {status_color}; color: white; padding: 2px 6px; border-radius: 3px; font-size: 12px;">
                {run.status.value.upper()}
            </span>
            <small>{run.created_at.strftime('%Y-%m-%d %H:%M:%S')}</small>
            <br/>
            <a href="/ui/runs/{run.id}">View Details →</a>
        </div>
        """

    logger.info("Rendered dashboard", correlation_id=correlation_id)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Triage Bugs Tool (Claude) - Dashboard</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f5; }}
            nav {{ background: #0066cc; color: white; padding: 15px 30px; }}
            nav h1 {{ margin: 0; font-size: 24px; }}
            nav ul {{ list-style: none; display: flex; gap: 20px; margin-top: 10px; }}
            nav a {{ color: white; text-decoration: none; }}
            nav a:hover {{ text-decoration: underline; }}
            .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
            h2 {{ color: #333; margin: 20px 0 10px 0; }}
            .card {{ background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .card-header {{ background: #f9f9f9; padding: 15px; border-bottom: 1px solid #eee; font-weight: bold; }}
            .card-content {{ padding: 0; }}
            a {{ color: #0066cc; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .actions-block {{
                background: linear-gradient(135deg, #0052a3 0%, #0b6bc8 55%, #3789da 100%);
                border-radius: 14px;
                padding: 22px;
                margin: 16px 0 24px 0;
                box-shadow: 0 8px 20px rgba(0, 82, 163, 0.22);
                color: white;
            }}
            .actions-title {{ font-size: 22px; font-weight: 700; margin-bottom: 8px; }}
            .actions-subtitle {{ color: #e9f3ff; margin-bottom: 16px; }}
            .action-buttons {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
            .action-buttons.five {{ grid-template-columns: repeat(5, minmax(0, 1fr)); }}
            @media (max-width: 1400px) {{
                .action-buttons.five {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
            }}
            .action-btn {{
                display: block;
                background: rgba(255, 255, 255, 0.15);
                border: 1px solid rgba(255, 255, 255, 0.3);
                color: white;
                text-decoration: none;
                border-radius: 10px;
                padding: 16px;
                min-height: 96px;
                transition: transform 0.15s ease, background 0.15s ease;
            }}
            .action-btn:hover {{
                background: rgba(255, 255, 255, 0.24);
                text-decoration: none;
                transform: translateY(-1px);
            }}
            .action-btn .label {{ display: block; font-size: 18px; font-weight: 700; margin-bottom: 4px; }}
            .action-btn .hint {{ display: block; font-size: 13px; color: #e9f3ff; }}
            @media (max-width: 900px) {{
                .action-buttons {{ grid-template-columns: 1fr; }}
            }}
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
            <h2>Dashboard</h2>
            <p>Standalone Jira bug triage workflow (extracted from Trace2Quality)</p>

            <div class="actions-block">
                <div class="actions-title">Get Started</div>
                <div class="actions-subtitle">Configure your integrations, then run a dry-run.</div>
                <div class="action-buttons five">
                    <a class="action-btn" href="/ui/integrations">
                        <span class="label">Configure Integrations</span>
                        <span class="hint">Set up Jira, Confluence, Claude and Azure DevOps credentials.</span>
                    </a>
                    <a class="action-btn" href="/ui/workflows/triage_bugs/run">
                        <span class="label">Triage Bugs</span>
                        <span class="hint">Analyze defects and classify severity/impact via Claude.</span>
                    </a>
                    <a class="action-btn" href="/ui/workflows/review_pull_request/run">
                        <span class="label">Review Pull Request</span>
                        <span class="hint">Claude reviews an Azure DevOps PR diff and posts line comments.</span>
                    </a>
                    <a class="action-btn" href="/ui/workflows/review_comment_fixes/run">
                        <span class="label">Review Comment Fixes</span>
                        <span class="hint">Verify whether "fixed" review threads were actually addressed.</span>
                    </a>
                    <a class="action-btn" href="/ui/workflows/upload_test_cases/run">
                        <span class="label">Upload Test Cases</span>
                        <span class="hint">Upload a reviewed CSV into the Web/Mobile/API Test Plan in Azure DevOps.</span>
                    </a>
                </div>
            </div>

            <div class="card">
                <div class="card-header">Recent Runs</div>
                <div class="card-content">
                    {runs_html if runs_html else '<div style="padding: 20px; text-align: center; color: #999;">No runs yet</div>'}
                    <div style="padding: 10px;">
                        <a href="/ui/runs">View All Runs →</a>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
