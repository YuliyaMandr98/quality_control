"""Jira integration client"""

from typing import Any, Optional

import httpx

from packages.common import IntegrationConnectionStatus, IntegrationType, get_logger
from packages.integrations import IntegrationClient

logger = get_logger(__name__)


class JiraClient(IntegrationClient):
    """Jira Cloud API client"""

    provider_type = IntegrationType.JIRA

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.base_url = config.get("base_url", "https://yourdomain.atlassian.net")
        self.email = config.get("email", "")
        self.api_token = config.get("api_token", "")

    async def test_connection(self) -> tuple[bool, Optional[str]]:
        """Test connection to Jira"""
        if not self.api_token or not self.email:
            return False, "API token or email not configured"

        try:
            async with httpx.AsyncClient() as client:
                auth = (self.email, self.api_token)
                response = await client.get(
                    f"{self.base_url}/rest/api/3/myself",
                    auth=auth,
                    timeout=10,
                )
                if response.status_code == 200:
                    return True, None
                elif response.status_code == 401:
                    return False, "Invalid API token or email"
                else:
                    return False, f"HTTP {response.status_code}: {response.text}"
        except httpx.TimeoutException:
            return False, "Connection timeout"
        except Exception as e:
            return False, str(e)

    async def get_health_status(self) -> IntegrationConnectionStatus:
        """Get current health status"""
        if not self.api_token or not self.email:
            return IntegrationConnectionStatus.UNCONFIGURED

        success, _ = await self.test_connection()
        return (
            IntegrationConnectionStatus.HEALTHY
            if success
            else IntegrationConnectionStatus.UNHEALTHY
        )

    async def fetch_issues(self, jql: str) -> list[dict[str, Any]]:
        """Fetch issues using JQL query"""
        try:
            async with httpx.AsyncClient() as client:
                auth = (self.email, self.api_token)
                # Jira Cloud moved JQL search to /search/jql. Try the new endpoint first.
                response = await client.get(
                    f"{self.base_url}/rest/api/3/search/jql",
                    auth=auth,
                    params={
                        "jql": jql,
                        "maxResults": 100,
                        "fields": "summary,issuetype,status",
                    },
                    timeout=30,
                )
                if response.status_code == 200:
                    data = response.json()
                    return data.get("issues", [])

                # Fallback for instances that still support the legacy endpoint.
                legacy_response = await client.get(
                    f"{self.base_url}/rest/api/3/search",
                    auth=auth,
                    params={"jql": jql, "maxResults": 100},
                    timeout=30,
                )
                if legacy_response.status_code == 200:
                    data = legacy_response.json()
                    return data.get("issues", [])

                logger.error(f"Failed to fetch issues: {response.text}")
                return []
        except Exception as e:
            logger.error(f"Error fetching issues: {str(e)}")
            return []

    async def create_issue(self, issue_data: dict[str, Any]) -> dict[str, Any]:
        """Create a new issue"""
        try:
            async with httpx.AsyncClient() as client:
                auth = (self.email, self.api_token)
                response = await client.post(
                    f"{self.base_url}/rest/api/3/issue",
                    auth=auth,
                    json=issue_data,
                    timeout=30,
                )
                if response.status_code in (201, 200):
                    data = response.json()
                    return {"success": True, "issue_key": data.get("key"), "issue_id": data.get("id")}
                else:
                    return {"success": False, "error": response.text}
        except Exception as e:
            logger.error(f"Error creating issue: {str(e)}")
            return {"success": False, "error": str(e)}

    async def update_issue(self, issue_key: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update an issue"""
        try:
            async with httpx.AsyncClient() as client:
                auth = (self.email, self.api_token)
                response = await client.put(
                    f"{self.base_url}/rest/api/3/issue/{issue_key}",
                    auth=auth,
                    json=updates,
                    timeout=30,
                )
                if response.status_code in (204, 200):
                    return {"success": True, "issue_key": issue_key}
                else:
                    return {"success": False, "error": response.text}
        except Exception as e:
            logger.error(f"Error updating issue {issue_key}: {str(e)}")
            return {"success": False, "error": str(e)}

    async def fetch_bugs(
        self,
        jql: str,
        max_results: int = 50,
        fields: str = "summary,issuetype,status,description,priority,assignee,created",
    ) -> list[dict[str, Any]]:
        """Fetch bug issues. `fields` is a comma-separated Jira field-id projection,
        overridable by callers that need a different set (e.g. custom fields, links)."""
        collected: list[dict[str, Any]] = []
        page_size = min(100, max_results)
        next_token: Optional[str] = None

        try:
            async with httpx.AsyncClient() as client:
                auth = (self.email, self.api_token)
                while len(collected) < max_results:
                    params: dict[str, Any] = {
                        "jql": jql,
                        "maxResults": min(page_size, max_results - len(collected)),
                        "fields": fields,
                    }
                    if next_token:
                        params["nextPageToken"] = next_token

                    response = await client.get(
                        f"{self.base_url}/rest/api/3/search/jql",
                        auth=auth,
                        params=params,
                        timeout=30,
                    )
                    if response.status_code == 200:
                        data = response.json()
                        batch = data.get("issues", [])
                        if not batch:
                            break
                        collected.extend(batch)
                        next_token = data.get("nextPageToken")
                        if not next_token:
                            break
                    else:
                        logger.error(f"Failed to fetch bugs: {response.text}")
                        break
        except Exception as e:
            logger.error(f"Error fetching bugs: {str(e)}")

        return collected[:max_results]

    async def fetch_issues_by_keys(self, keys: list[str]) -> list[dict[str, Any]]:
        """Look up issues by exact key (status/resolution only) — used to check whether
        bugs referenced from skipped-test comments have already been resolved.

        Batches into chunks of 50 keys per JQL query to keep the query short. Keys that
        don't exist (typo, wrong project, deleted) are simply absent from the result —
        callers should treat a missing key as "not found in Jira".
        """
        if not keys:
            return []

        collected: list[dict[str, Any]] = []
        chunk_size = 50
        try:
            async with httpx.AsyncClient() as client:
                auth = (self.email, self.api_token)
                for i in range(0, len(keys), chunk_size):
                    chunk = keys[i : i + chunk_size]
                    jql = f"key in ({','.join(chunk)})"
                    response = await client.get(
                        f"{self.base_url}/rest/api/3/search/jql",
                        auth=auth,
                        params={
                            "jql": jql,
                            "maxResults": chunk_size,
                            "fields": "summary,status,resolution",
                        },
                        timeout=30,
                    )
                    if response.status_code == 200:
                        collected.extend(response.json().get("issues", []))
                    else:
                        logger.error(f"Failed to fetch issues by keys ({jql!r}): {response.text}")
        except Exception as e:
            logger.error(f"Error fetching issues by keys: {str(e)}")

        return collected

    async def get_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        """Get available status transitions for an issue."""
        try:
            async with httpx.AsyncClient() as client:
                auth = (self.email, self.api_token)
                response = await client.get(
                    f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions",
                    auth=auth,
                    timeout=30,
                )
                if response.status_code == 200:
                    return response.json().get("transitions", [])
                logger.error(f"Failed to get transitions for {issue_key}: {response.text}")
                return []
        except Exception as e:
            logger.error(f"Error getting transitions for {issue_key}: {str(e)}")
            return []

    async def transition_issue(self, issue_key: str, target_status: str) -> bool:
        """Transition an issue to the given status name."""
        transitions = await self.get_transitions(issue_key)
        transition_id = next(
            (t["id"] for t in transitions if t.get("to", {}).get("name") == target_status),
            None,
        )
        if not transition_id:
            logger.warning(f"No transition to '{target_status}' found for {issue_key}")
            return False

        try:
            async with httpx.AsyncClient() as client:
                auth = (self.email, self.api_token)
                response = await client.post(
                    f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions",
                    auth=auth,
                    json={"transition": {"id": transition_id}},
                    timeout=30,
                )
                return response.status_code in (204, 200)
        except Exception as e:
            logger.error(f"Error transitioning {issue_key} to '{target_status}': {str(e)}")
            return False

    async def add_comment(self, issue_key: str, comment_text: str) -> bool:
        """Add a plain-text comment to an issue using Atlassian Document Format."""
        body = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": comment_text}],
                    }
                ],
            }
        }
        try:
            async with httpx.AsyncClient() as client:
                auth = (self.email, self.api_token)
                response = await client.post(
                    f"{self.base_url}/rest/api/3/issue/{issue_key}/comment",
                    auth=auth,
                    json=body,
                    timeout=30,
                )
                return response.status_code in (201, 200)
        except Exception as e:
            logger.error(f"Error adding comment to {issue_key}: {str(e)}")
            return False

    async def fetch_qa_tasks(self) -> list[dict[str, Any]]:
        """Fetch QA tasks (issues labeled as QA tasks)"""
        jql = 'labels = "QA" AND type = "Task"'
        return await self.fetch_issues(jql)


__all__ = ["JiraClient"]
