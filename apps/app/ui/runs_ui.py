"""Runs UI"""

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


@router.get("", response_class=HTMLResponse)
async def runs_page(request: Request, db: Session = Depends(get_db)) -> str:
    """Runs list page"""
    runs = (
        db.query(WorkflowRunModel)
        .order_by(WorkflowRunModel.created_at.desc())
        .limit(50)
        .all()
    )

    run_rows = ""
    for run in runs:
        status_color = {
            "queued": "blue",
            "running": "orange",
            "succeeded": "green",
            "failed": "red",
            "canceled": "gray",
        }.get(run.status.value, "gray")

        run_rows += f"""
        <tr>
            <td>{run.id[:8]}...</td>
            <td>{run.workflow_key}</td>
            <td><span style="background: {status_color}; color: white; padding: 4px 8px; border-radius: 3px; font-size: 12px;">{run.status.value.upper()}</span></td>
            <td>{run.created_at.strftime('%Y-%m-%d %H:%M:%S')}</td>
            <td><a href="/ui/runs/{run.id}">View Details</a></td>
        </tr>
        """

    logger.info("Rendered runs page")

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Runs - Triage Bugs Tool (Claude)</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f5; }}
            nav {{ background: #0066cc; color: white; padding: 15px 30px; }}
            nav h1 {{ margin: 0; font-size: 24px; }}
            nav ul {{ list-style: none; display: flex; gap: 20px; margin-top: 10px; }}
            nav a {{ color: white; text-decoration: none; }}
            .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
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
            <h2>Workflow Runs</h2>
            <p>History of workflow executions</p>

            <table>
                <thead>
                    <tr>
                        <th>Run ID</th>
                        <th>Workflow</th>
                        <th>Status</th>
                        <th>Created</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    {run_rows if run_rows else '<tr><td colspan="5" style="text-align: center; color: #999; padding: 40px;">No runs yet</td></tr>'}
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """


@router.get("/{run_id}", response_class=HTMLResponse)
async def run_detail_page(run_id: str, request: Request, db: Session = Depends(get_db)) -> str:
    """Run detail page"""
    run = db.query(WorkflowRunModel).filter(WorkflowRunModel.id == run_id).first()

    if not run:
        return "<html><body><h1>Run not found</h1></body></html>"

    status_color = {
        "queued": "blue",
        "running": "orange",
        "succeeded": "green",
        "failed": "red",
        "canceled": "gray",
    }.get(run.status.value, "gray")

    logger.info(f"Rendered run detail page: {run_id}")

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Run {run_id[:8]} - Triage Bugs Tool (Claude)</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f5; }}
            nav {{ background: #0066cc; color: white; padding: 15px 30px; }}
            nav a {{ color: white; text-decoration: none; }}
            .container {{ max-width: 1000px; margin: 0 auto; padding: 20px; }}
            h2 {{ color: #333; margin: 20px 0 10px 0; }}
            .status {{ background: {status_color}; color: white; padding: 4px 8px; border-radius: 3px; font-size: 12px; font-weight: bold; }}
            .info-box {{ background: white; padding: 15px; border-radius: 8px; margin: 10px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            pre {{ background: #f5f5f5; padding: 10px; border-radius: 4px; overflow-x: auto; }}
            a {{ color: #0066cc; text-decoration: none; }}
        </style>
    </head>
    <body>
        <nav>
            <h1>🐛 Triage Bugs Tool (Claude)</h1>
            <a href="/ui/runs">← Back to Runs</a>
        </nav>

        <div class="container">
            <h2>Run {run_id[:8]}</h2>

            <div class="info-box">
                <strong>Workflow:</strong> {run.workflow_key}<br/>
                <strong>Status:</strong> <span class="status">{run.status.value.upper()}</span><br/>
                <strong>Created:</strong> {run.created_at.strftime('%Y-%m-%d %H:%M:%S')}<br/>
                {f'<strong>Started:</strong> {run.started_at.strftime("%Y-%m-%d %H:%M:%S")}<br/>' if run.started_at else ''}
                {f'<strong>Completed:</strong> {run.completed_at.strftime("%Y-%m-%d %H:%M:%S")}<br/>' if run.completed_at else ''}
                {f'<strong>Error:</strong> {run.error_message}<br/>' if run.error_message else ''}
            </div>

            <h3>Parameters</h3>
            <pre>{run.parameters}</pre>

            <h3>Logs</h3>
            <p><a href="/api/runs/{run_id}/logs">Download Logs (JSON)</a></p>

            <h3>Artifacts</h3>
            <p><a href="/api/artifacts/run/{run_id}">View Artifacts</a></p>
        </div>
    </body>
    </html>
    """
