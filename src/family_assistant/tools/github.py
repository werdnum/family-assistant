"""GitHub issue creation tool for the engineer profile."""

from __future__ import annotations

import os
from typing import Any

import httpx


async def create_github_issue(title: str, body: str) -> str:
    """
    Creates a new issue in the GitHub repository.

    This tool is used to report bugs and suggest enhancements. The issue will be
    created in the repository specified by the GITHUB_REPOSITORY environment
    variable.

    A GitHub token with 'repo' scope is required and must be provided in the
    GITHUB_TOKEN environment variable.

    Args:
        title: The title of the issue.
        body: The body of the issue.

    Returns:
        A message indicating success or failure, including the URL of the
        created issue if successful.
    """
    github_token = os.environ.get("GITHUB_TOKEN")
    github_repository = os.environ.get("GITHUB_REPOSITORY")
    if not github_token or not github_repository:
        return "Error: GITHUB_TOKEN and GITHUB_REPOSITORY environment variables must be set."

    url = f"https://api.github.com/repos/{github_repository}/issues"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {"title": title, "body": body}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=data, timeout=10.0)
            response.raise_for_status()
            issue_url = response.json()["html_url"]
            return f"Successfully created issue: {issue_url}"

    except httpx.HTTPStatusError as e:
        return f"Error creating GitHub issue: HTTP {e.response.status_code} - {e.response.text}"
    except httpx.RequestError as e:
        return f"Error creating GitHub issue: {e}"


# ast-grep-ignore: no-dict-any - Legacy tool definition format
GITHUB_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_github_issue",
            "description": (
                "Creates a new issue in the GitHub repository to report bugs or suggest enhancements. "
                "Requires GITHUB_TOKEN and GITHUB_REPOSITORY environment variables."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The title of the issue.",
                    },
                    "body": {
                        "type": "string",
                        "description": "The body of the issue.",
                    },
                },
                "required": ["title", "body"],
            },
        },
    }
]
