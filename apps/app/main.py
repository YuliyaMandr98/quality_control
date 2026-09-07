"""FastAPI application entrypoint."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from apps.app.config import get_settings
from apps.app.database import create_all_tables
from packages.common import generate_correlation_id, get_logger
from packages.common import IntegrationType
from packages.integrations import integration_registry
from packages.integrations.anthropic import AnthropicClient
from packages.integrations.azure_devops import AzureDevOpsClient
from packages.integrations.confluence import ConfluenceClient
from packages.integrations.jira import JiraClient

logger = get_logger(__name__)

# Register integration clients
integration_registry.register(IntegrationType.CONFLUENCE, ConfluenceClient)
integration_registry.register(IntegrationType.JIRA, JiraClient)
integration_registry.register(IntegrationType.ANTHROPIC, AnthropicClient)
integration_registry.register(IntegrationType.AZURE_DEVOPS, AzureDevOpsClient)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    logger.info("Starting Triage Bugs Tool (Claude edition)")

    settings = get_settings()

    # Create data directories
    os.makedirs(settings.artifact_storage_path, exist_ok=True)

    # Initialize database (creates tables on first run — no migration step needed)
    create_all_tables(settings.database_url)
    logger.info("Database initialized")

    yield

    logger.info("Shutting down Triage Bugs Tool")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Triage Bugs Tool (Claude edition)",
        description="Standalone Jira bug triage workflow (extracted from Trace2Quality) - powered by Claude instead of Gemini",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.is_development else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request correlation ID middleware
    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        correlation_id = request.headers.get("x-correlation-id", generate_correlation_id())
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        return response

    # Root route
    @app.get("/", response_class=HTMLResponse)
    async def root():
        return """
        <html>
            <head>
                <title>Triage Bugs Tool (Claude)</title>
                <style>
                    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                           margin: 40px; color: #333; }
                    h1 { color: #0066cc; }
                    a { color: #0066cc; text-decoration: none; }
                    a:hover { text-decoration: underline; }
                    code { background: #f5f5f5; padding: 2px 6px; border-radius: 3px; }
                </style>
            </head>
            <body>
                <h1>🚀 Triage Bugs Tool (Claude edition)</h1>
                <p>Jira bug triage via Confluence specs + Claude, plus Claude-based Azure DevOps PR review (standalone extract of Trace2Quality)</p>
                <h2>Quick Links</h2>
                <ul>
                    <li><a href="/ui">💻 Dashboard</a></li>
                    <li><a href="/ui/integrations">🔌 Configure Integrations</a></li>
                    <li><a href="/ui/workflows/triage_bugs/run">🐛 Run Triage</a></li>
                    <li><a href="/ui/workflows/review_pull_request/run">🔍 Review Pull Request</a></li>
                    <li><a href="/ui/workflows/review_comment_fixes/run">✅ Review Comment Fixes</a></li>
                    <li><a href="/ui/workflows/upload_test_cases/run">📤 Upload Test Cases</a></li>
                    <li><a href="/docs">📚 API Documentation (Swagger)</a></li>
                </ul>
            </body>
        </html>
        """

    # Health check endpoint
    @app.get("/health")
    async def health_check():
        return {"status": "ok", "version": "0.1.0"}

    # Import and include API routers
    from apps.app.api import integrations, workflows, runs, artifacts, health

    app.include_router(integrations.router, prefix="/api/integrations", tags=["integrations"])
    app.include_router(workflows.router, prefix="/api/workflows", tags=["workflows"])
    app.include_router(runs.router, prefix="/api/runs", tags=["runs"])
    app.include_router(artifacts.router, prefix="/api/artifacts", tags=["artifacts"])
    app.include_router(health.router, prefix="/api", tags=["health"])

    # Import and include UI routers
    from apps.app.ui import dashboard, workflows_ui, runs_ui, integrations_ui

    app.include_router(dashboard.router, tags=["ui-dashboard"])
    app.include_router(workflows_ui.router, prefix="/ui/workflows", tags=["ui"])
    app.include_router(runs_ui.router, prefix="/ui/runs", tags=["ui"])
    app.include_router(integrations_ui.router, prefix="/ui/integrations", tags=["ui"])

    # Global error handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        logger.error(f"Unhandled exception: {str(exc)}", correlation_id=correlation_id)
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "correlation_id": correlation_id,
            },
        )

    logger.info("FastAPI application created successfully")
    return app


# Create application instance
app = create_app()

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "apps.app.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        reload=settings.is_development,
    )
