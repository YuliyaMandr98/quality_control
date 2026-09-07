"""Azure DevOps integration client (trimmed to what PR review needs)."""

from typing import Any, Optional

import httpx

from packages.common import IntegrationConnectionStatus, IntegrationType, get_logger
from packages.integrations import IntegrationClient

logger = get_logger(__name__)


class AzureDevOpsClient(IntegrationClient):
    """Azure DevOps REST API client"""

    provider_type = IntegrationType.AZURE_DEVOPS

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.org_url = str(config.get("org_url", "")).rstrip("/")
        self.project = config.get("project", "YourProject")
        self.pat = config.get("pat", "")
        self.base_url = f"{self.org_url}/_apis"
        self.project_base_url = f"{self.org_url}/{self.project}/_apis"

    async def test_connection(self) -> tuple[bool, Optional[str]]:
        """Test connection to Azure DevOps"""
        if not self.org_url or not self.pat:
            return False, "Organization URL or PAT (Personal Access Token) not configured"

        try:
            async with httpx.AsyncClient() as client:
                auth = ("", self.pat)  # PAT is passed as password
                response = await client.get(
                    f"{self.base_url}/projects/{self.project}?api-version=7.1",
                    auth=auth,
                    timeout=10,
                )
                if response.status_code == 200:
                    return True, None
                elif response.status_code == 401:
                    return False, "Invalid PAT or unauthorized"
                else:
                    return False, f"HTTP {response.status_code}: {response.text}"
        except httpx.TimeoutException:
            return False, "Connection timeout"
        except Exception as e:
            return False, str(e)

    async def get_health_status(self) -> IntegrationConnectionStatus:
        """Get current health status"""
        if not self.pat:
            return IntegrationConnectionStatus.UNCONFIGURED

        success, _ = await self.test_connection()
        return (
            IntegrationConnectionStatus.HEALTHY
            if success
            else IntegrationConnectionStatus.UNHEALTHY
        )

    async def fetch_suites(self, plan_id: str) -> list[dict[str, Any]]:
        """Fetch all suites (including the root suite) for a test plan."""
        try:
            async with httpx.AsyncClient() as client:
                auth = ("", self.pat)
                response = await client.get(
                    f"{self.project_base_url}/test/Plans/{plan_id}/suites?api-version=5.0",
                    auth=auth,
                    timeout=30,
                )
                if response.status_code == 200:
                    return response.json().get("value", [])
                logger.error(f"Failed to fetch suites for plan {plan_id}: {response.text}")
                return []
        except Exception as e:
            logger.error(f"Error fetching suites for plan {plan_id}: {str(e)}")
            return []

    async def find_suite(
        self, test_plan_id: str, suite_name: str, parent_suite_id: Optional[str] = None
    ) -> Optional[str]:
        """Find an existing static suite by name (optionally scoped to a parent suite).

        Returns the suite id, or None if no such suite exists. Read-only — does
        not create anything, unlike get_or_create_suite().
        """
        async with httpx.AsyncClient() as client:
            auth = ("", self.pat)
            resp = await client.get(
                f"{self.project_base_url}/testplan/Plans/{test_plan_id}/Suites?api-version=7.1",
                auth=auth,
                timeout=30,
            )
            if resp.status_code == 200:
                for suite in resp.json().get("value", []):
                    if suite.get("name") != suite_name:
                        continue
                    if parent_suite_id is not None and str(
                        suite.get("parentSuite", {}).get("id", "")
                    ) != str(parent_suite_id):
                        continue
                    return str(suite["id"])
        return None

    async def get_or_create_suite(
        self, test_plan_id: str, suite_name: str, parent_suite_id: Optional[str]
    ) -> str:
        """Find an existing static suite by name under *parent_suite_id*, or create it."""
        existing = await self.find_suite(test_plan_id, suite_name, parent_suite_id)
        if existing:
            return existing
        async with httpx.AsyncClient() as client:
            auth = ("", self.pat)
            body = {
                "suiteType": "staticTestSuite",
                "name": suite_name,
                "parentSuite": {"id": int(parent_suite_id)} if parent_suite_id else None,
            }
            create_resp = await client.post(
                f"{self.project_base_url}/testplan/Plans/{test_plan_id}/Suites?api-version=7.1",
                auth=auth,
                json=body,
                timeout=30,
            )
            create_resp.raise_for_status()
            return str(create_resp.json()["id"])

    async def fetch_test_cases_for_suite(self, plan_id: str, suite_id: str) -> list[dict[str, Any]]:
        """Fetch test cases linked to a suite."""
        try:
            async with httpx.AsyncClient() as client:
                auth = ("", self.pat)
                response = await client.get(
                    f"{self.project_base_url}/testplan/Plans/{plan_id}/Suites/{suite_id}/TestCase?api-version=7.1",
                    auth=auth,
                    timeout=30,
                )
                if response.status_code == 200:
                    return response.json().get("value", [])
                logger.error(f"Failed to fetch suite test cases: {response.text}")
                return []
        except Exception as e:
            logger.error(f"Error fetching suite test cases: {str(e)}")
            return []

    async def remove_test_cases_from_suite(
        self, plan_id: str, suite_id: str, case_ids: list[str]
    ) -> dict[str, Any]:
        """Remove test cases from a suite - unlinks them, does NOT delete the
        underlying Test Case work item.

        Deliberately not a hard delete: permanently deleting a work item
        requires project-level "Delete work items" permission, which many
        accounts don't have even with a "Full access" PAT scope (PAT scope
        only bounds what the API *can* do, it never grants permissions beyond
        the underlying user's own project permissions). Removing from a suite
        only needs Test Plan permissions, matching what a user can already do
        by hand from the Test Plans UI ("Remove" action).
        """
        if not case_ids:
            return {"success": True, "removed": [], "removed_count": 0, "error": None}

        try:
            async with httpx.AsyncClient() as client:
                auth = ("", self.pat)
                response = await client.delete(
                    f"{self.project_base_url}/testplan/Plans/{plan_id}/Suites/{suite_id}/TestCase",
                    auth=auth,
                    params={"testIds": ",".join(case_ids), "api-version": "7.1"},
                    timeout=30,
                )
                if response.status_code in (200, 204):
                    return {"success": True, "removed": case_ids, "removed_count": len(case_ids), "error": None}
                return {
                    "success": False,
                    "removed": [],
                    "removed_count": 0,
                    "error": response.text,
                }
        except Exception as e:
            logger.error(f"Error removing test cases from suite {suite_id}: {str(e)}")
            return {"success": False, "removed": [], "removed_count": 0, "error": str(e)}

    async def set_test_case_state(self, work_item_id: str, state: str) -> dict[str, Any]:
        """Transition a Test Case work item's System.State (e.g. Design -> Ready)."""
        try:
            async with httpx.AsyncClient() as client:
                auth = ("", self.pat)
                response = await client.patch(
                    f"{self.project_base_url}/wit/workitems/{work_item_id}?api-version=7.1",
                    auth=auth,
                    headers={"Content-Type": "application/json-patch+json"},
                    json=[{"op": "add", "path": "/fields/System.State", "value": state}],
                    timeout=30,
                )
                if response.status_code not in (200, 201):
                    return {"success": False, "error": response.text}
                return {"success": True}
        except Exception as exc:
            logger.error(f"Error in set_test_case_state: {exc}")
            return {"success": False, "error": str(exc)}

    async def create_test_case_in_suite(
        self,
        test_plan_id: str,
        suite_id: str,
        title: str,
        priority: str = "Medium",
        steps: Optional[list[dict[str, Any]]] = None,
        state: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a Test Case work item and link it to a suite."""
        priority_map = {"High": 1, "Medium": 2, "Low": 3}
        priority_int = priority_map.get(priority, 2)

        steps_xml = ""
        if steps:
            step_items = []
            for i, step in enumerate(steps, 1):
                action = str(step.get("action", "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                expected = str(step.get("expected", "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                step_items.append(
                    f'<step id="{i}" type="ValidateStep">'
                    f"<parameterizedString isformatted=\"true\">{action}</parameterizedString>"
                    f"<parameterizedString isformatted=\"true\">{expected}</parameterizedString>"
                    f"<description/></step>"
                )
            steps_xml = f'<steps id="0" last="{len(steps)}">' + "".join(step_items) + "</steps>"

        patch_body = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority_int},
        ]
        if steps_xml:
            patch_body.append({"op": "add", "path": "/fields/Microsoft.VSTS.TCM.Steps", "value": steps_xml})

        try:
            async with httpx.AsyncClient() as client:
                auth = ("", self.pat)
                response = await client.post(
                    f"{self.project_base_url}/wit/workitems/$Test%20Case?api-version=7.1",
                    auth=auth,
                    headers={"Content-Type": "application/json-patch+json"},
                    json=patch_body,
                    timeout=30,
                )
                if response.status_code not in (200, 201):
                    return {"success": False, "error": response.text}
                wi_id = response.json()["id"]

                link_response = await client.post(
                    f"{self.project_base_url}/testplan/Plans/{test_plan_id}/Suites/{suite_id}/TestCase?api-version=7.1",
                    auth=auth,
                    json=[{"workItem": {"id": wi_id}}],
                    timeout=30,
                )
                if link_response.status_code not in (200, 201):
                    logger.warning(f"TC {wi_id} created but not linked to suite {suite_id}: {link_response.text}")

                if state:
                    state_result = await self.set_test_case_state(str(wi_id), state)
                    if not state_result.get("success"):
                        logger.warning(f"TC {wi_id} created but state not set to '{state}': {state_result.get('error')}")
                    return {"success": True, "case_id": str(wi_id), "state_set": state_result.get("success", False)}

                return {"success": True, "case_id": str(wi_id)}
        except Exception as exc:
            logger.error(f"Error in create_test_case_in_suite: {exc}")
            return {"success": False, "error": str(exc)}


__all__ = ["AzureDevOpsClient"]
