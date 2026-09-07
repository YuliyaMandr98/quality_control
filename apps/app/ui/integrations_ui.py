"""Integrations UI: configure Jira, Confluence and Claude (Anthropic) credentials."""

from html import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from apps.app.database import get_session_factory, IntegrationConfigModel
from apps.app.config import get_settings
from packages.common import IntegrationType, get_logger

logger = get_logger(__name__)

router = APIRouter()

_PROVIDER_FIELDS = {
    "jira": [
        ("base_url", "text", "https://yourdomain.atlassian.net"),
        ("email", "text", "your.email@domain.com"),
        ("api_token", "password", "Jira API token"),
    ],
    "confluence": [
        ("base_url", "text", "https://yourdomain.atlassian.net/wiki"),
        ("space", "text", "YOURSPACE"),
        ("email", "text", "your.email@domain.com"),
        ("api_token", "password", "Confluence API token"),
    ],
    "anthropic": [
        ("api_key", "password", "Anthropic API key"),
        ("model", "text", "claude-sonnet-5"),
    ],
    "azure_devops": [
        ("org_url", "text", "https://dev.azure.com/yourorg"),
        ("project", "text", "YourProject"),
        ("pat", "password", "Azure DevOps Personal Access Token (Code: Read & Write)"),
    ],
}

_PROVIDER_LABELS = {
    "jira": "Jira",
    "confluence": "Confluence",
    "anthropic": "Claude (Anthropic)",
    "azure_devops": "Azure DevOps",
}


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
async def integrations_page(request: Request, db: Session = Depends(get_db)) -> str:
    """Integration settings page: one card per provider with a save + test form."""
    cards = ""

    for integration_type in IntegrationType:
        provider = integration_type.value
        config = (
            db.query(IntegrationConfigModel)
            .filter(IntegrationConfigModel.type == integration_type)
            .first()
        )

        status = config.status if config else "unconfigured"
        status_color = {
            "healthy": "#198754",
            "unhealthy": "#dc3545",
            "unconfigured": "#6c757d",
        }.get(status, "#6c757d")
        error_line = (
            f'<p class="error-line">{escape(config.error_message[:500])}</p>'
            if config and config.error_message
            else ""
        )

        fields_html = ""
        for field_name, field_type, placeholder in _PROVIDER_FIELDS[provider]:
            fields_html += f"""
            <div class="form-group">
                <label for="{provider}_{field_name}">{field_name.replace('_', ' ').title()}</label>
                <input id="{provider}_{field_name}" type="{field_type}" placeholder="{placeholder}" />
            </div>
            """

        cards += f"""
        <div class="card">
            <div class="card-header">
                <span>{_PROVIDER_LABELS[provider]}</span>
                <span class="status-badge" style="background:{status_color};">{status.upper()}</span>
            </div>
            <div class="card-body">
                {error_line}
                <p class="hint">Leave a field blank to keep saving over it as empty — values are write-only and never echoed back for security.</p>
                {fields_html}
                <div class="card-actions">
                    <button onclick="saveConfig('{provider}')">Save</button>
                    <button class="secondary" onclick="testConnection('{provider}')">Test Connection</button>
                    <span id="{provider}_status" class="inline-status"></span>
                </div>
            </div>
        </div>
        """

    logger.info("Rendered integrations page")

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Integrations - Triage Bugs Tool (Claude)</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f5; }}
            nav {{ background: #0066cc; color: white; padding: 15px 30px; }}
            nav h1 {{ margin: 0; font-size: 24px; }}
            nav ul {{ list-style: none; display: flex; gap: 20px; margin-top: 10px; }}
            nav a {{ color: white; text-decoration: none; }}
            .container {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
            h2 {{ color: #333; margin: 20px 0 10px 0; }}
            .card {{ background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 18px; overflow: hidden; }}
            .card-header {{ background: #f9f9f9; padding: 14px 18px; border-bottom: 1px solid #eee; font-weight: bold; font-size: 16px; display: flex; justify-content: space-between; align-items: center; }}
            .card-body {{ padding: 18px; }}
            .status-badge {{ color: white; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
            .form-group {{ margin-bottom: 12px; }}
            label {{ display: block; font-weight: 600; margin-bottom: 4px; color: #333; font-size: 13px; }}
            input {{ width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; font-family: inherit; font-size: 14px; }}
            .hint {{ font-size: 12px; color: #777; margin-bottom: 12px; }}
            .error-line {{ font-size: 12px; color: #dc3545; margin-bottom: 10px; }}
            .card-actions {{ display: flex; align-items: center; gap: 10px; margin-top: 10px; }}
            button {{ background: #0066cc; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-size: 13px; }}
            button:hover {{ background: #0052a3; }}
            button.secondary {{ background: #5c6bc0; }}
            button.secondary:hover {{ background: #3949ab; }}
            .inline-status {{ font-size: 13px; color: #333; }}
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
            <h2>Integration Settings</h2>
            <p style="color:#555; margin-bottom: 16px;">
                Configure connections to Jira, Confluence and Claude. Credentials are encrypted at rest.
                Values are never echoed back to the browser — re-enter a field only if you want to change it.
            </p>

            {cards}
        </div>

        <script>
        function collectFields(provider) {{
            const fieldIds = Array.from(document.querySelectorAll(`[id^="${{provider}}_"]`));
            const payload = {{}};
            for (const el of fieldIds) {{
                const key = el.id.replace(`${{provider}}_`, "");
                if (el.value) payload[key] = el.value;
            }}
            return payload;
        }}

        async function saveConfig(provider) {{
            const statusEl = document.getElementById(`${{provider}}_status`);
            const payload = collectFields(provider);
            if (Object.keys(payload).length === 0) {{
                statusEl.textContent = "Fill in at least one field before saving.";
                return;
            }}
            statusEl.textContent = "Saving...";
            try {{
                const resp = await fetch(`/api/integrations/${{provider}}`, {{
                    method: "PUT",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify(payload),
                }});
                const data = await resp.json();
                statusEl.textContent = data.success ? "Saved ✓ (test connection to verify)" : "Save failed";
            }} catch (err) {{
                statusEl.textContent = "Save failed: " + err.message;
            }}
        }}

        async function testConnection(provider) {{
            const statusEl = document.getElementById(`${{provider}}_status`);
            statusEl.textContent = "Testing...";
            try {{
                const resp = await fetch(`/api/integrations/${{provider}}/test`, {{ method: "POST" }});
                const data = await resp.json();
                statusEl.textContent = data.success ? "Connection OK ✓" : `Failed: ${{data.error || "unknown error"}}`;
            }} catch (err) {{
                statusEl.textContent = "Test failed: " + err.message;
            }}
            setTimeout(() => location.reload(), 1200);
        }}
        </script>
    </body>
    </html>
    """
