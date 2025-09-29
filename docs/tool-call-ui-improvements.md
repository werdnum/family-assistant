# Chat UI Tool Call Visualization Improvements

## Problem Statement

The current chat UI has several issues with tool call visualization:

1. **Tool Call Clutter**: When the LLM makes multiple consecutive tool calls (especially duplicates
   from model behavior issues), the UI becomes cluttered and difficult to read.

2. **Poor Visual Organization**: Tool calls are rendered inline as individual components, making it
   hard to understand the relationship between multiple tools in a single interaction.

3. **Streaming Display Issues**: As tool calls stream in, they can appear disjointed from the main
   message content, creating a fragmented user experience.

4. **Lack of Progressive Disclosure**: Users see all tool execution details at once, even when they
   may only care about the final result.

## Current Architecture

The chat UI uses assistant-ui's component system:

- `Thread.tsx` renders messages using `MessagePrimitive.Content`
- Tool calls are rendered through `DynamicToolUI` component
- Each tool call appears as an individual component within the message content
- Tool calls use the `ToolWithConfirmation` wrapper for confirmation flows

## Proposed Solution

### 1. Collapsible Tool Groups

Implement collapsible containers for consecutive tool calls using assistant-ui's `ToolGroup`
component:

- Group consecutive tool calls into expandable/collapsible sections
- Show summary information (number of tools, types) when collapsed
- Allow users to expand to see full details when needed
- Preserve individual tool functionality (confirmations, results display)

### 2. Visual Hierarchy Improvements

- Use clear visual distinction between message text and tool call groups
- Add subtle borders and background colors to separate content types
- Include icons to indicate tool types and execution status
- Maintain consistent spacing and alignment

### 3. Progressive Disclosure

- Show condensed summaries by default
- Allow expansion for detailed views
- Persist user preferences for expanded/collapsed state per message
- Smooth animations for expand/collapse transitions

## Implementation Plan

### Phase 1: Core Components

#### 1.1 ToolGroup Component (`frontend/src/chat/ToolGroup.tsx`)

```tsx
interface ToolGroupProps {
  startIndex: number;
  endIndex: number;
  children: React.ReactNode;
}

const ToolGroup: FC<ToolGroupProps> = ({ startIndex, endIndex, children }) => {
  const toolCount = endIndex - startIndex + 1;
  const [isExpanded, setIsExpanded] = useState(false);

  return (
    <Collapsible open={isExpanded} onOpenChange={setIsExpanded}>
      <CollapsibleTrigger className="tool-group-header">
        <span>{toolCount} tool {toolCount === 1 ? "call" : "calls"}</span>
        <ChevronDownIcon className={isExpanded ? "rotate-180" : ""} />
      </CollapsibleTrigger>
      <CollapsibleContent className="tool-group-content">
        {children}
      </CollapsibleContent>
    </Collapsible>
  );
};
```

#### 1.2 Reusable Collapsible Component (`frontend/src/components/ui/collapsible.tsx`)

Based on Radix UI Collapsible primitive, providing:

- Smooth animations
- Accessibility features
- Consistent styling
- Keyboard navigation support

### Phase 2: Integration

#### 2.1 Thread Component Updates

Update `Thread.tsx` to use ToolGroup in messageContentComponents:

```tsx
const messageContentComponents = {
  Text: MarkdownText,
  tools: {
    ToolGroup,
    Fallback: DynamicToolUI,
  },
};
```

#### 2.2 Styling Integration

- Add CSS classes that match the existing UI theme
- Ensure proper contrast and readability
- Responsive design for mobile devices
- Dark/light mode support

### Phase 3: Enhanced Features

#### 3.1 Tool Type Recognition

- Add icons for different tool types (calendar, notes, search, etc.)
- Color coding for different tool categories
- Status indicators for tool execution states

#### 3.2 Smart Grouping Logic

- Group tools by type when appropriate
- Handle mixed tool types in a single group
- Respect tool execution order and dependencies

## Testing Strategy

### Unit Tests (Vitest)

**File**: `frontend/src/chat/__tests__/ToolGroup.test.tsx`

```tsx
describe('ToolGroup', () => {
  it('displays tool count correctly', () => {
    render(<ToolGroup startIndex={0} endIndex={2}>{mockChildren}</ToolGroup>);
    expect(screen.getByText('3 tool calls')).toBeInTheDocument();
  });

  it('toggles expansion on click', async () => {
    render(<ToolGroup startIndex={0} endIndex={0}>{mockChildren}</ToolGroup>);
    const trigger = screen.getByRole('button');

    expect(screen.queryByTestId('tool-content')).not.toBeVisible();

    await userEvent.click(trigger);
    expect(screen.getByTestId('tool-content')).toBeVisible();
  });

  it('supports keyboard navigation', async () => {
    render(<ToolGroup startIndex={0} endIndex={1}>{mockChildren}</ToolGroup>);
    const trigger = screen.getByRole('button');

    await userEvent.tab();
    expect(trigger).toHaveFocus();

    await userEvent.keyboard('{Enter}');
    expect(screen.getByTestId('tool-content')).toBeVisible();
  });
});
```

### Integration Tests (Playwright)

**File**: `tests/functional/web/test_tool_call_grouping.py`

```python
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_multiple_tool_calls_are_grouped(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that multiple consecutive tool calls are grouped in a collapsible section."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock to return multiple tool calls
    mock_llm_client.rules = [
        (
            lambda args: "add multiple notes" in str(args.get("messages", [])),
            LLMOutput(
                content="I'll add several notes for you.",
                tool_calls=[
                    ToolCallItem(id="call_1", type="function", function=ToolCallFunction(...)),
                    ToolCallItem(id="call_2", type="function", function=ToolCallFunction(...)),
                    ToolCallItem(id="call_3", type="function", function=ToolCallFunction(...)),
                ]
            )
        )
    ]

    await chat_page.navigate()
    await chat_page.send_message("Please add multiple notes")

    # Wait for tool group to appear
    tool_group = page.locator('[data-testid="tool-group"]')
    await expect(tool_group).to_be_visible()

    # Verify tool count is displayed
    await expect(tool_group.locator('text=3 tool calls')).to_be_visible()

    # Verify tools are initially collapsed
    tool_content = page.locator('[data-testid="tool-group-content"]')
    await expect(tool_content).not_to_be_visible()

    # Click to expand
    await tool_group.click()
    await expect(tool_content).to_be_visible()
```

## Success Criteria

1. **Visual Organization**: Multiple tool calls are visually grouped together
2. **Progressive Disclosure**: Users can collapse/expand tool groups as needed
3. **Accessibility**: Keyboard navigation and screen reader support
4. **Performance**: No noticeable impact on rendering performance
5. **Responsive Design**: Works correctly on mobile devices
6. **Test Coverage**: >90% test coverage for new components
7. **Integration**: Seamless integration with existing tool confirmation flows

## Future Work (Not in Current Implementation)

### Reasoning Content Support

- Parse LLM output to identify reasoning/thinking sections
- Display reasoning in separate collapsible sections
- Allow users to toggle reasoning visibility globally
- Maintain reasoning content in message history for debugging

### Advanced Tool Visualizations

- Real-time progress indicators for long-running tools
- Tool result previews in collapsed state
- Custom tool-specific mini-visualizations
- Tool execution timelines and dependencies

### User Preferences

- Remember collapsed/expanded state per user
- Configurable default expansion behavior
- Tool-specific display preferences
- Export/import UI configuration

## Implementation Notes

- Uses native HTML `details`/`summary` elements for accessibility by default
- Graceful degradation for users with JavaScript disabled
- Follows existing code patterns and styling conventions
- Minimal performance impact through efficient React rendering
- Maintains compatibility with existing tool confirmation system

## Dependencies

- Radix UI Collapsible (for enhanced version)
- Lucide React (for icons)
- Existing assistant-ui primitives
- Current testing infrastructure (Vitest, Playwright)

## Risks and Mitigations

**Risk**: Breaking existing tool confirmation flows **Mitigation**: Thorough integration testing
with confirmation system

**Risk**: Performance impact with many tool calls **Mitigation**: Virtual scrolling or pagination
for large tool groups

**Risk**: Accessibility regression **Mitigation**: Comprehensive a11y testing and screen reader
validation

**Risk**: Complex state management for expand/collapse **Mitigation**: Simple local state, avoid
complex global state requirements
