import os
import requests


def create_github_issue(title: str, body: str) -> str:
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
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
    GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return (
            "Error: GITHUB_TOKEN and GITHUB_REPOSITORY environment variables must be set."
        )

    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {"title": title, "body": body}

    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        response.raise_for_status()
        issue_url = response.json()["html_url"]
        return f"Successfully created issue: {issue_url}"

    except requests.exceptions.RequestException as e:
        return f"Error creating GitHub issue: {e}"


GITHUB_TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "create_github_issue",
            "description": "Creates a new issue in the GitHub repository.",
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
