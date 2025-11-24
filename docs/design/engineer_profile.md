# Engineer Processing Profile Analysis

## 1. Introduction

This document analyzes the feasibility, plausibility, and security aspects of creating an "engineer" processing profile for the Family Assistant application. This profile is intended to be used for debugging and diagnosing issues with the application itself.

## 2. Feasibility and Plausibility

The concept of an "engineer" profile is highly feasible and plausible within the existing architecture. The application already supports multiple processing profiles, each with its own set of tools and configurations. The "Rule of Two" security model, as outlined in `AGENTS.md`, provides a solid foundation for implementing a secure "engineer" profile.

The proposed workflow, where a user can invoke the engineer profile to diagnose an issue (e.g., "/engineer I notice I haven’t been receiving my daily brief, can you find out why not and file an issue?"), is a natural extension of the application's conversational interface.

## 3. Security Analysis

The primary security concern with an "engineer" profile is the potential for prompt injection to lead to data exfiltration or unauthorized state changes. By adhering to the "Rule of Two," we can mitigate these risks. The engineer profile will operate in the **[AB] Untrusted-Readonly Profile**, where it can process untrustworthy user input [A] and access sensitive systems or private data [B], but it will be strictly prohibited from changing state or communicating externally [C], with the sole exception of creating a GitHub issue, which will be a carefully controlled action.

### 3.1. Tool Analysis

The following tools are proposed for the engineer profile:

*   **`list_source_files`**: This tool will list files and directories recursively within the project.
    *   **Security Considerations**: The tool must be restricted to listing files within the project's directory. It should not be able to access files outside of the project's root.
*   **`read_file_chunk`**: This tool will read a specific range of lines from a file.
    *   **Security Considerations**: The tool must be restricted to reading files within the project's directory. It should not be able to access files outside of the project's root.
*   **`search_in_file`**: This tool will search for a specific string within a file and return the line number and content.
    *   **Security Considerations**: The tool must be restricted to searching files within the project's directory. It should not be able to access files outside of the project's root.
*   **`create_github_issue`**: This tool will allow the agent to create a GitHub issue in the project's private repository.
    *   **Security Considerations**: The GitHub API token will be stored securely and will be scoped to only allow issue creation. The tool will not have access to read or modify any other repository data.
*   **`database_readonly_query`**: This tool will provide read-only access to the application's database.
    *   **Security Considerations**: The database connection will be configured with a read-only user. The tool will be designed to prevent any form of data modification or SQL injection.

### 3.2. Risk Mitigation

*   **Prompt Injection**: The system prompt for the engineer profile will be carefully crafted to instruct the agent on its purpose and limitations. It will be explicitly warned against performing any actions that are not directly related to debugging and issue creation.
*   **Data Exfiltration**: The `create_github_issue` tool will be the only external communication channel. The content of the issue will be sanitized to prevent the inclusion of sensitive user data.
*   **Unauthorized State Changes**: All tools will be strictly read-only, with the exception of `create_github_issue`.

## 4. Implementation Details

The implementation will involve the following steps:

1.  **Define the `engineer` profile in `config.yaml`**: This will include the list of allowed tools.
2.  **Create a system prompt in `prompts.yaml`**: This will define the agent's persona and instructions.
3.  **Implement the new tools**: Each tool will be implemented with security as a top priority.
4.  **Add tests**: Unit tests will be added to verify the functionality and security of the new tools.
5.  **Update documentation**: `AGENTS.md` will be updated to include information about the new "engineer" profile.

## 5. Conclusion

The "engineer" processing profile is a feasible and plausible addition to the Family Assistant application. By carefully designing the profile and its tools, we can create a powerful debugging tool that does not compromise the security of the application.
