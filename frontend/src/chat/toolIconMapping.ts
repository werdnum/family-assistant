/**
 * Tool Icon Mapping System
 *
 * Maps tool names to Lucide React icons and color categories.
 * Used for displaying visual indicators in tool call groups.
 */

import type { LucideIcon } from 'lucide-react';
import {
  StickyNote,
  FileText,
  Search,
  Clock,
  Bell,
  Zap,
  Home,
  Code,
  Paperclip,
  Wand2,
  Trash2,
  Edit,
  Plus,
  List,
  Download,
  Send,
  MessageSquare,
  FileSearch,
  CalendarPlus,
  CalendarSearch,
  CalendarX,
  Timer,
  Repeat,
  Play,
  Settings,
  Lightbulb,
  Camera,
  Users,
} from 'lucide-react';

export interface ToolIconInfo {
  icon: LucideIcon;
  category: ToolCategory;
  label: string;
}

export type ToolCategory =
  | 'notes'
  | 'calendar'
  | 'documents'
  | 'tasks'
  | 'communication'
  | 'events'
  | 'home_assistant'
  | 'scripting'
  | 'attachments'
  | 'images'
  | 'other';

/**
 * Category metadata for styling and display
 */
export const categoryInfo: Record<ToolCategory, { color: string; label: string }> = {
  notes: { color: 'text-yellow-600 dark:text-yellow-400', label: 'Notes' },
  calendar: { color: 'text-blue-600 dark:text-blue-400', label: 'Calendar' },
  documents: { color: 'text-purple-600 dark:text-purple-400', label: 'Documents' },
  tasks: { color: 'text-green-600 dark:text-green-400', label: 'Tasks' },
  communication: { color: 'text-cyan-600 dark:text-cyan-400', label: 'Communication' },
  events: { color: 'text-orange-600 dark:text-orange-400', label: 'Events' },
  home_assistant: { color: 'text-indigo-600 dark:text-indigo-400', label: 'Home' },
  scripting: { color: 'text-pink-600 dark:text-pink-400', label: 'Scripting' },
  attachments: { color: 'text-gray-600 dark:text-gray-400', label: 'Attachments' },
  images: { color: 'text-rose-600 dark:text-rose-400', label: 'Images' },
  other: { color: 'text-slate-600 dark:text-slate-400', label: 'Other' },
};

/**
 * Tool name to icon mapping
 */
export const toolIconMapping: Record<string, ToolIconInfo> = {
  // Notes tools (4)
  add_or_update_note: { icon: Plus, category: 'notes', label: 'Add/Update Note' },
  get_note: { icon: StickyNote, category: 'notes', label: 'Get Note' },
  list_notes: { icon: List, category: 'notes', label: 'List Notes' },
  delete_note: { icon: Trash2, category: 'notes', label: 'Delete Note' },

  // Calendar tools (4)
  add_calendar_event: { icon: CalendarPlus, category: 'calendar', label: 'Add Event' },
  search_calendar_events: { icon: CalendarSearch, category: 'calendar', label: 'Search Events' },
  modify_calendar_event: { icon: Edit, category: 'calendar', label: 'Modify Event' },
  delete_calendar_event: { icon: CalendarX, category: 'calendar', label: 'Delete Event' },

  // Document tools (4)
  search_documents: { icon: FileSearch, category: 'documents', label: 'Search Documents' },
  get_full_document_content: { icon: FileText, category: 'documents', label: 'Get Document' },
  ingest_document_from_url: { icon: Download, category: 'documents', label: 'Ingest Document' },
  get_user_documentation_content: { icon: FileText, category: 'documents', label: 'Get Docs' },

  // Task & Scheduling tools (8)
  schedule_reminder: { icon: Bell, category: 'tasks', label: 'Schedule Reminder' },
  schedule_future_callback: { icon: Timer, category: 'tasks', label: 'Schedule Callback' },
  schedule_recurring_task: { icon: Repeat, category: 'tasks', label: 'Recurring Task' },
  schedule_action: { icon: Clock, category: 'tasks', label: 'Schedule Action' },
  schedule_recurring_action: { icon: Repeat, category: 'tasks', label: 'Recurring Action' },
  list_pending_callbacks: { icon: List, category: 'tasks', label: 'List Callbacks' },
  modify_pending_callback: { icon: Edit, category: 'tasks', label: 'Modify Callback' },
  cancel_pending_callback: { icon: Trash2, category: 'tasks', label: 'Cancel Callback' },

  // Communication tools (3)
  get_message_history: { icon: MessageSquare, category: 'communication', label: 'Message History' },
  send_message_to_user: { icon: Send, category: 'communication', label: 'Send Message' },
  get_attachment_info: { icon: Paperclip, category: 'communication', label: 'Attachment Info' },

  // Event system tools (2)
  query_recent_events: { icon: Search, category: 'events', label: 'Query Events' },
  test_event_listener: { icon: Play, category: 'events', label: 'Test Listener' },

  // Automation tools (8)
  create_automation: { icon: Zap, category: 'events', label: 'Create Automation' },
  list_automations: { icon: List, category: 'events', label: 'List Automations' },
  get_automation: { icon: Search, category: 'events', label: 'Get Automation' },
  update_automation: { icon: Edit, category: 'events', label: 'Update Automation' },
  enable_automation: { icon: Play, category: 'events', label: 'Enable Automation' },
  disable_automation: { icon: Settings, category: 'events', label: 'Disable Automation' },
  delete_automation: { icon: Trash2, category: 'events', label: 'Delete Automation' },
  get_automation_stats: { icon: FileSearch, category: 'events', label: 'Automation Stats' },

  // Home Assistant tools (2)
  render_home_assistant_template: { icon: Home, category: 'home_assistant', label: 'HA Template' },
  get_camera_snapshot: { icon: Camera, category: 'home_assistant', label: 'Camera Snapshot' },

  // Service delegation (1)
  delegate_to_service: { icon: Users, category: 'other', label: 'Delegate' },

  // Script execution (1)
  execute_script: { icon: Code, category: 'scripting', label: 'Execute Script' },

  // Attachment tools (1)
  attach_to_response: { icon: Paperclip, category: 'attachments', label: 'Attach File' },

  // Image processing (1)
  highlight_image: { icon: Edit, category: 'images', label: 'Highlight Image' },

  // Image generation (2)
  generate_image: { icon: Wand2, category: 'images', label: 'Generate Image' },
  transform_image: { icon: Wand2, category: 'images', label: 'Transform Image' },
};

/**
 * Get icon info for a tool name, with fallback for unknown tools
 */
export function getToolIconInfo(toolName: string): ToolIconInfo {
  return (
    toolIconMapping[toolName] ?? {
      icon: Lightbulb,
      category: 'other',
      label: toolName,
    }
  );
}

/**
 * Get category color class for a tool
 */
export function getToolCategoryColor(toolName: string): string {
  const iconInfo = getToolIconInfo(toolName);
  return categoryInfo[iconInfo.category].color;
}

/**
 * Group tools by category
 */
export function groupToolsByCategory(toolNames: string[]): Map<ToolCategory, string[]> {
  const grouped = new Map<ToolCategory, string[]>();

  for (const toolName of toolNames) {
    const iconInfo = getToolIconInfo(toolName);
    const existing = grouped.get(iconInfo.category) ?? [];
    grouped.set(iconInfo.category, [...existing, toolName]);
  }

  return grouped;
}

/**
 * Get unique tool categories from a list of tool names
 */
export function getUniqueCategories(toolNames: string[]): ToolCategory[] {
  const categories = new Set<ToolCategory>();
  for (const toolName of toolNames) {
    const iconInfo = getToolIconInfo(toolName);
    categories.add(iconInfo.category);
  }
  return Array.from(categories);
}

/**
 * Format a count with proper pluralization
 */
export function formatToolCount(count: number, singular: string = 'tool', plural?: string): string {
  const pluralForm = plural ?? `${singular}s`;
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

/**
 * Generate a summary text for a tool group
 * Examples:
 *   "3 notes"
 *   "2 calendar and 1 tasks"
 *   "5 tools from 3 categories"
 */
export function generateToolGroupSummary(toolNames: string[]): string {
  const grouped = groupToolsByCategory(toolNames);
  const categoryEntries = Array.from(grouped.entries());

  // If only one category, show specific category name
  if (categoryEntries.length === 1) {
    const [category, tools] = categoryEntries[0];
    const categoryLabel = categoryInfo[category].label.toLowerCase();
    const count = tools.length;
    // Proper pluralization: "1 note" or "2 notes"
    return count === 1 ? `1 ${categoryLabel.slice(0, -1)}` : `${count} ${categoryLabel}`;
  }

  // If 2-3 categories, list them
  if (categoryEntries.length <= 3) {
    const parts = categoryEntries.map(([category, tools]) => {
      const categoryLabel = categoryInfo[category].label.toLowerCase();
      const count = tools.length;
      // Proper pluralization for each category
      return count === 1 ? `1 ${categoryLabel.slice(0, -1)}` : `${count} ${categoryLabel}`;
    });

    if (parts.length === 2) {
      return `${parts[0]} and ${parts[1]}`;
    }

    return parts.slice(0, -1).join(', ') + ', and ' + parts[parts.length - 1];
  }

  // If many categories, show generic summary
  return `${formatToolCount(toolNames.length)} from ${categoryEntries.length} categories`;
}
