# Family Assistant Architecture Diagram

> **Note**: For detailed information about the LLM agent system and service profiles, see
> [AGENTS.md](./AGENTS.md).

## Backend Component Architecture

```mermaid
graph TB
    %% Entry Points
    subgraph "Entry Points"
        CLI["CLI Entry Point<br/>(__main__.py)"]
        WebUI["Web UI<br/>(React SPA)"]
        TG["Telegram Bot"]
        EMAIL["Email Webhook"]
    end

    %% Configuration Layer
    subgraph "Configuration"
        CONFIG["Config Loader<br/>• Hierarchical config<br/>• Service profiles<br/>• Environment vars"]
    end

    %% Core Orchestration
    subgraph "Core Orchestration"
        ASSISTANT["Assistant<br/>• Dependency injection<br/>• Service lifecycle<br/>• Resource management"]
    end

    %% Processing Layer
    subgraph "Processing Layer"
        PROC["ProcessingService<br/>• LLM orchestration<br/>• Context aggregation<br/>• Tool execution<br/>• Message history"]
        
        subgraph "Service Profiles"
            PROF1["Default Profile<br/>• Full tools<br/>• Main LLM"]
            PROF2["Browser Profile<br/>• Browser tools only<br/>• Specialized LLM"]
            PROF3["Research Profile<br/>• No tools<br/>• Research LLM"]
        end
    end

    %% Context System
    subgraph "Context Providers"
        CTX_NOTES["Notes Context"]
        CTX_CAL["Calendar Context"]
        CTX_WEATHER["Weather Context"]
        CTX_HA["Home Assistant Context"]
        CTX_USERS["Known Users Context"]
    end

    %% Tool System
    subgraph "Tool System"
        TOOLS["CompositeToolsProvider"]
        
        subgraph "Tool Providers"
            LOCAL["LocalToolsProvider<br/>• Python functions<br/>• Dependency injection"]
            MCP["MCPToolsProvider<br/>• External servers<br/>• Protocol integration"]
            FILTERED["FilteredToolsProvider<br/>• Profile restrictions"]
            CONFIRM["ConfirmingToolsProvider<br/>• User confirmation"]
        end
        
        subgraph "Tool Categories"
            T_NOTES["Notes Tools"]
            T_TASKS["Task Tools"]
            T_DOCS["Document Tools"]
            T_CAL["Calendar Tools"]
            T_COMM["Communication Tools"]
            T_HA_TOOLS["Home Assistant Tools"]
            T_EVENTS["Event Tools"]
        end
    end

    %% Storage Layer
    subgraph "Storage Layer"
        DB_CTX["DatabaseContext<br/>• Transaction management<br/>• Connection pooling<br/>• Retry logic"]
        
        subgraph "Repositories"
            R_NOTES["NotesRepository"]
            R_TASKS["TasksRepository"]
            R_MSG["MessageHistoryRepository"]
            R_EMAIL["EmailRepository"]
            R_EVENTS["EventsRepository"]
            R_VECTOR["VectorRepository"]
            R_ERROR["ErrorLogsRepository"]
        end
        
        subgraph "Database"
            SQLITE["SQLite<br/>(Development)"]
            POSTGRES["PostgreSQL<br/>(Production)"]
        end
    end

    %% Task System
    subgraph "Task Worker System"
        WORKER["TaskWorker<br/>• Queue polling<br/>• Handler execution<br/>• Retry logic"]
        
        subgraph "Task Handlers"
            H_LLM["LLM Callback"]
            H_INDEX["Document Indexing"]
            H_EMBED["Embedding Generation"]
            H_SCRIPT["Script Execution"]
            H_MAINT["System Maintenance"]
        end
    end

    %% Event System
    subgraph "Event System"
        EVENT_PROC["EventProcessor<br/>• Event routing<br/>• Listener matching<br/>• Action execution"]
        
        subgraph "Event Sources"
            E_HA["Home Assistant Source<br/>(WebSocket)"]
            E_INDEX["Indexing Source"]
        end
        
        E_LISTENERS["Event Listeners<br/>• Condition matching<br/>• Rate limiting<br/>• Action triggers"]
    end

    %% Document Indexing
    subgraph "Document Indexing"
        INDEX_PIPE["IndexingPipeline<br/>• Pipeline orchestration<br/>• Processor chain"]
        
        subgraph "Processors"
            P_TEXT["Text Chunker"]
            P_LLM["LLM Intelligence"]
            P_EMBED["Embedding Dispatch"]
            P_FILE["File Processors"]
        end
        
        subgraph "Indexers"
            IDX_DOC["Document Indexer"]
            IDX_EMAIL["Email Indexer"]
            IDX_NOTE["Notes Indexer"]
        end
    end

    %% Web API Layer
    subgraph "Web API (FastAPI)"
        APP["FastAPI App<br/>• Router management<br/>• Middleware stack<br/>• SSE streaming"]
        
        subgraph "API Routers"
            API_CHAT["Chat API<br/>• Send message<br/>• SSE streaming<br/>• Conversations"]
            API_TOOLS["Tools API<br/>• Execute tools<br/>• Get definitions"]
            API_DOCS["Documents API<br/>• Upload<br/>• Search<br/>• Reindex"]
            API_NOTES["Notes API<br/>• CRUD operations"]
            API_TASKS["Tasks API<br/>• Task management"]
            API_EVENTS["Events API<br/>• Event queries<br/>• Listener management"]
        end
        
        CONFIRM_MGR["ConfirmationManager<br/>• Tool confirmations<br/>• SSE-based flow"]
    end

    %% LLM Integration
    subgraph "LLM Integration"
        LLM["LLMInterface<br/>• Model abstraction<br/>• Streaming support<br/>• Tool calling"]
        
        subgraph "LLM Backends"
            LLM_OPENAI["OpenAI"]
            LLM_ANTHROPIC["Anthropic"]
            LLM_GEMINI["Gemini"]
            LLM_LOCAL["Local Models"]
        end
    end

    %% Connections - Entry to Core
    CLI --> CONFIG
    CONFIG --> ASSISTANT
    
    %% Core to Services
    ASSISTANT --> PROC
    ASSISTANT --> WORKER
    ASSISTANT --> EVENT_PROC
    ASSISTANT --> APP
    ASSISTANT --> TG
    
    %% Web Connections
    WebUI --> APP
    EMAIL --> APP
    TG --> PROC
    
    %% Processing Connections
    PROC --> PROF1
    PROC --> PROF2
    PROC --> PROF3
    PROC --> LLM
    PROC --> TOOLS
    PROC --> DB_CTX
    
    %% Context Provider Connections
    PROC --> CTX_NOTES
    PROC --> CTX_CAL
    PROC --> CTX_WEATHER
    PROC --> CTX_HA
    PROC --> CTX_USERS
    
    %% Tool System Connections
    TOOLS --> LOCAL
    TOOLS --> MCP
    LOCAL --> FILTERED
    FILTERED --> CONFIRM
    LOCAL --> T_NOTES
    LOCAL --> T_TASKS
    LOCAL --> T_DOCS
    LOCAL --> T_CAL
    LOCAL --> T_COMM
    LOCAL --> T_HA_TOOLS
    LOCAL --> T_EVENTS
    
    %% Database Connections
    DB_CTX --> R_NOTES
    DB_CTX --> R_TASKS
    DB_CTX --> R_MSG
    DB_CTX --> R_EMAIL
    DB_CTX --> R_EVENTS
    DB_CTX --> R_VECTOR
    DB_CTX --> R_ERROR
    
    R_NOTES --> SQLITE
    R_NOTES --> POSTGRES
    R_TASKS --> SQLITE
    R_TASKS --> POSTGRES
    R_MSG --> SQLITE
    R_MSG --> POSTGRES
    R_EMAIL --> SQLITE
    R_EMAIL --> POSTGRES
    R_EVENTS --> SQLITE
    R_EVENTS --> POSTGRES
    R_VECTOR --> SQLITE
    R_VECTOR --> POSTGRES
    R_ERROR --> SQLITE
    R_ERROR --> POSTGRES
    
    %% Task Worker Connections
    WORKER --> R_TASKS
    WORKER --> H_LLM
    WORKER --> H_INDEX
    WORKER --> H_EMBED
    WORKER --> H_SCRIPT
    WORKER --> H_MAINT
    
    %% Event System Connections
    EVENT_PROC --> E_HA
    EVENT_PROC --> E_INDEX
    EVENT_PROC --> E_LISTENERS
    EVENT_PROC --> R_EVENTS
    E_LISTENERS --> WORKER
    
    %% Indexing Connections
    INDEX_PIPE --> P_TEXT
    INDEX_PIPE --> P_LLM
    INDEX_PIPE --> P_EMBED
    INDEX_PIPE --> P_FILE
    IDX_DOC --> INDEX_PIPE
    IDX_EMAIL --> INDEX_PIPE
    IDX_NOTE --> INDEX_PIPE
    P_EMBED --> WORKER
    INDEX_PIPE --> E_INDEX
    
    %% API Router Connections
    APP --> API_CHAT
    APP --> API_TOOLS
    APP --> API_DOCS
    APP --> API_NOTES
    APP --> API_TASKS
    APP --> API_EVENTS
    API_CHAT --> PROC
    API_CHAT --> CONFIRM_MGR
    API_TOOLS --> TOOLS
    API_DOCS --> IDX_DOC
    API_NOTES --> R_NOTES
    API_TASKS --> R_TASKS
    API_EVENTS --> R_EVENTS
    
    %% LLM Backend Connections
    LLM --> LLM_OPENAI
    LLM --> LLM_ANTHROPIC
    LLM --> LLM_GEMINI
    LLM --> LLM_LOCAL

    %% Styling
    classDef entryPoint fill:#e1f5e1,stroke:#4caf50,stroke-width:2px
    classDef core fill:#fff3e0,stroke:#ff9800,stroke-width:2px
    classDef storage fill:#e3f2fd,stroke:#2196f3,stroke-width:2px
    classDef processing fill:#fce4ec,stroke:#e91e63,stroke-width:2px
    classDef tool fill:#f3e5f5,stroke:#9c27b0,stroke-width:2px
    classDef api fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
    classDef event fill:#fff9c4,stroke:#fbc02d,stroke-width:2px
    classDef index fill:#efebe9,stroke:#795548,stroke-width:2px
    
    class CLI,WebUI,TG,EMAIL entryPoint
    class ASSISTANT,CONFIG core
    class DB_CTX,R_NOTES,R_TASKS,R_MSG,R_EMAIL,R_EVENTS,R_VECTOR,R_ERROR,SQLITE,POSTGRES storage
    class PROC,PROF1,PROF2,PROF3,LLM,LLM_OPENAI,LLM_ANTHROPIC,LLM_GEMINI,LLM_LOCAL processing
    class TOOLS,LOCAL,MCP,FILTERED,CONFIRM,T_NOTES,T_TASKS,T_DOCS,T_CAL,T_COMM,T_HA_TOOLS,T_EVENTS tool
    class APP,API_CHAT,API_TOOLS,API_DOCS,API_NOTES,API_TASKS,API_EVENTS,CONFIRM_MGR api
    class EVENT_PROC,E_HA,E_INDEX,E_LISTENERS event
    class INDEX_PIPE,P_TEXT,P_LLM,P_EMBED,P_FILE,IDX_DOC,IDX_EMAIL,IDX_NOTE index
```

## Data Flow Diagrams

### 1. User Message Processing Flow

```mermaid
sequenceDiagram
    participant User
    participant Interface as Interface<br/>(Web/Telegram)
    participant Processing as ProcessingService
    participant Context as Context Providers
    participant LLM as LLM Interface
    participant Tools as Tool System
    participant DB as Database

    User->>Interface: Send message
    Interface->>Processing: handle_chat_interaction()
    
    Processing->>Context: Aggregate context
    Context-->>Processing: Context fragments
    
    Processing->>DB: Store user message
    Processing->>DB: Get message history
    
    Processing->>LLM: Stream completion
    
    loop Tool Calling Loop
        LLM-->>Processing: Tool call request
        Processing->>Tools: Execute tool
        Tools->>DB: Read/Write data
        Tools-->>Processing: Tool result
        Processing->>LLM: Tool result
    end
    
    LLM-->>Processing: Final response
    Processing->>DB: Store assistant message
    Processing-->>Interface: Response
    Interface-->>User: Display response
```

### 2. Task Processing Flow

```mermaid
sequenceDiagram
    participant Tool
    participant DB as TasksRepository
    participant Worker as TaskWorker
    participant Handler as Task Handler
    participant System as External System

    Tool->>DB: Enqueue task
    DB-->>Tool: Task ID
    
    loop Polling Loop
        Worker->>DB: Dequeue next task
        DB-->>Worker: Task details
        
        Worker->>DB: Lock task
        Worker->>Handler: Execute handler
        
        alt Success
            Handler->>System: Perform action
            System-->>Handler: Result
            Handler-->>Worker: Success
            Worker->>DB: Mark complete
        else Failure
            Handler-->>Worker: Error
            Worker->>DB: Update retry count
            Note over Worker,DB: Exponential backoff
        end
    end
```

### 3. Document Indexing Flow

```mermaid
sequenceDiagram
    participant API
    participant Indexer as Document Indexer
    participant Pipeline as IndexingPipeline
    participant Processor as Processors
    participant TaskQueue as Task Queue
    participant VectorDB as Vector Storage

    API->>Indexer: Upload document
    Indexer->>Pipeline: Process content
    
    loop For each processor
        Pipeline->>Processor: Transform content
        Processor-->>Pipeline: Processed content
    end
    
    Pipeline->>TaskQueue: Queue embedding task
    
    Note over TaskQueue: Async processing
    
    TaskQueue->>VectorDB: Store embeddings
    Pipeline->>API: Indexing complete
    
    Pipeline-->>EventSystem: Emit IndexingEvent
```

### 4. Event Processing Flow

```mermaid
sequenceDiagram
    participant Source as Event Source
    participant Processor as EventProcessor
    participant DB as EventsRepository
    participant Listeners as Event Listeners
    participant Actions as Action Executor

    Source->>Processor: Emit event
    
    Processor->>DB: Store event
    Processor->>Listeners: Match conditions
    
    loop For each matching listener
        Listeners->>Processor: Matched listener
        
        alt Rate limit OK
            Processor->>Actions: Execute action
            
            alt Wake LLM
                Actions->>ProcessingService: Trigger conversation
            else Run Script
                Actions->>TaskWorker: Queue task
            end
            
            Processor->>DB: Update listener state
        else Rate limited
            Processor->>Processor: Skip action
        end
    end
```

## Key Architectural Patterns

### Repository Pattern

- All data access through `DatabaseContext`
- Each repository handles specific domain
- Consistent error handling and retry logic
- Transaction management at context level

### Dependency Injection

- Constructor-based injection throughout
- Protocol-based interfaces for testing
- Configuration-driven service creation
- Shared resource management

### Event-Driven Architecture

- Loosely coupled components
- Asynchronous event processing
- Rate limiting and deduplication
- Action-based responses

### Pipeline Processing

- Configurable processor chains
- Content transformation stages
- Async task dispatching
- Error handling at each stage

### Service Profiles

- Multiple LLM configurations
- Tool access control per profile
- Context provider customization
- Delegation between profiles

## Technology Stack

### Core Technologies

- **Language**: Python 3.11+
- **Web Framework**: FastAPI
- **Database ORM**: SQLAlchemy 2.0
- **Async Runtime**: asyncio
- **Frontend**: React + Vite
- **Task Queue**: Custom database-backed

### Storage

- **Development**: SQLite with optimizations
- **Production**: PostgreSQL with pgvector
- **Migrations**: Alembic
- **Caching**: In-memory + database

### External Integrations

- **LLM Providers**: OpenAI, Anthropic, Gemini
- **Home Automation**: Home Assistant WebSocket
- **Calendar**: CalDAV protocol
- **Email**: Mailgun webhooks
- **Weather**: WillyWeather API
- **Tools**: MCP (Model Context Protocol)

### Development Tools

- **Testing**: pytest + pytest-asyncio
- **UI Testing**: Playwright
- **Linting**: ruff, basedpyright, pylint
- **Formatting**: ruff format
- **Documentation**: Markdown + Mermaid

## Additional Component Interaction Flows

### 5. Service Startup Flow

```mermaid
sequenceDiagram
    participant Main as __main__.py
    participant Config as Config Loader
    participant Assistant
    participant Services as Services
    participant DB as Database

    Main->>Config: Load configuration
    Config->>Config: Merge hierarchical config
    Config-->>Main: Configuration object
    
    Main->>Assistant: Create with config
    Assistant->>Assistant: setup_dependencies()
    
    Assistant->>DB: Initialize database
    DB->>DB: Run Alembic migrations
    DB-->>Assistant: Database ready
    
    Assistant->>Services: Create service instances
    Note over Services: ProcessingService<br/>TaskWorker<br/>EventProcessor<br/>TelegramBot<br/>FastAPI
    
    Assistant->>Services: start_services()
    
    par Parallel Startup
        Services->>Services: Start TaskWorker
        and
        Services->>Services: Start EventProcessor
        and
        Services->>Services: Start Web Server
        and
        Services->>Services: Start Telegram Bot
    end
    
    Services-->>Assistant: Services running
    Assistant-->>Main: Application ready
```

### 6. Tool Confirmation Flow

```mermaid
sequenceDiagram
    participant LLM
    participant Processing as ProcessingService
    participant Confirming as ConfirmingToolsProvider
    participant Manager as ConfirmationManager
    participant User
    participant Tool as Actual Tool

    LLM->>Processing: Tool call request
    Processing->>Confirming: Execute tool
    
    alt Tool needs confirmation
        Confirming->>Manager: Request confirmation
        Manager->>User: Send confirmation request
        Note over Manager: SSE or Telegram message
        
        User->>Manager: Approve/Reject
        Manager-->>Confirming: User decision
        
        alt Approved
            Confirming->>Tool: Execute tool
            Tool-->>Confirming: Result
            Confirming-->>Processing: Tool result
        else Rejected
            Confirming-->>Processing: Rejection message
        end
    else No confirmation needed
        Confirming->>Tool: Execute directly
        Tool-->>Confirming: Result
        Confirming-->>Processing: Tool result
    end
    
    Processing->>LLM: Tool result
```

### 7. Profile Delegation Flow

```mermaid
sequenceDiagram
    participant User
    participant Default as Default Profile
    participant Delegate as Delegate Tool
    participant Browser as Browser Profile
    participant Tools as Browser Tools

    User->>Default: "Search for Python documentation"
    Default->>Default: Process with LLM
    Default->>Delegate: delegate_to_assistant("browser_profile")
    
    Delegate->>Browser: Forward request
    Browser->>Browser: Process with specialized LLM
    Browser->>Tools: Execute browser tools
    Tools-->>Browser: Web content
    Browser->>Browser: Summarize findings
    Browser-->>Delegate: Response
    
    Delegate-->>Default: Delegation result
    Default->>Default: Incorporate into response
    Default-->>User: Final answer
```

### 8. Database Transaction Flow

```mermaid
sequenceDiagram
    participant Service
    participant Context as DatabaseContext
    participant Conn as Connection
    participant Repo as Repository
    participant DB as Database

    Service->>Context: async with DatabaseContext()
    Context->>Conn: engine.begin()
    Conn->>Conn: Start transaction
    
    Context->>Repo: Create repositories
    Service->>Repo: Repository operation
    
    loop Retry on transient error
        Repo->>DB: Execute query
        
        alt Success
            DB-->>Repo: Result
            Repo-->>Service: Data
        else Transient Error
            DB-->>Repo: Error
            Note over Repo: Exponential backoff
            Repo->>Repo: Wait and retry
        else Non-retryable Error
            DB-->>Repo: Error
            Repo-->>Service: Raise exception
            Context->>Conn: Rollback
        end
    end
    
    alt All operations succeed
        Context->>Conn: Commit transaction
    else Any operation fails
        Context->>Conn: Rollback transaction
    end
    
    Context->>Conn: Close connection
```

### 9. Real-time Streaming Flow (SSE)

```mermaid
sequenceDiagram
    participant Browser as Web Browser
    participant API as FastAPI
    participant Processing as ProcessingService
    participant LLM
    participant SSE as SSE Stream

    Browser->>API: POST /v1/chat/send_message_stream
    API->>SSE: Create EventSourceResponse
    API->>Processing: handle_chat_interaction_stream()
    
    Processing->>LLM: Stream completion
    
    loop Streaming Response
        LLM-->>Processing: Content chunk
        Processing->>SSE: Send "text" event
        SSE-->>Browser: Update UI
        
        alt Tool Call
            LLM-->>Processing: Tool call
            Processing->>SSE: Send "tool_call" event
            Processing->>Processing: Execute tool
            Processing->>SSE: Send "tool_result" event
        end
    end
    
    LLM-->>Processing: Complete
    Processing->>SSE: Send "end" event
    SSE-->>Browser: Finalize display
    API->>API: Close stream
```

### 10. Home Assistant Integration Flow

```mermaid
sequenceDiagram
    participant HA as Home Assistant
    participant WS as WebSocket
    participant Source as HA Event Source
    participant Processor as EventProcessor
    participant Listener as Event Listener
    participant LLM as ProcessingService

    HA->>WS: State change
    WS->>Source: Receive event
    Source->>Source: Parse event
    Source->>Processor: Emit event
    
    Processor->>Listener: Match conditions
    
    alt Condition matches
        Listener->>Processor: Trigger action
        
        alt Wake LLM Action
            Processor->>LLM: Wake with context
            LLM->>LLM: Process event
            LLM-->>User: Send notification
        else Script Action
            Processor->>TaskWorker: Queue script
            TaskWorker->>HA: Execute automation
        end
        
        Processor->>DB: Store event
        Processor->>Listener: Update last triggered
    end
```

## Component Responsibilities Summary

### Core Components

| Component             | Primary Responsibilities                                        | Key Interactions                                  |
| --------------------- | --------------------------------------------------------------- | ------------------------------------------------- |
| **Assistant**         | Service lifecycle, dependency injection, resource management    | All services, Database, Configuration             |
| **ProcessingService** | LLM orchestration, context aggregation, tool execution          | LLM providers, Tools, Context providers, Database |
| **DatabaseContext**   | Transaction management, repository access, connection pooling   | All repositories, Database engines                |
| **TaskWorker**        | Background job processing, scheduling, retry logic              | Task handlers, Database, Services                 |
| **EventProcessor**    | Event routing, listener matching, action execution              | Event sources, Database, Task system              |
| **IndexingPipeline**  | Document processing, content transformation, embedding dispatch | Processors, Task queue, Vector storage            |

### Repository Responsibilities

| Repository                   | Data Domain              | Key Operations                         |
| ---------------------------- | ------------------------ | -------------------------------------- |
| **NotesRepository**          | User notes and knowledge | CRUD, prompt inclusion, search         |
| **TasksRepository**          | Background tasks         | Queue management, scheduling, retries  |
| **MessageHistoryRepository** | Conversation history     | Thread management, turn grouping       |
| **EmailRepository**          | Email storage            | Parsing, attachment handling, indexing |
| **EventsRepository**         | System events            | Event storage, listener management     |
| **VectorRepository**         | Embeddings               | Semantic search, document vectors      |
| **ErrorLogsRepository**      | Error tracking           | Logging, categorization, debugging     |

### Tool Categories

| Category           | Purpose                  | Example Tools                            |
| ------------------ | ------------------------ | ---------------------------------------- |
| **Notes**          | Knowledge management     | add_or_update_note, search_notes         |
| **Tasks**          | Scheduling and reminders | schedule_callback, create_recurring_task |
| **Documents**      | Content processing       | search_documents, ingest_document        |
| **Calendar**       | Event management         | create_event, search_events              |
| **Communication**  | Messaging                | send_message, view_message_history       |
| **Events**         | System automation        | query_recent_events, manage_listeners    |
| **Home Assistant** | Home automation          | execute_service, get_state               |
