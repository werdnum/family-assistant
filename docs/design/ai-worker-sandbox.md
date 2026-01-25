# AI Worker Sandbox Design

## Overview

This document describes a system for spawning isolated AI coding agents (Claude Code or Gemini CLI)
as Kubernetes Jobs to handle complex, multi-step tasks that require general-purpose computing
capabilities beyond Family Assistant's current tool-based approach.

### Motivation: Lessons from ClawdBot

[ClawdBot](https://github.com/clawdbot/clawdbot) is a self-hosted AI assistant that demonstrates the
power of giving AI agents direct filesystem and shell access in isolated containers. While Family
Assistant excels at structured data management (semantic search, calendar, Home Assistant
integration), it lacks the general-purpose computing flexibility that makes ClawdBot powerful.

**Key insight**: ClawdBot's killer feature isn't its 13+ messaging integrations—it's that it can
give Claude Code **full filesystem/bash access in isolated containers**. This makes it a
general-purpose computer operator, not just a chatbot with tools.

### Current FA Limitation

```
User: "Write a Python script to analyze my expenses CSV"

Current FA:
1. Can generate code in a message
2. execute_script runs Starlark (sandboxed, limited)
3. No persistent filesystem
4. No way to "just run Python"
```

### Proposed Solution

```
User: "Write a Python script to analyze my expenses CSV"

With AI Worker Sandbox:
1. FA spawns Kubernetes Job with ai-coder image
2. Claude Code/Gemini CLI runs with full bash/filesystem access
3. Worker writes script to persistent workspace
4. Results returned via webhook when complete
5. Script persists for future use
```

### Design Goals

1. **Leverage existing infrastructure**: Use Kubernetes Jobs, Longhorn volumes, gVisor, existing
   ai-coder container image
2. **Maintain security**: Isolated execution with gVisor, network restrictions, resource limits
3. **Preserve FA strengths**: Keep structured data access (notes, calendar, embeddings) while adding
   general-purpose computing
4. **Enable skill accumulation**: Workers can create reusable scripts/artifacts in a persistent
   workspace

## Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Longhorn RWX Volume                           │
│  /workspace/                                                         │
│  ├── shared/              # Shared artifacts, scripts, skills        │
│  │   ├── scripts/         # Reusable Python/shell scripts           │
│  │   ├── data/            # Persistent data files                   │
│  │   └── skills/          # Skill definitions (like notes)          │
│  └── tasks/               # Isolated per-job directories            │
│      ├── task-abc123/                                               │
│      │   ├── prompt.md    # Task description                        │
│      │   ├── context/     # Input files copied from shared/         │
│      │   ├── output/      # Results written by worker               │
│      │   └── status.json  # Completion status + metadata            │
│      └── task-def456/                                               │
└─────────────────────────────────────────────────────────────────────┘
           │                                         ▲
           │ mount (RW)                              │ mount (task dir only)
           ▼                                         │
┌──────────────────────┐                 ┌──────────────────────────────┐
│  Family Assistant     │                 │   K8s Job (gVisor runtime)   │
│  Pod                  │                 │   ai-coder image             │
│                       │ ──creates──▶    │                              │
│  Tools:               │                 │   - Claude Code / Gemini CLI │
│  - spawn_worker       │                 │   - run-task mode            │
│  - read_workspace     │                 │   - /workspace/shared (RO)   │
│  - write_workspace    │                 │   - /task (RW)               │
│  - list_workspace     │                 │   - Structured output hooks  │
│  - read_task_result   │ ◀──webhook───   │   - Network restricted       │
│                       │                 └──────────────────────────────┘
│  Webhook handler:     │
│  - /api/webhooks/     │
│    worker-complete    │
└──────────────────────┘
```

### Component Responsibilities

#### Family Assistant Pod

- **Workspace management**: Read/write files in `/workspace/shared/`
- **Job orchestration**: Create Kubernetes Jobs with appropriate configuration
- **Task tracking**: Store pending worker tasks in database
- **Result processing**: Handle webhook notifications, read task results
- **LLM coordination**: Wake LLM when workers complete to process results

#### AI Worker Jobs

- **Task execution**: Run Claude Code or Gemini CLI in run-task mode
- **Isolated execution**: gVisor sandbox, restricted network access
- **Structured output**: Hooks ensure output follows expected format
- **Completion notification**: POST to webhook when done

#### Longhorn RWX Volume

- **Shared workspace**: Artifacts accessible by both FA and workers
- **Task isolation**: Each job gets its own task directory
- **Persistence**: Scripts and data survive pod restarts
- **Concurrent access**: Multiple workers can run simultaneously

### Data Flow

#### Spawning a Worker

```
1. User: "Analyze my spending data and create a visualization"

2. FA LLM determines worker is needed:
   - Task requires: Python execution, file I/O, matplotlib
   - Not achievable with: Starlark, existing tools

3. FA calls spawn_worker tool:
   spawn_worker(
     task_description="Analyze spending.csv, create spending chart",
     context_files=["data/spending.csv"],
     model="claude",
     timeout_minutes=30
   )

4. spawn_worker implementation:
   a. Generate task_id: "task-abc123"
   b. Create task directory: /workspace/tasks/task-abc123/
   c. Write prompt.md with task description
   d. Copy context files from shared/ to task/context/
   e. Record task in database (pending)
   f. Create Kubernetes Job manifest
   g. Apply Job to cluster
   h. Return task_id to LLM

5. FA LLM responds to user:
   "I've started a worker to analyze your spending data. I'll let you
   know when it's complete."
```

#### Worker Execution

```
1. Kubernetes schedules Job on node with gVisor

2. Worker container starts:
   - Mounts /workspace/shared (read-only)
   - Mounts /workspace/tasks/task-abc123 as /task (read-write)
   - Reads /task/prompt.md

3. AI coder (Claude Code/Gemini CLI) executes:
   - Reads context files from /task/context/
   - Writes Python script to /task/output/analyze_spending.py
   - Runs script, generates chart
   - Writes chart to /task/output/spending_chart.png
   - Writes summary to /task/output/summary.md

4. On completion, worker:
   - Writes /task/status.json with result metadata
   - POSTs to webhook: POST /api/webhooks/worker-complete
     {
       "task_id": "task-abc123",
       "status": "success",
       "duration_seconds": 145,
       "output_files": ["analyze_spending.py", "spending_chart.png", "summary.md"]
     }

5. Container exits, Job marked Complete
```

#### Result Processing

```
1. Webhook handler receives completion notification

2. Handler creates event for event system:
   {
     "event_type": "worker_completed",
     "source": "kubernetes",
     "task_id": "task-abc123",
     "status": "success"
   }

3. Event matches listener (auto-created by spawn_worker):
   - Listener wakes LLM with task context

4. LLM calls read_task_result("task-abc123"):
   Returns:
   {
     "status": "success",
     "duration_seconds": 145,
     "output_files": [
       {"name": "analyze_spending.py", "size": 2048},
       {"name": "spending_chart.png", "size": 45000, "type": "image"},
       {"name": "summary.md", "size": 512}
     ],
     "summary": "Created spending analysis..."
   }

5. LLM reads output files, sends results to user:
   - Displays chart image
   - Provides summary
   - Optionally copies script to shared/ for reuse
```

## End-to-End Use Cases

Understanding concrete user workflows helps identify required tools and integration points.

### Use Case 1: Bank Statement Analysis

**User uploads PDF via Telegram, asks for spending analysis**

```
User: [uploads bank-statement.pdf]
User: "Analyze my spending and create a chart by category"

Current FA:
1. PDF stored as attachment (attachment_id: abc123)
2. LLM sees attachment, can describe it
3. Cannot actually run code to process it
4. Could only offer to "write a script for you"

With AI Worker (unified storage):
1. PDF stored directly at /workspace/attachments/ab/abc123.pdf
2. LLM: spawn_worker(
     task_description="Parse bank-statement.pdf using tabula-py or pdfplumber.
           Categorize transactions. Create matplotlib pie chart.
           Save chart as spending_by_category.png",
     attachment_ids=["abc123"]  # Automatically symlinked to task/context/
   )
   → Creates task, symlinks PDF to task/context/, spawns Job
3. Worker runs: reads context/bank-statement.pdf, generates chart
4. Webhook fires → event wakes LLM
5. LLM: read_task_result("task-xyz")
   → Gets output files, can include image directly in response
6. LLM sends chart image to user with summary
```

**Key simplification**: No explicit copy step needed. Attachment IDs passed directly to
spawn_worker, which creates symlinks automatically.

### Use Case 2: Note Compilation

**User wants summary of all notes about a topic**

```
User: "Compile all my notes about Project Alpha into a summary document"

Flow:
1. LLM: export_notes_to_workspace(
     query="Project Alpha",
     destination="context/project-alpha/"
   )
   → Semantic search finds 12 matching notes
   → Exports as markdown files to /workspace/shared/context/project-alpha/
   → Returns: {"exported": 12, "path": "context/project-alpha/"}

2. LLM: spawn_worker(
     task="Read all notes in context/project-alpha/.
           Create comprehensive summary document.
           Include timeline, key decisions, open items.
           Output as project_alpha_summary.md",
     context_files=["context/project-alpha/"]
   )

3. Worker reads notes, synthesizes, writes summary

4. Webhook → LLM wakes

5. LLM reads summary, presents to user

6. LLM: ingest_from_workspace(
     path="tasks/task-xyz/output/project_alpha_summary.md",
     title="Project Alpha Summary",
     document_type="note"
   )
   → Indexes output as new searchable document
```

**Required tools**:

- `export_notes_to_workspace` - Materialize notes to files for worker
- `ingest_from_workspace` - Add worker output to document index

### Use Case 3: Reusable Script Creation

**User asks for a utility that can be reused**

```
User: "Create a script to resize images to 800px wide"

Flow:
1. LLM: spawn_worker(
     task_description="Create Python script resize_images.py that:
           - Takes input directory and output directory as args
           - Resizes all images to 800px wide (maintaining aspect ratio)
           - Uses Pillow library
           - Include usage examples in docstring",
     save_to_shared=True  # Copy outputs to shared/scripts/ on success
   )

2. Worker creates /task/output/resize_images.py

3. On success, FA copies to /workspace/shared/scripts/resize_images.py

4. LLM confirms: "Created resize_images.py. It's saved for future use."

--- Later ---

User: [uploads vacation_photos.zip, attachment_id: "zip123"]
User: "Resize these for the web"

Flow:
1. LLM: list_workspace("shared/scripts/")
   → Sees resize_images.py exists

2. LLM: spawn_worker(
     task_description="Unzip the photos, run resize_images.py, create output.zip",
     attachment_ids=["zip123"],
     shared_files=["scripts/resize_images.py"]  # Include existing script
   )
   → Symlinks both zip and script to task/context/

3. Worker runs existing script, creates output.zip

4. LLM reads output.zip from task result, sends to user
```

**Insight**: Scripts accumulate in workspace, becoming reusable "skills".

### Use Case 4: Data Transformation

**User wants to clean up and convert a file**

```
User: [uploads messy_data.xlsx via web UI]
User: "Clean this up and convert to CSV"

Flow:
1. File stored as attachment

2. LLM: copy_attachment_to_workspace("xlsx-id", "data/messy_data.xlsx")

3. LLM: spawn_worker(
     task="Read data/messy_data.xlsx.
           Clean: remove empty rows, normalize dates, trim whitespace.
           Output as cleaned_data.csv with summary of changes."
   )

4. Worker processes, outputs:
   - cleaned_data.csv
   - cleaning_report.md (rows removed, columns fixed, etc.)

5. LLM: read_task_result → gets both files

6. LLM could:
   a. create_attachment_from_workspace → send CSV to user
   b. ingest_from_workspace → index CSV for semantic search
   c. write_workspace("data/cleaned_data.csv", ...) → save for later
```

### Use Case 5: Complex Research Task

**User wants analysis requiring web research + computation**

```
User: "Research the top 5 electric vehicles and create a comparison spreadsheet"

Flow:
1. LLM: spawn_worker(
     task="Research top 5 EVs for 2025.
           For each: price, range, charging speed, cargo space, safety rating.
           Create comparison.csv and summary.md with recommendations.
           Note: Use available web access to fetch current specs."
   )

2. Worker (with Claude Code's web capabilities):
   - Searches for current EV specs
   - Compiles data
   - Creates spreadsheet and summary

3. LLM retrieves results, presents comparison to user
```

**Note**: This requires workers to have internet access to AI APIs and potentially web search.
Network policy would need to allow this.

### Use Case 6: Document Batch Processing

**User has multiple documents to process**

```
User: "I've uploaded 5 receipts. Extract the totals and create a summary."

Flow:
1. User uploads: receipt1.pdf through receipt5.pdf (5 attachments)

2. LLM: For each attachment:
   copy_attachment_to_workspace("att-1", "data/receipts/receipt1.pdf")
   ... (or batch copy tool)

3. LLM: spawn_worker(
     task="For each PDF in data/receipts/:
           - Extract merchant, date, total, items
           - Output receipts_summary.csv with all data
           - Output total_spending.md with grand total"
   )

4. Worker uses OCR/PDF extraction, creates summary

5. LLM: ingest_from_workspace(output CSV) → searchable expense data
```

**Potential enhancement**: `copy_attachments_to_workspace` (batch version)

## Bridge Tools

With unified storage on the workspace volume, bridge tools are simpler filesystem operations.
Workers have direct read-only access to `/attachments` and `/documents`, so many operations don't
require explicit copying.

### link_to_task (internal helper)

When `spawn_worker` is called with attachment IDs or document references, the implementation creates
symlinks in the task's context directory:

```python
async def prepare_task_context(
    task_id: str,
    attachment_ids: list[str],
    document_ids: list[str],
    note_query: str | None,
) -> dict:
    """Prepare task context directory with symlinks to source files."""
    task_dir = f"/workspace/tasks/{task_id}/context"
    os.makedirs(task_dir, exist_ok=True)

    linked_files = []

    # Symlink attachments
    for att_id in attachment_ids:
        src = await get_attachment_path(att_id)  # /workspace/attachments/XX/uuid.ext
        filename = await get_attachment_filename(att_id)
        dst = f"{task_dir}/{filename}"
        os.symlink(src, dst)
        linked_files.append({"type": "attachment", "path": filename})

    # Symlink documents
    for doc_id in document_ids:
        src = await get_document_path(doc_id)  # /workspace/documents/uuid_name.ext
        filename = os.path.basename(src)
        dst = f"{task_dir}/{filename}"
        os.symlink(src, dst)
        linked_files.append({"type": "document", "path": filename})

    # Export notes as files (these are generated, not symlinked)
    if note_query:
        notes = await search_notes(note_query)
        for note in notes:
            filename = f"{slugify(note.title)}.md"
            dst = f"{task_dir}/notes/{filename}"
            await write_note_as_markdown(note, dst)
            linked_files.append({"type": "note", "path": f"notes/{filename}"})

    return {"task_id": task_id, "context_files": linked_files}
```

### spawn_worker (enhanced)

The spawn_worker tool now accepts attachment/document IDs directly:

```python
SPAWN_WORKER_TOOL_DEFINITION = {
    # ... existing fields ...
    "parameters": {
        "properties": {
            "task_description": { ... },
            "attachment_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Attachment IDs to make available to the worker. "
                "These will be symlinked into the task's context directory."
            },
            "document_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Document IDs (from search results) to include."
            },
            "note_query": {
                "type": "string",
                "description": "Search query to find notes to export as markdown files."
            },
            # ... rest of parameters ...
        }
    }
}
```

**Example usage**:

```
User: [uploads bank-statement.pdf, attachment_id: "abc123"]
User: "Analyze my spending"

LLM: spawn_worker(
    task_description="Parse the bank statement PDF and create a spending analysis...",
    attachment_ids=["abc123"],
    note_query="budget categories"  # Include relevant notes for context
)
```

### export_notes_to_workspace

Export notes matching a query to workspace files. Still needed because notes are in the database,
not the filesystem.

### export_notes_to_workspace

Export notes matching a query to workspace files for worker access.

```python
EXPORT_NOTES_TO_WORKSPACE_TOOL = {
    "type": "function",
    "function": {
        "name": "export_notes_to_workspace",
        "description": """Export notes matching a search query to the workspace as markdown files.
Use this to give workers access to relevant notes for synthesis, analysis, or reference.

Each note is exported as a separate .md file with frontmatter containing metadata.""",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Semantic search query to find relevant notes."
                },
                "destination_dir": {
                    "type": "string",
                    "description": "Directory in /workspace/shared/ to export to. "
                    "Example: 'context/project-notes/'"
                },
                "max_notes": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of notes to export."
                }
            },
            "required": ["query", "destination_dir"]
        }
    }
}
```

### export_documents_to_workspace

Export indexed documents matching a query to workspace for worker access.

```python
EXPORT_DOCUMENTS_TO_WORKSPACE_TOOL = {
    "type": "function",
    "function": {
        "name": "export_documents_to_workspace",
        "description": """Export indexed documents matching a search query to the workspace.
Use this to give workers access to PDFs, emails, or other documents for processing.

Documents are copied as their original files (PDF, etc.) with a manifest.json
containing metadata.""",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Semantic search query to find relevant documents."
                },
                "destination_dir": {
                    "type": "string",
                    "description": "Directory in /workspace/shared/ to export to."
                },
                "max_documents": {
                    "type": "integer",
                    "default": 10,
                    "description": "Maximum number of documents to export."
                },
                "document_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by type: 'pdf', 'email', 'note'. Empty = all types."
                }
            },
            "required": ["query", "destination_dir"]
        }
    }
}
```

### ingest_from_workspace

Add a file from the workspace to FA's document index for semantic search.

```python
INGEST_FROM_WORKSPACE_TOOL = {
    "type": "function",
    "function": {
        "name": "ingest_from_workspace",
        "description": """Ingest a file from the workspace into Family Assistant's document index.
This makes the file searchable via semantic search and available to the assistant.

Use this for worker outputs that should become part of the knowledge base.""",
        "parameters": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Path to file in workspace. Can be in shared/ or tasks/task-id/output/."
                },
                "title": {
                    "type": "string",
                    "description": "Title for the document in the index."
                },
                "source_type": {
                    "type": "string",
                    "enum": ["worker_output", "generated", "imported"],
                    "default": "worker_output",
                    "description": "Type of document for categorization."
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional metadata to attach (tags, source_task_id, etc.)."
                }
            },
            "required": ["source_path", "title"]
        }
    }
}
```

### create_attachment_from_workspace

Create a sendable attachment from a workspace file.

```python
CREATE_ATTACHMENT_FROM_WORKSPACE_TOOL = {
    "type": "function",
    "function": {
        "name": "create_attachment_from_workspace",
        "description": """Create an attachment from a workspace file so it can be sent in messages.
Use this to send worker output files (images, documents, etc.) to the user.

Returns an attachment_id that can be used with send_message or included in responses.""",
        "parameters": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Path to file in workspace (shared/ or tasks/task-id/output/)."
                },
                "description": {
                    "type": "string",
                    "description": "Description of the attachment for accessibility."
                }
            },
            "required": ["source_path"]
        }
    }
}
```

## Workspace Directory Structure

Based on use cases, the recommended workspace structure:

```
/workspace/
├── shared/                          # Persistent, accessible to FA and workers (RO for workers)
│   ├── scripts/                     # Reusable scripts created by workers
│   │   └── resize_images.py
│   ├── data/                        # User data files (copied from attachments)
│   │   ├── bank-statement.pdf
│   │   └── receipts/
│   ├── context/                     # Exported notes/documents for worker reference
│   │   └── project-alpha/
│   │       ├── note1.md
│   │       └── note2.md
│   ├── skills/                      # Skill definitions (future)
│   │   └── expense-analyzer/
│   │       ├── SKILL.md
│   │       └── main.py
│   └── outputs/                     # Saved worker outputs for reuse
│       └── 2025-01/
│           └── spending_analysis.png
│
└── tasks/                           # Per-task directories (task dir is RW for its worker)
    └── task-abc123/
        ├── prompt.md                # Task description
        ├── context/                 # Files copied for this task
        │   └── bank-statement.pdf
        ├── output/                  # Worker results
        │   ├── analysis.md
        │   └── chart.png
        └── status.json              # Completion status
```

### Directory Lifecycle

| Directory              | Created By | Written By | Cleaned Up          |
| ---------------------- | ---------- | ---------- | ------------------- |
| `shared/scripts/`      | FA/Worker  | Worker     | Manual/never        |
| `shared/data/`         | FA         | FA         | Manual/policy       |
| `shared/context/`      | FA         | FA         | After task complete |
| `shared/outputs/`      | FA         | FA         | Policy-based        |
| `tasks/task-*/`        | FA         | Worker     | After retention     |
| `tasks/task-*/output/` | Worker     | Worker     | With parent         |

## Tool Summary

With unified storage, fewer explicit bridge tools are needed. Most data movement happens
automatically via symlinks when spawning workers.

| Tool                               | Purpose                                     | Phase |
| ---------------------------------- | ------------------------------------------- | ----- |
| **Workspace Operations**           |                                             |       |
| `read_workspace`                   | Read file from shared workspace             | 1     |
| `write_workspace`                  | Write file to shared workspace              | 1     |
| `list_workspace`                   | List workspace directory contents           | 1     |
| **Worker Management**              |                                             |       |
| `spawn_worker`                     | Create K8s Job (handles context linking)    | 2     |
| `read_task_result`                 | Get completed task output                   | 3     |
| `list_worker_tasks`                | List tasks for conversation                 | 3     |
| `cancel_worker_task`               | Cancel running task                         | 4     |
| **Output Handling**                |                                             |       |
| `ingest_from_workspace`            | Add workspace file to document index        | 3     |
| `create_attachment_from_workspace` | Create sendable attachment from task output | 3     |

**Note**: `copy_attachment_to_workspace` and `export_documents_to_workspace` are no longer needed as
separate tools. Attachments and documents already live on the workspace volume, and `spawn_worker`
handles symlinking them into task context via `attachment_ids` and `document_ids` parameters.

`export_notes_to_workspace` is still useful for ad-hoc note exports to shared/, but `spawn_worker`'s
`note_query` parameter handles most cases automatically.

## New Tools

### spawn_worker

Spawns an AI coder worker to complete a complex task.

```python
SPAWN_WORKER_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "spawn_worker",
        "description": """Spawn an AI coder worker (Claude Code or Gemini CLI) to complete
a complex task that requires general-purpose computing capabilities like running Python,
shell commands, or file manipulation.

Use this when:
- Task requires running Python/shell scripts
- Task needs file I/O beyond notes/documents
- Task involves complex data processing or visualization
- Task would take many tool calls to complete manually

Do NOT use this for:
- Simple queries answerable with existing tools
- Tasks that only need note/calendar/document access
- Quick one-off operations

The worker runs in an isolated container with:
- Read access to /workspace/shared/ (persistent files)
- Write access to its task directory
- Claude Code or Gemini CLI with full bash/filesystem access
- Network restricted to cluster-internal only

You will be notified via webhook when the worker completes.
Use read_task_result(task_id) to retrieve the output.""",
        "parameters": {
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "Detailed description of what the worker should accomplish. "
                    "Be specific about expected outputs and success criteria."
                },
                "context_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of files from /workspace/shared/ to copy to the task "
                    "directory. Use paths relative to shared/, e.g., 'data/input.csv'."
                },
                "model": {
                    "type": "string",
                    "enum": ["claude", "gemini"],
                    "default": "claude",
                    "description": "Which AI coder to use. Claude Code recommended for most tasks."
                },
                "timeout_minutes": {
                    "type": "integer",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 120,
                    "description": "Maximum execution time in minutes."
                },
                "save_to_shared": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, copy successful output files to /workspace/shared/ "
                    "for future reuse."
                }
            },
            "required": ["task_description"]
        }
    }
}
```

### read_workspace

Read a file from the shared workspace.

```python
READ_WORKSPACE_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "read_workspace",
        "description": "Read a file from /workspace/shared/. Use this to access persistent "
        "files like scripts, data, or worker outputs that were saved to shared.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to /workspace/shared/, e.g., 'scripts/analyze.py'"
                }
            },
            "required": ["path"]
        }
    }
}
```

### write_workspace

Write a file to the shared workspace.

```python
WRITE_WORKSPACE_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "write_workspace",
        "description": "Write a file to /workspace/shared/. Use this to store data for "
        "workers to access, or to save scripts/outputs for future use.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to /workspace/shared/, e.g., 'data/input.csv'"
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file."
                }
            },
            "required": ["path", "content"]
        }
    }
}
```

### list_workspace

List files in the shared workspace.

```python
LIST_WORKSPACE_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "list_workspace",
        "description": "List files and directories in /workspace/shared/.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "default": "",
                    "description": "Path relative to /workspace/shared/. Empty string for root."
                },
                "recursive": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, list files recursively."
                }
            }
        }
    }
}
```

### read_task_result

Read the result of a completed worker task.

```python
READ_TASK_RESULT_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "read_task_result",
        "description": "Read the result of a completed worker task. Call this after being "
        "notified that a worker has completed.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID returned by spawn_worker."
                },
                "include_file_contents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of output file names to include content for. "
                    "For large files, consider reading them separately."
                }
            },
            "required": ["task_id"]
        }
    }
}
```

## Webhook Contract

### Worker Completion Webhook

**Endpoint**: `POST /api/webhooks/worker-complete`

**Headers**:

- `Content-Type: application/json`
- `X-Webhook-Signature: sha256=<hmac>` (if secrets configured)
- `X-Webhook-Source: kubernetes`

**Request Body**:

```json
{
    "event_type": "worker_completed",
    "source": "kubernetes",
    "task_id": "task-abc123",
    "status": "success",
    "duration_seconds": 145,
    "exit_code": 0,
    "output_files": [
        "analyze_spending.py",
        "spending_chart.png",
        "summary.md"
    ],
    "error_message": null
}
```

**Status Values**:

- `success`: Task completed successfully
- `failed`: Task failed (see error_message)
- `timeout`: Task exceeded timeout
- `cancelled`: Task was cancelled

**Response**: Standard webhook response

```json
{
    "status": "accepted",
    "event_id": "evt-xyz789"
}
```

### Task Status File Format

Written by worker to `/task/status.json`:

```json
{
    "status": "success",
    "started_at": "2025-01-25T10:00:00Z",
    "completed_at": "2025-01-25T10:02:25Z",
    "duration_seconds": 145,
    "exit_code": 0,
    "output_files": [
        {
            "name": "analyze_spending.py",
            "size": 2048,
            "type": "text/x-python"
        },
        {
            "name": "spending_chart.png",
            "size": 45000,
            "type": "image/png"
        }
    ],
    "summary": "Created Python script to analyze spending data. Generated bar chart showing monthly spending by category.",
    "error_message": null,
    "logs_truncated": false
}
```

## Kubernetes Integration

### Job Manifest Template

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: ai-worker-${TASK_ID}
  namespace: ${NAMESPACE}
  labels:
    app: family-assistant-worker
    task-id: ${TASK_ID}
    model: ${MODEL}
spec:
  backoffLimit: 0  # No retries - let FA handle retry logic
  activeDeadlineSeconds: ${TIMEOUT_SECONDS}
  ttlSecondsAfterFinished: 3600  # Clean up after 1 hour
  template:
    metadata:
      labels:
        app: family-assistant-worker
        task-id: ${TASK_ID}
    spec:
      runtimeClassName: gvisor  # Sandboxed execution
      restartPolicy: Never

      # Security context
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000

      containers:
        - name: worker
          image: ${AI_CODER_IMAGE}
          imagePullPolicy: Always

          args:
            - "--run-task"
            - "/task/prompt.md"
            - "--webhook"
            - "${WEBHOOK_URL}"
            - "--task-id"
            - "${TASK_ID}"

          env:
            - name: ANTHROPIC_API_KEY
              valueFrom:
                secretKeyRef:
                  name: ai-worker-secrets
                  key: anthropic-api-key
            - name: GEMINI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: ai-worker-secrets
                  key: gemini-api-key
            - name: WEBHOOK_SECRET
              valueFrom:
                secretKeyRef:
                  name: ai-worker-secrets
                  key: webhook-secret

          resources:
            requests:
              memory: "512Mi"
              cpu: "500m"
            limits:
              memory: "2Gi"
              cpu: "2000m"

          volumeMounts:
            # Shared workspace (read-only)
            - name: workspace
              mountPath: /workspace/shared
              subPath: shared
              readOnly: true

            # Task directory (read-write)
            - name: workspace
              mountPath: /task
              subPath: tasks/${TASK_ID}
              readOnly: false

          # Security restrictions
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
            readOnlyRootFilesystem: false  # Worker needs to write

      volumes:
        - name: workspace
          persistentVolumeClaim:
            claimName: ai-workspace-pvc

      # Network policy applied at namespace level
      # Restricts egress to: Kubernetes API, webhook endpoint, AI provider APIs
```

### Network Policy

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ai-worker-network-policy
  namespace: ${NAMESPACE}
spec:
  podSelector:
    matchLabels:
      app: family-assistant-worker
  policyTypes:
    - Egress
  egress:
    # Allow DNS
    - to:
        - namespaceSelector: {}
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53

    # Allow webhook callback to FA
    - to:
        - podSelector:
            matchLabels:
              app: family-assistant
      ports:
        - protocol: TCP
          port: 8000

    # Allow Anthropic API
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
      ports:
        - protocol: TCP
          port: 443
      # Note: Consider using an egress gateway for tighter control
```

### Persistent Volume Claim

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ai-workspace-pvc
  namespace: ${NAMESPACE}
spec:
  accessModes:
    - ReadWriteMany  # RWX for concurrent access
  storageClassName: longhorn
  resources:
    requests:
      storage: 10Gi
```

## Configuration

### Pydantic Models

```python
# In config_models.py

class WorkerResourceLimits(BaseModel):
    """Resource limits for worker containers."""

    model_config = ConfigDict(extra="forbid")

    memory_request: str = "512Mi"
    memory_limit: str = "2Gi"
    cpu_request: str = "500m"
    cpu_limit: str = "2000m"


class AIWorkerConfig(BaseModel):
    """AI Worker Sandbox configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False

    # Kubernetes settings
    namespace: str = "default"
    ai_coder_image: str = "ghcr.io/example/ai-coder:latest"
    service_account: str = "ai-worker"
    runtime_class: str = "gvisor"

    # Volume settings
    workspace_pvc: str = "ai-workspace-pvc"
    workspace_mount_path: str = "/workspace"

    # Execution settings
    default_timeout_minutes: int = 30
    max_timeout_minutes: int = 120
    max_concurrent_workers: int = 3

    # Resource limits
    resources: WorkerResourceLimits = Field(default_factory=WorkerResourceLimits)

    # Webhook settings
    webhook_path: str = "/api/webhooks/worker-complete"
    webhook_secret: str | None = None  # From env var AI_WORKER_WEBHOOK_SECRET

    # Cleanup settings
    task_retention_hours: int = 48
    job_ttl_seconds: int = 3600


# Add to AppConfig
class AppConfig(BaseModel):
    # ... existing fields ...
    ai_worker_config: AIWorkerConfig = Field(default_factory=AIWorkerConfig)
```

### Environment Variable Mappings

```python
# In config_loader.py

ENV_VAR_MAPPINGS: list[EnvVarMapping] = [
    # ... existing mappings ...

    # AI Worker configuration
    EnvVarMapping("AI_WORKER_ENABLED", "ai_worker_config.enabled", bool),
    EnvVarMapping("AI_WORKER_NAMESPACE", "ai_worker_config.namespace"),
    EnvVarMapping("AI_WORKER_IMAGE", "ai_worker_config.ai_coder_image"),
    EnvVarMapping("AI_WORKER_PVC", "ai_worker_config.workspace_pvc"),
    EnvVarMapping("AI_WORKER_WEBHOOK_SECRET", "ai_worker_config.webhook_secret"),
    EnvVarMapping("AI_WORKER_DEFAULT_TIMEOUT", "ai_worker_config.default_timeout_minutes", int),
    EnvVarMapping("AI_WORKER_MAX_CONCURRENT", "ai_worker_config.max_concurrent_workers", int),
]
```

### YAML Configuration Example

```yaml
# In config.yaml

ai_worker_config:
  enabled: true
  namespace: "family-assistant"
  ai_coder_image: "containers.example.com/ai-coder:v1.0"
  service_account: "ai-worker"
  runtime_class: "gvisor"

  workspace_pvc: "ai-workspace-pvc"

  default_timeout_minutes: 30
  max_timeout_minutes: 120
  max_concurrent_workers: 3

  resources:
    memory_request: "512Mi"
    memory_limit: "2Gi"
    cpu_request: "500m"
    cpu_limit: "2000m"

  task_retention_hours: 48
  job_ttl_seconds: 3600
```

## Database Schema

### Worker Tasks Table

```python
# In storage/workers.py

worker_tasks_table = Table(
    "worker_tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(100), nullable=False, unique=True, index=True),
    Column("conversation_id", String(255), nullable=False, index=True),
    Column("interface_type", String(50), nullable=False),
    Column("user_name", String(255), nullable=True),

    # Task configuration
    Column("model", String(50), nullable=False, default="claude"),
    Column("task_description", Text, nullable=False),
    Column("context_files", JSON, nullable=True),  # List of copied files
    Column("timeout_minutes", Integer, nullable=False, default=30),

    # Status tracking
    Column("status", String(50), nullable=False, default="pending", index=True),
    # pending, submitted, running, success, failed, timeout, cancelled
    Column("job_name", String(255), nullable=True),  # K8s Job name
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("duration_seconds", Integer, nullable=True),

    # Results
    Column("exit_code", Integer, nullable=True),
    Column("output_files", JSON, nullable=True),  # List of output file metadata
    Column("summary", Text, nullable=True),
    Column("error_message", Text, nullable=True),

    # Metadata
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=True, onupdate=func.now()),

    # Indexes
    Index("idx_worker_status", "status"),
    Index("idx_worker_conversation", "conversation_id", "status"),
    Index("idx_worker_created", "created_at"),
)
```

### Repository Methods

```python
# In storage/repositories/workers.py

class WorkerTasksRepository:

    async def create_task(
        self,
        task_id: str,
        conversation_id: str,
        interface_type: str,
        task_description: str,
        model: str = "claude",
        context_files: list[str] | None = None,
        timeout_minutes: int = 30,
        user_name: str | None = None,
    ) -> None:
        """Create a new worker task record."""

    async def get_task(self, task_id: str) -> dict | None:
        """Get task by ID."""

    async def get_tasks_for_conversation(
        self,
        conversation_id: str,
        status: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Get tasks for a conversation."""

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        job_name: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        duration_seconds: int | None = None,
        exit_code: int | None = None,
        output_files: list[dict] | None = None,
        summary: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Update task status and results."""

    async def get_running_tasks_count(self) -> int:
        """Count currently running tasks (for concurrency limit)."""

    async def cleanup_old_tasks(self, retention_hours: int = 48) -> int:
        """Delete old task records."""
```

## Security Model

### Isolation Boundaries

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Trust Boundary                                │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  Family Assistant Pod (Trusted)                                      │
│  ├── Full database access                                            │
│  ├── Full workspace read/write                                       │
│  ├── Kubernetes API access (create jobs)                             │
│  └── Network access                                                  │
│                                                                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  AI Worker Job (Semi-Trusted)                                        │
│  ├── Read-only access to /workspace/shared/                          │
│  ├── Read-write access to /task/ (own task only)                     │
│  ├── No database access                                              │
│  ├── No Kubernetes API access                                        │
│  ├── Limited network (DNS, webhook, AI APIs only)                    │
│  ├── gVisor sandbox (syscall filtering)                              │
│  ├── Resource limits (CPU, memory)                                   │
│  └── Timeout enforcement                                             │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Security Controls

1. **Container Isolation**

   - gVisor runtime class provides syscall-level sandboxing
   - Non-root user execution (UID 1000)
   - Dropped capabilities (no CAP_NET_RAW, etc.)
   - Read-only shared workspace prevents cross-task interference

2. **Network Restrictions**

   - NetworkPolicy limits egress to:
     - DNS (required for any networking)
     - FA webhook endpoint (for completion notification)
     - AI provider APIs (Anthropic, Google)
   - No access to internal cluster services
   - No access to FA's database

3. **Resource Limits**

   - Memory limit prevents OOM attacks on node
   - CPU limit prevents resource monopolization
   - Timeout prevents runaway tasks
   - Concurrency limit prevents job flooding

4. **Data Isolation**

   - Each task gets isolated directory
   - Shared workspace is read-only to workers
   - Task directories cleaned up after retention period
   - Secrets injected via Kubernetes Secrets, not files

5. **Audit Trail**

   - All tasks recorded in database
   - Webhook notifications logged
   - Job events visible in Kubernetes audit log

### Threat Mitigations

| Threat                        | Mitigation                                              |
| ----------------------------- | ------------------------------------------------------- |
| Prompt injection in task      | Worker can only affect its task directory; no DB access |
| Data exfiltration via network | NetworkPolicy restricts egress; only AI APIs allowed    |
| Cross-task interference       | Isolated task directories; shared/ is read-only         |
| Resource exhaustion           | CPU/memory limits; timeout; concurrency limit           |
| Container escape              | gVisor sandbox; dropped capabilities; non-root          |
| Secrets exposure              | Kubernetes Secrets; not in task directory               |
| Webhook spoofing              | HMAC signature verification                             |

## Implementation Plan

### Phase 1: Foundation

**Goal**: Unified storage and basic workspace tools

1. **Storage migration**

   - Configure `attachment_storage_path` to `/workspace/attachments`
   - Configure `document_storage_path` to `/workspace/documents`
   - Create migration script for existing files
   - Update AttachmentRegistry paths

2. **Workspace tools** (no Kubernetes yet)

   - `read_workspace`, `write_workspace`, `list_workspace`
   - Mount workspace PVC to FA pod
   - Test file operations

3. **Database schema**

   - Create `worker_tasks` table
   - Implement repository
   - Add migration

4. **Configuration**

   - Add `AIWorkerConfig` Pydantic model
   - Add environment variable mappings
   - Add to `defaults.yaml`

### Phase 2: Job Orchestration

**Goal**: Spawn and monitor Kubernetes Jobs

1. **Kubernetes client integration**

   - Use `kubernetes` Python package
   - Service account with Job create/get/delete permissions
   - Job manifest templating

2. **spawn_worker tool**

   - Create task directory structure
   - Write prompt.md
   - Copy context files
   - Create and submit Job
   - Record task in database

3. **Job monitoring**

   - Poll job status (or use informers)
   - Handle job completion/failure
   - Update database status

### Phase 3: Webhook Integration

**Goal**: Event-driven completion handling

1. **Webhook endpoint**

   - Add `/api/webhooks/worker-complete` route
   - HMAC signature verification
   - Parse completion payload

2. **Event system integration**

   - Emit `worker_completed` event
   - Auto-create listener on spawn_worker
   - Wake LLM with task context

3. **read_task_result tool**

   - Read status.json
   - List output files
   - Return structured result

### Phase 4: Production Hardening

**Goal**: Reliability and security

1. **Concurrency control**

   - Check running task count before spawn
   - Queue tasks if limit reached
   - Handle job failures gracefully

2. **Cleanup**

   - System task for old task directory cleanup
   - Kubernetes Job TTL
   - Database task record cleanup

3. **Security**

   - NetworkPolicy deployment
   - gVisor validation
   - Webhook secret rotation

4. **Monitoring**

   - Task duration metrics
   - Failure rate tracking
   - Alert on repeated failures

### Phase 5: Skills System (Optional)

**Goal**: Enable skill accumulation like ClawdBot

1. **Skill conventions**

   - `/workspace/shared/skills/<name>/SKILL.md`
   - Skill discovery and listing
   - Skill invocation via workers

2. **Skill management tools**

   - `list_skills`, `get_skill`, `create_skill`
   - LLM can create new skills from successful tasks

## Testing Strategy

### Unit Tests

- Workspace tool file operations
- Job manifest generation
- Webhook payload parsing
- Repository CRUD operations

### Integration Tests

- End-to-end spawn → webhook → result flow
- Concurrent task handling
- Timeout behavior
- Error handling and cleanup

### Security Tests

- Network policy validation
- Resource limit enforcement
- Webhook signature verification
- Task isolation verification

## Comparison: FA + Workers vs ClawdBot

| Aspect            | FA + Workers            | ClawdBot              |
| ----------------- | ----------------------- | --------------------- |
| Structured data   | PostgreSQL + embeddings | Markdown files        |
| Calendar          | CalDAV native           | Via skills            |
| Home automation   | Home Assistant native   | Via skills            |
| General computing | Isolated K8s Jobs       | Container per session |
| Skill system      | Notes + workspace       | SKILL.md + ClawdHub   |
| Voice             | PWA + Asterisk          | Native apps           |
| Messaging         | Telegram, Web           | 13+ platforms         |
| Security model    | Rule of Two + gVisor    | Docker sandboxes      |

**Key advantage**: FA maintains its structured data strengths while gaining ClawdBot's
general-purpose computing flexibility.

## Integration with Existing FA Systems

### Attachment Storage Integration

FA currently stores attachments in two locations:

- **Chat attachments**: Hash-sharded at `/tmp/chat_attachments/XX/uuid.ext`
- **Documents**: At `/mnt/data/files/uuid_filename.ext`

The workspace volume is a **third storage location** that bridges the gap:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      Data Flow Between Storage Systems                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  User Upload                                                             │
│       │                                                                  │
│       ▼                                                                  │
│  ┌─────────────────┐                                                     │
│  │ Chat Attachments │──copy_attachment_to_workspace──▶┌────────────────┐│
│  │ /tmp/chat_...   │                                  │   Workspace    ││
│  └─────────────────┘                                  │   /workspace/  ││
│                                                       │   shared/      ││
│  ┌─────────────────┐                                  │                ││
│  │ Document Index  │──export_documents_to_workspace──▶│   ┌─────────┐  ││
│  │ /mnt/data/files │                                  │   │ Worker  │  ││
│  └─────────────────┘                                  │   │ Process │  ││
│                                                       │   └────┬────┘  ││
│  ┌─────────────────┐                                  │        │       ││
│  │ Notes Database  │──export_notes_to_workspace──────▶│        ▼       ││
│  └─────────────────┘                                  │   ┌─────────┐  ││
│                                                       │   │ Output  │  ││
│                     ◀─────────────────────────────────│   └────┬────┘  ││
│  ingest_from_workspace                                └────────┼───────┘│
│  create_attachment_from_workspace                              │        │
│                                                                │        │
└────────────────────────────────────────────────────────────────┘        │
```

### Volume Mounting Strategy

FA pod needs access to multiple storage locations:

```yaml
# FA Pod volumes
volumes:
  - name: workspace
    persistentVolumeClaim:
      claimName: ai-workspace-pvc  # RWX Longhorn volume

  - name: chat-attachments
    persistentVolumeClaim:
      claimName: chat-attachments-pvc  # Existing attachment storage

  - name: documents
    persistentVolumeClaim:
      claimName: documents-pvc  # Existing document storage

volumeMounts:
  - name: workspace
    mountPath: /workspace

  - name: chat-attachments
    mountPath: /mnt/chat-attachments
    readOnly: true  # FA reads, copies to workspace

  - name: documents
    mountPath: /mnt/data/files
```

### Recommended Architecture: Unified Storage

Store all persistent data on the workspace volume from the start:

```
/workspace/
├── attachments/      # Chat attachments (replaces /tmp/chat_attachments/)
│   └── XX/           # Hash-sharded by first 2 chars of UUID
│       └── uuid.ext
├── documents/        # Indexed documents (replaces /mnt/data/files/)
│   └── uuid_name.ext
├── shared/           # Shared workspace for workers
│   ├── scripts/
│   ├── data/
│   └── skills/
└── tasks/            # Per-task directories
    └── task-abc123/
        ├── prompt.md
        ├── context/  # Symlinks or copies from attachments/documents
        └── output/
```

**Benefits**:

- Single volume to manage and backup
- No cross-volume copies (symlinks work within same volume)
- Attachments directly accessible to workers via subPath mount
- Simpler bridge tools (just symlink/copy within volume)
- Atomic operations within same filesystem

**Migration path**:

1. Update `AttachmentConfig.storage_path` to `/workspace/attachments`
2. Update `document_storage_path` to `/workspace/documents`
3. Migrate existing files (one-time script)
4. Workers mount `/workspace` with appropriate subPaths

### Simplified Bridge Operations

With unified storage, bridge tools become simple filesystem operations:

```python
# copy_attachment_to_workspace becomes:
async def link_attachment_to_task(attachment_id: str, task_id: str) -> str:
    """Create symlink from task context to attachment file."""
    src = f"/workspace/attachments/{attachment_id[:2]}/{attachment_id}.ext"
    dst = f"/workspace/tasks/{task_id}/context/{filename}"
    os.symlink(src, dst)  # or shutil.copy for isolation
    return dst

# No cross-volume copy needed!
```

### Worker Mount Strategy

Workers get read-only access to attachments/documents, read-write to their task:

```yaml
volumeMounts:
  # Read-only access to attachments
  - name: workspace
    mountPath: /attachments
    subPath: attachments
    readOnly: true

  # Read-only access to documents
  - name: workspace
    mountPath: /documents
    subPath: documents
    readOnly: true

  # Read-only access to shared workspace
  - name: workspace
    mountPath: /workspace/shared
    subPath: shared
    readOnly: true

  # Read-write access to own task directory
  - name: workspace
    mountPath: /task
    subPath: tasks/${TASK_ID}
    readOnly: false
```

Now workers can directly reference `/attachments/XX/uuid.ext` in their prompts without needing FA to
copy files first.

## Open Questions

### Resolved

1. ~~**Workspace directory structure**~~: Defined with `attachments/`, `documents/`, `shared/`,
   `tasks/` at the workspace root.

2. ~~**File system integration**~~: Unified storage on workspace volume. Attachments and documents
   stored there by default. Symlinks used to provide task context.

3. ~~**Attachment unification**~~: Yes. Store attachments and documents on the workspace volume from
   the start. Simplifies architecture and eliminates cross-volume copies.

### Remaining Questions

1. **AI coder image interface**: What exact arguments/environment does the ai-coder image expect?
   Need to align Job manifest with image's run-task mode.

2. **Result size limits**: How to handle workers that produce very large output files? Options:

   - Stream directly from workspace via presigned-style URLs
   - Chunk large files for reading
   - Set output size limits in worker

3. **Network access for workers**: Use cases like "research EVs" require web access. Options:

   - Egress gateway that logs/filters traffic
   - AI API access only (current design)
   - Full internet with rate limiting

4. **Skill format**: If implementing skills, should they follow ClawdBot's SKILL.md format or use
   FA's note conventions? Consider:

   - ClawdBot format for compatibility
   - Note-based for integration with existing search
   - Hybrid: SKILL.md in workspace, indexed as notes

5. **Multi-step workflows**: Should workers be able to spawn other workers? Current answer: No. FA
   orchestrates all workflows. Workers are single-task.

6. **Context size management**: For note exports, how to handle cases where matching notes exceed
   reasonable context size?

   - Chunking strategy
   - Summary generation before export
   - Relevance ranking cutoff

7. **Migration strategy**: How to migrate existing attachments/documents to the workspace volume?
   Options:

   - One-time migration script
   - Lazy migration (copy on first access)
   - Symlink from old location during transition

## Appendix: ClawdBot Feature Comparison

### Features FA Already Has Better

- **Semantic search**: pgvector embeddings vs grep-over-Markdown
- **Calendar**: Native CalDAV with duplicate detection
- **Home Assistant**: Deep integration with WebSocket events
- **Structured data**: SQL queries, repository pattern, migrations

### Features to Adopt from ClawdBot

- **Container sandbox** (this design): General-purpose computing in isolated containers
- **Persistent workspace**: Files that accumulate over time
- **Skills**: Reusable capabilities (achievable via notes + workspace)

### Features Not Adopting

- **13+ messaging platforms**: Telegram + Web is sufficient for now
- **Native apps**: PWA provides adequate mobile experience
- **Markdown-based memory**: Prefer structured database
- **Agent-to-agent messaging**: Profile delegation is simpler

## References

- [ClawdBot GitHub](https://github.com/clawdbot/clawdbot)
- [ClawdBot Documentation](https://docs.clawd.bot)
- [gVisor Documentation](https://gvisor.dev/docs/)
- [Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
- [Longhorn RWX Volumes](https://longhorn.io/docs/latest/advanced-resources/rwx-workloads/)
