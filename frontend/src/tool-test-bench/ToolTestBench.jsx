import React from 'react';
import { toolUIsByName, ToolFallback } from '../chat/ToolUI';

// Sample data for testing different tool states
const sampleToolCalls = [
  {
    name: 'add_or_update_note',
    title: 'Note Tool - Running',
    args: {
      title: 'Meeting Notes',
      content: 'Discussed the Q4 roadmap and budget allocations. Action items include...',
      include_in_prompt: true,
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'add_or_update_note',
    title: 'Note Tool - Complete',
    args: {
      title: 'Shopping List',
      content: 'Milk, Eggs, Bread, Coffee',
      include_in_prompt: false,
    },
    result: "Note 'Shopping List' has been created successfully.",
    status: { type: 'complete' },
  },
  {
    name: 'get_note',
    title: 'Get Note - Running',
    args: {
      title: 'Meeting Notes',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'get_note',
    title: 'Get Note - Found (Included in Prompts)',
    args: {
      title: 'Project Roadmap',
    },
    result: JSON.stringify({
      exists: true,
      title: 'Project Roadmap',
      content:
        'Q1: Focus on user authentication and core features\nQ2: Implement advanced search and filtering\nQ3: Mobile app development\nQ4: Performance optimization and scaling',
      include_in_prompt: true,
    }),
    status: { type: 'complete' },
  },
  {
    name: 'get_note',
    title: 'Get Note - Found (Not Included in Prompts)',
    args: {
      title: 'Personal Shopping List',
    },
    result: JSON.stringify({
      exists: true,
      title: 'Personal Shopping List',
      content:
        'Groceries:\n- Organic milk\n- Free-range eggs\n- Whole grain bread\n- Fair trade coffee\n- Fresh vegetables for the week',
      include_in_prompt: false,
    }),
    status: { type: 'complete' },
  },
  {
    name: 'get_note',
    title: 'Get Note - Not Found',
    args: {
      title: 'Nonexistent Note',
    },
    result: JSON.stringify({
      exists: false,
      title: null,
      content: null,
      include_in_prompt: null,
    }),
    status: { type: 'complete' },
  },
  {
    name: 'get_note',
    title: 'Get Note - Error',
    args: {
      title: 'Error Test Note',
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'get_note',
    title: 'Get Note - Malformed JSON Response',
    args: {
      title: 'Test Note',
    },
    result: 'This is not valid JSON, should fallback gracefully',
    status: { type: 'complete' },
  },
  {
    name: 'list_notes',
    title: 'List Notes - Running',
    args: {},
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'list_notes',
    title: 'List Notes - Complete with Notes',
    args: {},
    result: JSON.stringify([
      {
        title: 'Project Roadmap',
        content_preview:
          'Q1: Focus on user authentication and core features. Q2: Implement advanced search and filtering...',
        include_in_prompt: true,
      },
      {
        title: 'Meeting Notes - Team Standup',
        content_preview:
          'Discussed sprint progress, blockers, and upcoming deliverables. Action items include...',
        include_in_prompt: true,
      },
      {
        title: 'Personal Shopping List',
        content_preview:
          'Groceries: Organic milk, Free-range eggs, Whole grain bread, Fair trade coffee...',
        include_in_prompt: false,
      },
      {
        title: 'Vacation Planning',
        content_preview:
          'Summer trip ideas: Beach house rental, Mountain hiking, City exploration tours...',
        include_in_prompt: false,
      },
    ]),
    status: { type: 'complete' },
  },
  {
    name: 'list_notes',
    title: 'List Notes - Filtered (Include in Prompt Only)',
    args: {
      include_in_prompt_only: true,
    },
    result: JSON.stringify([
      {
        title: 'Project Roadmap',
        content_preview:
          'Q1: Focus on user authentication and core features. Q2: Implement advanced search...',
        include_in_prompt: true,
      },
      {
        title: 'Important Decisions Log',
        content_preview:
          'Architecture decisions, technology choices, and their rationale. Key decisions include...',
        include_in_prompt: true,
      },
    ]),
    status: { type: 'complete' },
  },
  {
    name: 'list_notes',
    title: 'List Notes - Empty Results',
    args: {},
    result: JSON.stringify([]),
    status: { type: 'complete' },
  },
  {
    name: 'list_notes',
    title: 'List Notes - Empty Results (Filtered)',
    args: {
      include_in_prompt_only: true,
    },
    result: JSON.stringify([]),
    status: { type: 'complete' },
  },
  {
    name: 'list_notes',
    title: 'List Notes - Error',
    args: {},
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'list_notes',
    title: 'List Notes - Malformed JSON Response',
    args: {},
    result: 'This is not valid JSON, should fallback gracefully',
    status: { type: 'complete' },
  },
  {
    name: 'delete_note',
    title: 'Delete Note - Running',
    args: {
      title: 'Old Meeting Notes',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'delete_note',
    title: 'Delete Note - Success',
    args: {
      title: 'Shopping List',
    },
    result: JSON.stringify({
      success: true,
      message: "Note 'Shopping List' deleted successfully.",
    }),
    status: { type: 'complete' },
  },
  {
    name: 'delete_note',
    title: 'Delete Note - Not Found',
    args: {
      title: 'Nonexistent Note',
    },
    result: JSON.stringify({
      success: false,
      message: "Note 'Nonexistent Note' not found.",
    }),
    status: { type: 'complete' },
  },
  {
    name: 'delete_note',
    title: 'Delete Note - Error',
    args: {
      title: 'Error Test Note',
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'delete_note',
    title: 'Delete Note - Malformed JSON Response',
    args: {
      title: 'Test Note',
    },
    result: 'This is not valid JSON, should fallback gracefully',
    status: { type: 'complete' },
  },
  {
    name: 'search_documents',
    title: 'Search Documents - Running',
    args: {
      query: 'project roadmap 2024',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'search_documents',
    title: 'Search Documents - Complete',
    args: {
      query: 'vacation policy',
    },
    result: [
      {
        title: 'HR Policy Document',
        snippet: '...employees are entitled to 15 days of paid vacation...',
      },
      {
        title: 'Employee Handbook',
        snippet: '...vacation requests must be submitted 2 weeks in advance...',
      },
    ],
    status: { type: 'complete' },
  },
  {
    name: 'schedule_reminder',
    title: 'Schedule Reminder - Running',
    args: {
      message: 'Call the dentist for annual checkup',
      time: '2024-12-20T14:00:00',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'schedule_reminder',
    title: 'Schedule Reminder - Complete',
    args: {
      message: 'Team standup meeting',
      time: '2024-12-21T09:30:00',
      recurring: false,
    },
    result: 'Reminder scheduled successfully for December 21, 2024 at 9:30 AM',
    status: { type: 'complete' },
  },
  {
    name: 'schedule_reminder',
    title: 'Schedule Reminder - Recurring',
    args: {
      message: 'Take daily vitamins',
      time: '2024-12-20T08:00:00',
      recurring: true,
    },
    result: 'Recurring reminder set up successfully',
    status: { type: 'complete' },
  },
  {
    name: 'schedule_reminder',
    title: 'Schedule Reminder - Error',
    args: {
      message: 'Invalid time test',
      time: 'invalid-time-format',
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'schedule_action',
    title: 'Schedule Action - Running',
    args: {
      action_name: 'Send weekly report',
      execution_time: '2024-12-23T10:00:00',
      action_data: {
        report_type: 'weekly_summary',
        recipients: ['manager@company.com', 'team@company.com'],
        format: 'pdf',
      },
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'schedule_action',
    title: 'Schedule Action - Complete',
    args: {
      action_name: 'Database backup',
      execution_time: '2024-12-24T02:00:00',
      action_data: {
        backup_type: 'full',
        retention_days: 30,
        compress: true,
      },
      recurring: false,
    },
    result: 'Action "Database backup" scheduled successfully for December 24, 2024 at 2:00 AM',
    status: { type: 'complete' },
  },
  {
    name: 'schedule_action',
    title: 'Schedule Action - Recurring',
    args: {
      action_name: 'System health check',
      execution_time: '2024-12-20T06:00:00',
      action_data: {
        check_type: 'comprehensive',
        alert_threshold: 85,
        services: ['database', 'api_server', 'cache', 'queue'],
      },
      recurring: true,
    },
    result: 'Recurring action "System health check" set up successfully',
    status: { type: 'complete' },
  },
  {
    name: 'schedule_action',
    title: 'Schedule Action - Simple Parameters',
    args: {
      action_name: 'Clean temporary files',
      execution_time: '2024-12-25T01:00:00',
      action_data: {
        directory: '/tmp',
        older_than_days: 7,
      },
    },
    result: 'Cleanup action scheduled successfully',
    status: { type: 'complete' },
  },
  {
    name: 'schedule_action',
    title: 'Schedule Action - Complex Nested Data',
    args: {
      action_name: 'Deploy application update',
      execution_time: '2024-12-26T20:00:00',
      action_data: {
        version: '2.1.0',
        environments: ['staging', 'production'],
        rollback_config: {
          enabled: true,
          timeout_minutes: 10,
        },
        notifications: {
          slack_channel: '#deployments',
          email_list: ['devops@company.com'],
        },
      },
      recurring: false,
    },
    result: 'Deployment action configured and scheduled',
    status: { type: 'complete' },
  },
  {
    name: 'schedule_action',
    title: 'Schedule Action - No Parameters',
    args: {
      action_name: 'Restart cache service',
      execution_time: '2024-12-27T03:30:00',
      action_data: {},
    },
    result: 'Simple action scheduled without parameters',
    status: { type: 'complete' },
  },
  {
    name: 'schedule_action',
    title: 'Schedule Action - Error',
    args: {
      action_name: 'Invalid action test',
      execution_time: 'not-a-valid-time',
      action_data: {
        test_param: 'value',
      },
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'add_calendar_event',
    title: 'Calendar Event - Running',
    args: {
      calendar_url: 'https://cal.example.com/personal',
      summary: 'Doctor Appointment',
      start_time: '2024-12-20T14:30:00',
      end_time: '2024-12-20T15:30:00',
      description: 'Annual checkup with Dr. Smith',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'add_calendar_event',
    title: 'Calendar Event - Complete',
    args: {
      calendar_url: 'https://cal.example.com/work',
      summary: 'Team Meeting',
      start_time: '2024-12-21T10:00:00',
      end_time: '2024-12-21T11:00:00',
      description: 'Weekly team standup and project updates',
    },
    result: 'Calendar event "Team Meeting" has been created successfully.',
    status: { type: 'complete' },
  },
  {
    name: 'add_calendar_event',
    title: 'Calendar Event - All Day',
    args: {
      calendar_url: 'https://cal.example.com/personal',
      summary: 'Vacation Day',
      start_time: '2024-12-25T00:00:00',
      end_time: '2024-12-25T23:59:59',
      all_day: true,
      description: 'Christmas Day - Family time',
    },
    result: 'All-day event created successfully.',
    status: { type: 'complete' },
  },
  {
    name: 'add_calendar_event',
    title: 'Calendar Event - Multi-day All Day',
    args: {
      calendar_url: 'https://cal.example.com/personal',
      summary: 'Summer Vacation',
      start_time: '2024-07-15T00:00:00',
      end_time: '2024-07-22T23:59:59',
      all_day: true,
      description: 'Family trip to the beach',
    },
    result: 'Multi-day event created successfully.',
    status: { type: 'complete' },
  },
  {
    name: 'add_calendar_event',
    title: 'Calendar Event - Recurring',
    args: {
      calendar_url: 'https://cal.example.com/work',
      summary: 'Daily Standup',
      start_time: '2024-12-20T09:00:00',
      end_time: '2024-12-20T09:30:00',
      description: 'Daily team sync meeting',
      recurrence_rule: 'FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR',
    },
    result: 'Recurring event created successfully.',
    status: { type: 'complete' },
  },
  {
    name: 'add_calendar_event',
    title: 'Calendar Event - Error',
    args: {
      calendar_url: 'https://cal.example.com/invalid',
      summary: 'Failed Event',
      start_time: 'invalid-date-format',
      end_time: '2024-12-20T15:30:00',
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'search_calendar_events',
    title: 'Search Calendar Events - Running',
    args: {
      search_text: 'meeting',
      date_range_start: '2024-12-20',
      date_range_end: '2024-12-25',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'search_calendar_events',
    title: 'Search Calendar Events - Complete with Results',
    args: {
      search_text: 'team',
      date_range_start: '2024-12-20',
      max_results: 5,
    },
    result: `Found 3 event(s):

1. Team Standup Meeting
   Start: 2024-12-21 09:00 PST
   End: 2024-12-21 09:30 PST
   UID: team-standup-123-456
   Calendar: https://cal.example.com/work

2. Team Building Event
   Start: 2024-12-22 14:00 PST
   End: 2024-12-22 17:00 PST
   UID: team-building-789-012
   Calendar: https://cal.example.com/work

3. Team Retrospective
   Start: 2024-12-23 15:00 PST
   End: 2024-12-23 16:00 PST
   UID: team-retro-345-678
   Calendar: https://cal.example.com/work`,
    status: { type: 'complete' },
  },
  {
    name: 'search_calendar_events',
    title: 'Search Calendar Events - No Results',
    args: {
      search_text: 'nonexistent',
      date_range_start: '2024-12-20',
      date_range_end: '2024-12-25',
    },
    result: 'No events found matching the search criteria.',
    status: { type: 'complete' },
  },
  {
    name: 'search_calendar_events',
    title: 'Search Calendar Events - Date Range Only',
    args: {
      date_range_start: '2024-12-20',
      date_range_end: '2024-12-22',
    },
    result: `Found 2 event(s):

1. Doctor Appointment
   Start: 2024-12-20 14:30 PST
   End: 2024-12-20 15:30 PST
   UID: doctor-appt-456-789
   Calendar: https://cal.example.com/personal

2. Vacation Day
   Start: 2024-12-21 00:00 PST
   End: 2024-12-21 23:59 PST
   UID: vacation-123-456
   Calendar: https://cal.example.com/personal`,
    status: { type: 'complete' },
  },
  {
    name: 'search_calendar_events',
    title: 'Search Calendar Events - Error',
    args: {
      search_text: 'meeting',
      date_range_start: '2024-12-20',
    },
    result: 'Error: CalDAV is not configured. Cannot search calendar events.',
    status: { type: 'incomplete', reason: 'error' },
  },
  // Calendar modification tools
  {
    name: 'modify_calendar_event',
    title: 'Modify Calendar Event - Running',
    args: {
      uid: 'event-12345-abcdef',
      calendar_url: 'https://cal.example.com/personal',
      new_summary: 'Updated Team Meeting',
      new_start_time: '2024-12-21T14:00:00+01:00',
      new_end_time: '2024-12-21T15:00:00+01:00',
      new_description: 'Updated agenda: Q4 review and 2025 planning',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'modify_calendar_event',
    title: 'Modify Calendar Event - Complete (Title Only)',
    args: {
      uid: 'team-standup-456-789',
      calendar_url: 'https://cal.example.com/work',
      new_summary: 'Daily Standup - Sprint 12',
    },
    result: "OK. Event 'Daily Standup' updated: title to 'Daily Standup - Sprint 12'.",
    status: { type: 'complete' },
  },
  {
    name: 'modify_calendar_event',
    title: 'Modify Calendar Event - Complete (Time Change)',
    args: {
      uid: 'doctor-appointment-789-012',
      calendar_url: 'https://cal.example.com/personal',
      new_start_time: '2024-12-22T15:30:00+01:00',
      new_end_time: '2024-12-22T16:30:00+01:00',
    },
    result:
      "OK. Event 'Doctor Appointment' updated: start time to 2024-12-22T15:30:00+01:00, end time to 2024-12-22T16:30:00+01:00.",
    status: { type: 'complete' },
  },
  {
    name: 'modify_calendar_event',
    title: 'Modify Calendar Event - Complete (Full Update)',
    args: {
      uid: 'meeting-abc-123',
      calendar_url: 'https://cal.example.com/work',
      new_summary: 'Project Kickoff Meeting',
      new_start_time: '2024-12-23T10:00:00+01:00',
      new_end_time: '2024-12-23T11:30:00+01:00',
      new_description:
        'Introduction to the new project, team assignments, and initial planning session.',
      recurrence_rule: 'FREQ=WEEKLY;BYDAY=MO',
    },
    result:
      "OK. Event 'Team Meeting' updated: title to 'Project Kickoff Meeting', start time to 2024-12-23T10:00:00+01:00, end time to 2024-12-23T11:30:00+01:00, description, recurrence rule.",
    status: { type: 'complete' },
  },
  {
    name: 'modify_calendar_event',
    title: 'Modify Calendar Event - Remove Recurrence',
    args: {
      uid: 'recurring-event-456',
      calendar_url: 'https://cal.example.com/personal',
      new_summary: 'One-time Workout Session',
      recurrence_rule: '',
    },
    result:
      "OK. Event 'Weekly Workout' updated: title to 'One-time Workout Session', removed recurrence.",
    status: { type: 'complete' },
  },
  {
    name: 'modify_calendar_event',
    title: 'Modify Calendar Event - Event Not Found',
    args: {
      uid: 'nonexistent-event-123',
      calendar_url: 'https://cal.example.com/personal',
      new_summary: 'Updated Title',
    },
    result: "Error: Event with UID 'nonexistent-event-123' not found in calendar.",
    status: { type: 'complete' },
  },
  {
    name: 'modify_calendar_event',
    title: 'Modify Calendar Event - Calendar Access Error',
    args: {
      uid: 'event-789-xyz',
      calendar_url: 'https://cal.example.com/restricted',
      new_summary: 'Updated Meeting',
    },
    result: 'Error: CalDAV configuration is incomplete. Cannot modify event.',
    status: { type: 'complete' },
  },
  {
    name: 'modify_calendar_event',
    title: 'Modify Calendar Event - System Error',
    args: {
      uid: 'system-error-event',
      calendar_url: 'https://cal.example.com/personal',
      new_summary: 'Test Event',
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'delete_calendar_event',
    title: 'Delete Calendar Event - Running',
    args: {
      uid: 'old-meeting-456-789',
      calendar_url: 'https://cal.example.com/work',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'delete_calendar_event',
    title: 'Delete Calendar Event - Success',
    args: {
      uid: 'cancelled-appointment-123',
      calendar_url: 'https://cal.example.com/personal',
    },
    result: "OK. Event 'Doctor Appointment' deleted from calendar.",
    status: { type: 'complete' },
  },
  {
    name: 'delete_calendar_event',
    title: 'Delete Calendar Event - Success (Work Event)',
    args: {
      uid: 'project-review-789',
      calendar_url: 'https://cal.example.com/work',
    },
    result: "OK. Event 'Project Review Meeting' deleted from calendar.",
    status: { type: 'complete' },
  },
  {
    name: 'delete_calendar_event',
    title: 'Delete Calendar Event - Success (Recurring Event)',
    args: {
      uid: 'weekly-standup-recurring',
      calendar_url: 'https://cal.example.com/work',
    },
    result: "OK. Event 'Weekly Team Standup' deleted from calendar.",
    status: { type: 'complete' },
  },
  {
    name: 'delete_calendar_event',
    title: 'Delete Calendar Event - Event Not Found',
    args: {
      uid: 'nonexistent-event-delete',
      calendar_url: 'https://cal.example.com/personal',
    },
    result: "Error: Event with UID 'nonexistent-event-delete' not found in calendar.",
    status: { type: 'complete' },
  },
  {
    name: 'delete_calendar_event',
    title: 'Delete Calendar Event - Calendar Access Error',
    args: {
      uid: 'restricted-event-123',
      calendar_url: 'https://cal.example.com/restricted',
    },
    result: 'Error: CalDAV configuration is incomplete. Cannot delete event.',
    status: { type: 'complete' },
  },
  {
    name: 'delete_calendar_event',
    title: 'Delete Calendar Event - System Error',
    args: {
      uid: 'system-error-delete',
      calendar_url: 'https://cal.example.com/personal',
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'unknown_tool',
    title: 'Unknown Tool - Error State',
    args: {
      some_param: 'value',
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'get_full_document_content',
    title: 'Get Document Content - Running',
    args: {
      document_id: 'doc-123-user-manual',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'get_full_document_content',
    title: 'Get Document Content - Short Text Document',
    args: {
      document_id: 'doc-456-meeting-notes',
    },
    result: `Team Meeting Notes - Q4 Planning

Attendees: Alice, Bob, Carol, David
Date: December 15, 2024

Agenda:
1. Review Q3 performance metrics
2. Discuss Q4 goals and objectives
3. Budget allocation for 2025
4. Team restructuring proposal

Key Decisions:
- Approved 15% budget increase for engineering team
- New product launch scheduled for March 2025
- Weekly check-ins to be implemented

Action Items:
- Alice: Prepare detailed budget proposal by Dec 20
- Bob: Research competitive analysis
- Carol: Draft team structure document
- David: Schedule follow-up meetings with stakeholders`,
    status: { type: 'complete' },
  },
  {
    name: 'get_full_document_content',
    title: 'Get Document Content - Long Document (Truncated)',
    args: {
      document_id: 'doc-789-technical-specification',
    },
    result: `Technical Specification Document
Project: Family Assistant Platform
Version: 2.1.0
Last Updated: December 2024

Table of Contents:
1. Introduction
2. System Architecture
3. API Specifications
4. Database Schema
5. Security Requirements
6. Performance Metrics
7. Testing Procedures
8. Deployment Guidelines

1. Introduction
================

The Family Assistant Platform is a comprehensive solution designed to centralize family information management and automate various household tasks. This document outlines the technical specifications, architecture decisions, and implementation details for version 2.1.0.

The system provides multiple interfaces including Telegram bot integration, web UI, and email webhook processing. It uses a modular architecture built with Python, FastAPI, and SQLAlchemy, supporting both SQLite and PostgreSQL databases.

Key Features:
- Multi-interface support (Telegram, Web, Email)
- Intelligent document processing and indexing
- Calendar integration with CalDAV support
- Task scheduling and reminder system
- Event-driven architecture
- Vector-based semantic search
- Background task processing
- Configurable LLM integration

2. System Architecture
======================

The application follows a layered architecture pattern:

2.1 Presentation Layer
- Web UI (React + Vite)
- Telegram Bot Interface
- Email Webhook Handler
- REST API Endpoints

2.2 Business Logic Layer
- Processing Service
- Tool System
- Context Providers
- Event System

2.3 Data Layer
- Repository Pattern Implementation
- Database Abstraction
- Vector Storage
- File System Integration

2.4 Infrastructure Layer
- Task Queue System
- Background Workers
- External Service Integrations
- Configuration Management

The system uses dependency injection for loose coupling and follows SOLID principles throughout the codebase. All components are designed to be testable and maintainable.

3. API Specifications
=====================

3.1 REST API Endpoints

Authentication:
All API endpoints require authentication via session cookies or API tokens.

Base URL: https://api.familyassistant.com/v1

3.1.1 Document Management
GET /documents - List all documents
POST /documents - Upload new document
GET /documents/{id} - Get document metadata
GET /documents/{id}/content - Get full document content
PUT /documents/{id} - Update document
DELETE /documents/{id} - Delete document

3.1.2 Note Management  
GET /notes - List notes with optional filtering
POST /notes - Create new note
GET /notes/{id} - Get specific note
PUT /notes/{id} - Update note
DELETE /notes/{id} - Delete note

3.1.3 Calendar Integration
GET /calendar/events - Search calendar events
POST /calendar/events - Create new event
PUT /calendar/events/{id} - Update event
DELETE /calendar/events/{id} - Delete event

3.1.4 Task Management
GET /tasks - List pending tasks
POST /tasks - Schedule new task
GET /tasks/{id} - Get task status
DELETE /tasks/{id} - Cancel task

4. Database Schema
==================

The application uses SQLAlchemy ORM with Alembic migrations for schema management.

4.1 Core Tables

documents:
- id (UUID, primary key)
- title (VARCHAR(255))
- content (TEXT)
- content_type (VARCHAR(50))
- file_path (VARCHAR(500))
- upload_time (TIMESTAMP)
- last_modified (TIMESTAMP)
- metadata (JSON)

notes:
- id (UUID, primary key)  
- title (VARCHAR(255))
- content (TEXT)
- include_in_prompt (BOOLEAN)
- created_at (TIMESTAMP)
- updated_at (TIMESTAMP)
- tags (JSON)

calendar_events:
- id (UUID, primary key)
- summary (VARCHAR(255))
- description (TEXT)
- start_time (TIMESTAMP)
- end_time (TIMESTAMP)
- all_day (BOOLEAN)
- calendar_url (VARCHAR(500))
- uid (VARCHAR(255))
- recurrence_rule (TEXT)

This document continues for several more sections covering security, performance, testing, and deployment procedures...`,
    status: { type: 'complete' },
  },
  {
    name: 'get_full_document_content',
    title: 'Get Document Content - Markdown Document',
    args: {
      document_id: 'doc-101-readme',
    },
    result: `# Family Assistant

A comprehensive family information management and automation platform.

## Features

- **Multi-Interface Support**: Telegram bot, Web UI, and Email integration
- **Document Management**: Intelligent processing and semantic search
- **Calendar Integration**: CalDAV support with event management
- **Task Automation**: Scheduling and reminder system
- **Context-Aware**: Dynamic prompt injection based on user data

## Quick Start

\`\`\`bash
# Install dependencies
uv pip install -e '.[dev]'

# Run development server
poe dev
\`\`\`

## Configuration

Create a \`config.yaml\` file:

\`\`\`yaml
service_profiles:
  - id: "default_assistant"
    llm_client:
      provider: "openai"
      model: "gpt-4"
    tools_config:
      enable_local_tools: true
\`\`\`

## Architecture

The application follows a modular architecture:

1. **Entry Point** (\`__main__.py\`)
2. **Core Assistant** (\`assistant.py\`)  
3. **Processing Layer** (\`processing.py\`)
4. **Storage Layer** (\`storage/\`)
5. **Tools System** (\`tools/\`)
6. **Web Interface** (\`web/\`)

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests: \`poe test\`
5. Submit a pull request

## License

MIT License - see LICENSE file for details.`,
    status: { type: 'complete' },
  },
  {
    name: 'get_full_document_content',
    title: 'Get Document Content - Code File',
    args: {
      document_id: 'doc-202-python-script',
    },
    result: `#!/usr/bin/env python3
"""
Data processing script for Family Assistant
Handles document indexing and vector embeddings
"""

import asyncio
import logging
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass

from sqlalchemy import select
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


@dataclass
class DocumentChunk:
    """Represents a chunk of document content for embedding."""
    content: str
    metadata: dict
    document_id: str
    chunk_index: int


class DocumentProcessor:
    """Processes documents for semantic search indexing."""
    
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model = SentenceTransformer(model_name)
        self.chunk_size = 512
        self.overlap = 50
    
    async def process_document(self, document_path: Path) -> List[DocumentChunk]:
        """
        Process a document into chunks for embedding.
        
        Args:
            document_path: Path to the document file
            
        Returns:
            List of document chunks ready for embedding
        """
        content = await self._read_document(document_path)
        chunks = self._create_chunks(content)
        
        processed_chunks = []
        for i, chunk_text in enumerate(chunks):
            chunk = DocumentChunk(
                content=chunk_text,
                metadata={
                    "source": str(document_path),
                    "chunk_size": len(chunk_text),
                    "processing_time": asyncio.get_event_loop().time()
                },
                document_id=document_path.stem,
                chunk_index=i
            )
            processed_chunks.append(chunk)
        
        return processed_chunks
    
    async def _read_document(self, path: Path) -> str:
        """Read document content from file."""
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning(f"Failed to read {path} as UTF-8, trying latin-1")
            return path.read_text(encoding="latin-1")
    
    def _create_chunks(self, content: str) -> List[str]:
        """Split content into overlapping chunks."""
        if len(content) <= self.chunk_size:
            return [content]
        
        chunks = []
        start = 0
        
        while start < len(content):
            end = start + self.chunk_size
            chunk = content[start:end]
            
            # Try to break at word boundary
            if end < len(content):
                last_space = chunk.rfind(' ')
                if last_space > self.chunk_size * 0.8:
                    end = start + last_space
                    chunk = content[start:end]
            
            chunks.append(chunk.strip())
            start = end - self.overlap
        
        return chunks
    
    def generate_embeddings(self, chunks: List[DocumentChunk]) -> List[List[float]]:
        """Generate embeddings for document chunks."""
        contents = [chunk.content for chunk in chunks]
        embeddings = self.model.encode(contents, convert_to_numpy=True)
        return embeddings.tolist()


async def main():
    """Main processing function."""
    processor = DocumentProcessor()
    documents_dir = Path("./documents")
    
    for doc_path in documents_dir.glob("*.txt"):
        logger.info(f"Processing {doc_path}")
        chunks = await processor.process_document(doc_path)
        embeddings = processor.generate_embeddings(chunks)
        
        print(f"Generated {len(embeddings)} embeddings for {doc_path}")
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            print(f"  Chunk {i}: {len(chunk.content)} chars, {len(embedding)} dims")


if __name__ == "__main__":
    asyncio.run(main())`,
    status: { type: 'complete' },
  },
  {
    name: 'get_full_document_content',
    title: 'Get Document Content - Document Not Found',
    args: {
      document_id: 'doc-nonexistent-file',
    },
    result: 'Error: Document with ID "doc-nonexistent-file" not found in the system.',
    status: { type: 'complete' },
  },
  {
    name: 'get_full_document_content',
    title: 'Get Document Content - Permission Error',
    args: {
      document_id: 'doc-restricted-access',
    },
    result: 'Error: Access denied. You do not have permission to read this document.',
    status: { type: 'complete' },
  },
  {
    name: 'get_full_document_content',
    title: 'Get Document Content - System Error',
    args: {
      document_id: 'doc-system-failure',
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Running',
    args: {
      url: 'https://docs.example.com/user-manual.pdf',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Success',
    args: {
      url: 'https://api.github.com/repos/owner/repo/contents/README.md',
    },
    result: 'Document successfully ingested and indexed. Added 1 document with 15 chunks.',
    status: { type: 'complete' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Success with Metadata',
    args: {
      url: 'https://example.com/research-paper.pdf',
      metadata: {
        title: 'AI Research Paper 2024',
        author: 'Dr. Jane Smith',
        category: 'research',
        tags: ['AI', 'machine learning', 'research'],
      },
    },
    result:
      'Document "AI Research Paper 2024" has been successfully processed and added to the knowledge base.',
    status: { type: 'complete' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Web Page',
    args: {
      url: 'https://blog.example.com/how-to-setup-development-environment',
    },
    result: 'Web page content successfully ingested. Extracted 2,450 words across 8 sections.',
    status: { type: 'complete' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Long URL',
    args: {
      url: 'https://very-long-domain-name-for-testing-url-truncation.example.com/path/to/very/deep/nested/directory/structure/with/multiple/segments/document-with-very-long-filename.pdf?version=2024&format=latest&include_metadata=true',
    },
    result: 'Document ingested successfully from remote server.',
    status: { type: 'complete' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Word Document',
    args: {
      url: 'https://company.sharepoint.com/sites/docs/proposal.docx',
      metadata: {
        department: 'Sales',
        confidential: true,
      },
    },
    result: 'Word document processed successfully. Extracted text and formatting information.',
    status: { type: 'complete' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Markdown File',
    args: {
      url: 'https://raw.githubusercontent.com/owner/repo/main/CHANGELOG.md',
    },
    result: 'Markdown document ingested and structured content extracted.',
    status: { type: 'complete' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Access Denied',
    args: {
      url: 'https://private.example.com/confidential-document.pdf',
    },
    result:
      'Error: Access denied. Unable to retrieve document from the specified URL. Please check permissions.',
    status: { type: 'complete' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Invalid URL',
    args: {
      url: 'not-a-valid-url',
    },
    result: 'Error: Invalid URL format. Please provide a valid HTTP or HTTPS URL.',
    status: { type: 'complete' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Network Error',
    args: {
      url: 'https://unreachable-server.example.com/document.pdf',
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'ingest_document_from_url',
    title: 'Ingest Document from URL - Unsupported Format',
    args: {
      url: 'https://example.com/video-file.mp4',
    },
    result:
      'Error: Unsupported file format. Only text documents, PDFs, and web pages are supported.',
    status: { type: 'complete' },
  },
  {
    name: 'get_message_history',
    title: 'Get Message History - Running',
    args: {
      interface_type: 'telegram',
      limit: 10,
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'get_message_history',
    title: 'Get Message History - Complete with Messages',
    args: {
      interface_type: 'web',
      limit: 5,
      user_name: 'alice',
    },
    result: JSON.stringify([
      {
        role: 'user',
        content: 'Can you help me organize my notes for the upcoming project meeting?',
        timestamp: '2024-12-20T14:30:00Z',
        interface_type: 'web',
        user_name: 'alice',
      },
      {
        role: 'assistant',
        content:
          "I'd be happy to help you organize your notes! Let me search for any existing notes related to your project and then we can create a structured agenda.",
        timestamp: '2024-12-20T14:30:15Z',
        interface_type: 'web',
        user_name: 'alice',
      },
      {
        role: 'user',
        content: 'Great! I need to cover the Q4 roadmap, budget allocations, and team assignments.',
        timestamp: '2024-12-20T14:31:00Z',
        interface_type: 'web',
        user_name: 'alice',
      },
      {
        role: 'assistant',
        content:
          "Perfect! I'll help you create a comprehensive meeting agenda covering those three key areas. Let me start by checking your existing notes and then we can structure everything properly.",
        timestamp: '2024-12-20T14:31:20Z',
        interface_type: 'web',
        user_name: 'alice',
      },
      {
        role: 'system',
        content: 'Context updated with project-related notes and calendar events.',
        timestamp: '2024-12-20T14:31:25Z',
        interface_type: 'web',
        user_name: 'system',
      },
    ]),
    status: { type: 'complete' },
  },
  {
    name: 'get_message_history',
    title: 'Get Message History - Telegram Conversation',
    args: {
      interface_type: 'telegram',
      limit: 3,
      user_name: 'bob',
    },
    result: JSON.stringify([
      {
        role: 'user',
        content: '/remind me to call mom tomorrow at 2pm',
        timestamp: '2024-12-19T18:45:00Z',
        interface_type: 'telegram',
        user_name: 'bob',
      },
      {
        role: 'assistant',
        content:
          "I've scheduled a reminder for you to call mom tomorrow (December 20th) at 2:00 PM. You'll receive a notification when it's time!",
        timestamp: '2024-12-19T18:45:05Z',
        interface_type: 'telegram',
        user_name: 'bob',
      },
      {
        role: 'user',
        content: 'Thanks! Also, can you add "buy groceries" to my shopping list note?',
        timestamp: '2024-12-19T18:46:00Z',
        interface_type: 'telegram',
        user_name: 'bob',
      },
    ]),
    status: { type: 'complete' },
  },
  {
    name: 'get_message_history',
    title: 'Get Message History - Mixed Interfaces',
    args: {
      limit: 8,
    },
    result: JSON.stringify([
      {
        role: 'user',
        content: "What's on my calendar for next week?",
        timestamp: '2024-12-20T09:15:00Z',
        interface_type: 'telegram',
        user_name: 'carol',
      },
      {
        role: 'assistant',
        content: 'Let me check your calendar for next week...',
        timestamp: '2024-12-20T09:15:05Z',
        interface_type: 'telegram',
        user_name: 'carol',
      },
      {
        role: 'user',
        content:
          'I need to schedule a team meeting to discuss the new project requirements. Can you help me find a time that works for everyone?',
        timestamp: '2024-12-20T11:30:00Z',
        interface_type: 'web',
        user_name: 'david',
      },
      {
        role: 'assistant',
        content:
          "I'll help you schedule that team meeting. Let me search for available time slots and check everyone's calendars.",
        timestamp: '2024-12-20T11:30:10Z',
        interface_type: 'web',
        user_name: 'david',
      },
      {
        role: 'user',
        content: 'Please create a note about the client feedback we received today.',
        timestamp: '2024-12-20T16:20:00Z',
        interface_type: 'email',
        user_name: 'eve',
      },
      {
        role: 'assistant',
        content:
          'I\'ve created a new note titled "Client Feedback - December 20, 2024" with the key points from today\'s discussion.',
        timestamp: '2024-12-20T16:20:25Z',
        interface_type: 'email',
        user_name: 'eve',
      },
    ]),
    status: { type: 'complete' },
  },
  {
    name: 'get_message_history',
    title: 'Get Message History - Long Message Content',
    args: {
      interface_type: 'web',
      limit: 2,
    },
    result: JSON.stringify([
      {
        role: 'user',
        content:
          'I need to prepare a comprehensive project proposal for the executive team. The proposal should include a detailed analysis of our current market position, competitive landscape, projected costs and benefits, timeline with key milestones, risk assessment and mitigation strategies, resource requirements including personnel and technology needs, and expected ROI calculations. This is a critical presentation that could determine the future direction of our product development efforts for the next two years.',
        timestamp: '2024-12-20T13:00:00Z',
        interface_type: 'web',
        user_name: 'frank',
      },
      {
        role: 'assistant',
        content:
          "I'll help you create a comprehensive project proposal for the executive team. This is indeed a critical presentation, and I'll make sure we cover all the essential elements you mentioned. Let me break this down into structured sections: 1) Executive Summary, 2) Market Analysis, 3) Competitive Landscape, 4) Financial Projections, 5) Implementation Timeline, 6) Risk Assessment, 7) Resource Planning, and 8) ROI Analysis. I'll start by gathering relevant information from your existing notes and documents to build a solid foundation for each section.",
        timestamp: '2024-12-20T13:00:30Z',
        interface_type: 'web',
        user_name: 'frank',
      },
    ]),
    status: { type: 'complete' },
  },
  {
    name: 'get_message_history',
    title: 'Get Message History - Empty Results',
    args: {
      interface_type: 'telegram',
      user_name: 'nonexistent_user',
    },
    result: JSON.stringify([]),
    status: { type: 'complete' },
  },
  {
    name: 'get_message_history',
    title: 'Get Message History - No Filter (All Messages)',
    args: {
      limit: 15,
    },
    result: JSON.stringify([
      {
        role: 'user',
        content: 'Hello!',
        timestamp: '2024-12-20T08:00:00Z',
        interface_type: 'telegram',
        user_name: 'alice',
      },
      {
        role: 'assistant',
        content: 'Hello! How can I help you today?',
        timestamp: '2024-12-20T08:00:05Z',
        interface_type: 'telegram',
        user_name: 'alice',
      },
      {
        role: 'system',
        content: 'User session started',
        timestamp: '2024-12-20T08:00:00Z',
        interface_type: 'telegram',
        user_name: 'system',
      },
    ]),
    status: { type: 'complete' },
  },
  {
    name: 'get_message_history',
    title: 'Get Message History - Error',
    args: {
      interface_type: 'telegram',
      limit: 10,
    },
    result: null,
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'get_message_history',
    title: 'Get Message History - Malformed JSON Response',
    args: {
      interface_type: 'web',
      limit: 5,
    },
    result: 'This is not valid JSON, should fallback gracefully to show raw text',
    status: { type: 'complete' },
  },
  // Event system tools
  {
    name: 'query_recent_events',
    title: 'Query Recent Events - Running',
    args: {
      source_id: 'home_assistant',
      hours: 24,
      limit: 10,
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'query_recent_events',
    title: 'Query Recent Events - Complete with Results',
    args: {
      source_id: 'home_assistant',
      hours: 6,
      limit: 5,
    },
    result: JSON.stringify({
      count: 3,
      events: [
        {
          event_id: 'evt_001',
          source_id: 'home_assistant',
          timestamp: '2024-12-20T14:30:00Z',
          event_data: {
            entity_id: 'sensor.temperature',
            state: '22.5',
            attributes: { unit: '°C', friendly_name: 'Living Room Temperature' },
          },
          triggered_listeners: ['listener_temp_alert'],
        },
        {
          event_id: 'evt_002',
          source_id: 'home_assistant',
          timestamp: '2024-12-20T13:15:00Z',
          event_data: {
            entity_id: 'binary_sensor.front_door',
            state: 'off',
            attributes: { friendly_name: 'Front Door', device_class: 'door' },
          },
          triggered_listeners: [],
        },
        {
          event_id: 'evt_003',
          source_id: 'home_assistant',
          timestamp: '2024-12-20T12:45:00Z',
          event_data: {
            entity_id: 'light.bedroom',
            state: 'on',
            attributes: { brightness: 180, friendly_name: 'Bedroom Light' },
          },
          triggered_listeners: ['listener_light_automation'],
        },
      ],
    }),
    status: { type: 'complete' },
  },
  {
    name: 'query_recent_events',
    title: 'Query Recent Events - No Results',
    args: {
      source_id: 'indexing',
      hours: 1,
    },
    result: JSON.stringify({
      message: 'No events found in the specified time range.',
    }),
    status: { type: 'complete' },
  },
  {
    name: 'create_event_listener',
    title: 'Create Event Listener - Running',
    args: {
      name: 'Temperature Alert',
      source: 'home_assistant',
      conditions: [
        {
          field: 'entity_id',
          operator: 'equals',
          value: 'sensor.temperature',
        },
        {
          field: 'state',
          operator: 'greater_than',
          value: '25',
        },
      ],
      action_type: 'wake_llm',
      action_data: {
        message: 'Temperature is high: {state}°C',
      },
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'create_event_listener',
    title: 'Create Event Listener - Success',
    args: {
      name: 'Door Security Alert',
      source: 'home_assistant',
      conditions: [
        {
          field: 'entity_id',
          operator: 'equals',
          value: 'binary_sensor.front_door',
        },
        {
          field: 'state',
          operator: 'equals',
          value: 'on',
        },
      ],
      action_type: 'wake_llm',
      action_data: {
        message: 'Front door opened at {timestamp}',
        priority: 'high',
      },
      enabled: true,
      one_time: false,
    },
    result: JSON.stringify({
      success: true,
      listener_id: 'listener_123',
      message: 'Event listener "Door Security Alert" created successfully.',
    }),
    status: { type: 'complete' },
  },
  {
    name: 'list_event_listeners',
    title: 'List Event Listeners - Running',
    args: {
      source: 'home_assistant',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'list_event_listeners',
    title: 'List Event Listeners - Complete with Results',
    args: {
      enabled: true,
    },
    result: JSON.stringify({
      success: true,
      count: 3,
      listeners: [
        {
          id: 'listener_001',
          name: 'Temperature Monitor',
          source: 'home_assistant',
          enabled: true,
          one_time: false,
          daily_executions: 5,
          last_execution_at: '2024-12-20T14:30:00Z',
          created_at: '2024-12-15T10:00:00Z',
        },
        {
          id: 'listener_002',
          name: 'Door Security Alert',
          source: 'home_assistant',
          enabled: true,
          one_time: false,
          daily_executions: 2,
          last_execution_at: '2024-12-20T09:15:00Z',
          created_at: '2024-12-18T16:30:00Z',
        },
        {
          id: 'listener_003',
          name: 'One-time Test Listener',
          source: 'home_assistant',
          enabled: true,
          one_time: true,
          daily_executions: 0,
          last_execution_at: null,
          created_at: '2024-12-20T12:00:00Z',
        },
      ],
    }),
    status: { type: 'complete' },
  },
  {
    name: 'list_event_listeners',
    title: 'List Event Listeners - Empty Results',
    args: {
      source: 'indexing',
    },
    result: JSON.stringify({
      success: true,
      count: 0,
      listeners: [],
    }),
    status: { type: 'complete' },
  },
  {
    name: 'delete_event_listener',
    title: 'Delete Event Listener - Running',
    args: {
      listener_id: 'listener_456',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'delete_event_listener',
    title: 'Delete Event Listener - Success',
    args: {
      listener_id: 'listener_789',
    },
    result: JSON.stringify({
      success: true,
      message: 'Event listener deleted successfully.',
    }),
    status: { type: 'complete' },
  },
  {
    name: 'delete_event_listener',
    title: 'Delete Event Listener - Not Found',
    args: {
      listener_id: 'listener_nonexistent',
    },
    result: JSON.stringify({
      success: false,
      message: 'Event listener with ID "listener_nonexistent" not found.',
    }),
    status: { type: 'complete' },
  },
  {
    name: 'toggle_event_listener',
    title: 'Toggle Event Listener - Enable (Running)',
    args: {
      listener_id: 'listener_123',
      enabled: true,
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'toggle_event_listener',
    title: 'Toggle Event Listener - Enable Success',
    args: {
      listener_id: 'listener_456',
      enabled: true,
    },
    result: JSON.stringify({
      success: true,
      message: 'Event listener "Temperature Monitor" has been enabled.',
    }),
    status: { type: 'complete' },
  },
  {
    name: 'toggle_event_listener',
    title: 'Toggle Event Listener - Disable Success',
    args: {
      listener_id: 'listener_789',
      enabled: false,
    },
    result: JSON.stringify({
      success: true,
      message: 'Event listener "Door Alert" has been disabled.',
    }),
    status: { type: 'complete' },
  },
  {
    name: 'toggle_event_listener',
    title: 'Toggle Event Listener - Error',
    args: {
      listener_id: 'listener_invalid',
      enabled: true,
    },
    result: JSON.stringify({
      success: false,
      message: 'Event listener with ID "listener_invalid" not found.',
    }),
    status: { type: 'complete' },
  },

  // Event Validation Tools
  {
    name: 'test_event_listener',
    title: 'Test Event Listener - Running',
    args: {
      source: 'home_assistant',
      hours: 24,
      match_conditions: {
        entity_id: 'sensor.temperature',
      },
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'test_event_listener',
    title: 'Test Event Listener - Success with Matches',
    args: {
      source: 'indexing',
      hours: 48,
      match_conditions: {
        document_type: 'pdf',
        status: 'processed',
      },
    },
    result: JSON.stringify({
      matched_count: 15,
      total_tested: 32,
      message: 'Found 15 matching events out of 32 total events in the last 48 hours.',
      analysis: [
        'Most matches occurred during business hours (9-17)',
        'Peak activity on Tuesday and Wednesday',
        'Processing time averaged 2.3 seconds per document',
      ],
      matched_events: [
        {
          timestamp: '2024-01-15T14:30:00Z',
          event_data: {
            document_type: 'pdf',
            status: 'processed',
            file_name: 'quarterly_report.pdf',
            processing_time: 2.1,
          },
        },
        {
          timestamp: '2024-01-15T16:45:00Z',
          event_data: {
            document_type: 'pdf',
            status: 'processed',
            file_name: 'meeting_minutes.pdf',
            processing_time: 1.8,
          },
        },
        {
          timestamp: '2024-01-16T09:15:00Z',
          event_data: {
            document_type: 'pdf',
            status: 'processed',
            file_name: 'invoice_jan_2024.pdf',
            processing_time: 3.2,
          },
        },
      ],
    }),
    status: { type: 'complete' },
  },
  {
    name: 'test_event_listener',
    title: 'Test Event Listener - No Matches',
    args: {
      source: 'webhook',
      hours: 12,
      match_conditions: {
        event_type: 'payment_received',
        amount: { $gt: 1000 },
      },
    },
    result: JSON.stringify({
      matched_count: 0,
      total_tested: 8,
      message: 'No events matched the specified conditions in the last 12 hours.',
      analysis: [
        'Total webhook events received: 8',
        'No payment events exceeded $1000 threshold',
        'Largest payment was $750',
      ],
      matched_events: [],
    }),
    status: { type: 'complete' },
  },
  {
    name: 'test_event_listener',
    title: 'Test Event Listener - Error',
    args: {
      source: 'invalid_source',
      hours: 6,
      match_conditions: {},
    },
    result: JSON.stringify({
      error: 'Invalid event source: invalid_source',
      message: 'Supported sources are: home_assistant, indexing, webhook',
    }),
    status: { type: 'complete' },
  },
  {
    name: 'validate_event_listener_script',
    title: 'Validate Script - Running',
    args: {
      script_code: `
def process_event(event_data):
    temperature = event_data.get('temperature', 0)
    if temperature > 25:
        return {"action": "turn_on_ac", "temperature": temperature}
    return None
      `.trim(),
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'validate_event_listener_script',
    title: 'Validate Script - Success',
    args: {
      script_code: `
def process_event(event_data):
    sensor_value = event_data.get('value', 0)
    threshold = event_data.get('threshold', 50)
    
    if sensor_value > threshold:
        return {
            "alert": True,
            "message": f"Sensor value {sensor_value} exceeds threshold {threshold}",
            "severity": "high" if sensor_value > threshold * 1.5 else "medium"
        }
    return {"alert": False}
      `.trim(),
    },
    result: JSON.stringify({
      success: true,
      message: 'Script validation passed. Syntax is correct and function signature is valid.',
    }),
    status: { type: 'complete' },
  },
  {
    name: 'validate_event_listener_script',
    title: 'Validate Script - Syntax Error',
    args: {
      script_code: `
def process_event(event_data):
    temperature = event_data.get('temperature', 0
    if temperature > 25:
        return {"action": "turn_on_ac"}
    return None
      `.trim(),
    },
    result: JSON.stringify({
      success: false,
      error: 'SyntaxError: unexpected EOF while parsing',
      line: 2,
      message: 'Script contains syntax errors and cannot be executed.',
    }),
    status: { type: 'complete' },
  },
  {
    name: 'test_event_listener_script',
    title: 'Test Script - Running',
    args: {
      script_code: `
def process_event(event_data):
    motion_detected = event_data.get('motion', False)
    time_of_day = event_data.get('hour', 12)
    
    if motion_detected and (time_of_day < 7 or time_of_day > 22):
        return {
            "action": "turn_on_lights",
            "brightness": 30,
            "reason": "Motion detected during night hours"
        }
    return None
      `.trim(),
      sample_event: {
        motion: true,
        hour: 23,
        location: 'hallway',
        timestamp: '2024-01-15T23:15:00Z',
      },
      timeout: 5,
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'test_event_listener_script',
    title: 'Test Script - Success',
    args: {
      script_code: `
def process_event(event_data):
    door_state = event_data.get('state', 'closed')
    armed = event_data.get('armed', False)
    
    if door_state == 'open' and armed:
        return {
            "alert": True,
            "action": "send_notification",
            "message": "Door opened while system is armed!",
            "priority": "high"
        }
    return {"alert": False}
      `.trim(),
      sample_event: {
        state: 'open',
        armed: true,
        door_id: 'front_door',
        timestamp: '2024-01-15T14:30:00Z',
      },
      timeout: 10,
    },
    result: JSON.stringify({
      success: true,
      message: 'Script executed successfully with no errors.',
      result: {
        alert: true,
        action: 'send_notification',
        message: 'Door opened while system is armed!',
        priority: 'high',
      },
    }),
    status: { type: 'complete' },
  },
  {
    name: 'test_event_listener_script',
    title: 'Test Script - Returns None',
    args: {
      script_code: `
def process_event(event_data):
    temperature = event_data.get('temperature', 20)
    comfort_range = (18, 24)
    
    if comfort_range[0] <= temperature <= comfort_range[1]:
        return None  # No action needed
    
    return {"action": "adjust_temperature", "target": 22}
      `.trim(),
      sample_event: {
        temperature: 21,
        humidity: 45,
        room: 'living_room',
      },
      timeout: 3,
    },
    result: JSON.stringify({
      success: true,
      message: 'Script executed successfully with no errors.',
      result: null,
    }),
    status: { type: 'complete' },
  },
  {
    name: 'test_event_listener_script',
    title: 'Test Script - Runtime Error',
    args: {
      script_code: `
def process_event(event_data):
    # This will cause a runtime error
    value = event_data['missing_key']  # KeyError
    return {"value": value * 2}
      `.trim(),
      sample_event: {
        temperature: 25,
        timestamp: '2024-01-15T12:00:00Z',
      },
      timeout: 5,
    },
    result: JSON.stringify({
      success: false,
      message: 'Script execution failed with runtime error.',
      error: "KeyError: 'missing_key'",
    }),
    status: { type: 'complete' },
  },

  // Utility Tool UIs
  {
    name: 'get_user_documentation_content',
    title: 'Get User Documentation - Running',
    args: {
      filename: 'USER_GUIDE.md',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'get_user_documentation_content',
    title: 'Get User Documentation - Success',
    args: {
      filename: 'FEATURES.md',
    },
    result:
      '# Family Assistant Features\n\nThis document outlines the key features of the Family Assistant application:\n\n## Core Features\n- **Smart Chat Interface**: Interact with the assistant via Telegram or web interface\n- **Document Management**: Store, search, and retrieve documents and notes\n- **Calendar Integration**: Manage events and appointments\n- **Task Scheduling**: Set up automated tasks and reminders\n\n## Advanced Features\n- **Home Assistant Integration**: Control smart home devices\n- **Script Execution**: Run custom automation scripts\n- **Event Listeners**: Respond to system events automatically\n\nFor detailed usage instructions, see the USER_GUIDE.md file.',
    status: { type: 'complete' },
  },
  {
    name: 'get_user_documentation_content',
    title: 'Get User Documentation - File Not Found',
    args: {
      filename: 'NONEXISTENT.md',
    },
    result: 'Error: Documentation file NONEXISTENT.md not found.',
    status: { type: 'incomplete', reason: 'error' },
  },
  {
    name: 'get_user_documentation_content',
    title: 'Get User Documentation - Access Denied',
    args: {
      filename: '../../../etc/passwd',
    },
    result: "Error: Access denied. Invalid filename or extension '../../../etc/passwd'.",
    status: { type: 'incomplete', reason: 'error' },
  },

  {
    name: 'render_home_assistant_template',
    title: 'Home Assistant Template - Running',
    args: {
      template: '{{ states("sensor.temperature") }}°C',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'render_home_assistant_template',
    title: 'Home Assistant Template - Success',
    args: {
      template:
        'The living room temperature is {{ states("sensor.living_room_temperature") }}°C and the humidity is {{ states("sensor.living_room_humidity") }}%',
    },
    result: 'The living room temperature is 22.5°C and the humidity is 45%',
    status: { type: 'complete' },
  },
  {
    name: 'render_home_assistant_template',
    title: 'Home Assistant Template - Empty Result',
    args: {
      template: '{{ states("sensor.nonexistent") if false }}',
    },
    result: 'Template rendered to empty result',
    status: { type: 'complete' },
  },
  {
    name: 'render_home_assistant_template',
    title: 'Home Assistant Template - Error',
    args: {
      template: '{{ invalid_function() }}',
    },
    result: 'Error: Home Assistant API error - Template error: unknown function invalid_function',
    status: { type: 'incomplete', reason: 'error' },
  },

  {
    name: 'send_message_to_user',
    title: 'Send Message - Running',
    args: {
      target_chat_id: 123456789,
      message_content: 'Hello! This is a test message from the Family Assistant.',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'send_message_to_user',
    title: 'Send Message - Success',
    args: {
      target_chat_id: 987654321,
      message_content:
        "Your scheduled reminder: Don't forget to pick up groceries on your way home!",
    },
    result: 'Message sent successfully to user with Chat ID 987654321.',
    status: { type: 'complete' },
  },
  {
    name: 'send_message_to_user',
    title: 'Send Message - Long Content',
    args: {
      target_chat_id: 555666777,
      message_content:
        'This is a very long message that demonstrates how the UI handles content truncation. It contains a lot of text that might be too long to display comfortably in the UI without truncation. The message continues with more detailed information about various topics and explanations that would normally be quite lengthy in a real-world scenario.',
    },
    result: 'Message sent successfully to user with Chat ID 555666777.',
    status: { type: 'complete' },
  },
  {
    name: 'send_message_to_user',
    title: 'Send Message - Error',
    args: {
      target_chat_id: 123456789,
      message_content: 'This message failed to send.',
    },
    result:
      'Error: Could not send message to Chat ID 123456789. Details: User not found or blocked the bot.',
    status: { type: 'incomplete', reason: 'error' },
  },

  {
    name: 'execute_script',
    title: 'Execute Script - Running',
    args: {
      script: 'print("Hello from Starlark!")\nresult = 2 + 3\nprint("Result:", result)',
      globals: null,
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'execute_script',
    title: 'Execute Script - Success with Output',
    args: {
      script:
        'def fibonacci(n):\n    if n <= 1:\n        return n\n    return fibonacci(n-1) + fibonacci(n-2)\n\nfor i in range(8):\n    print("fib(" + str(i) + ") =", fibonacci(i))',
      globals: null,
    },
    result:
      'fib(0) = 0\nfib(1) = 1\nfib(2) = 1\nfib(3) = 2\nfib(4) = 3\nfib(5) = 5\nfib(6) = 8\nfib(7) = 13',
    status: { type: 'complete' },
  },
  {
    name: 'execute_script',
    title: 'Execute Script - With Globals',
    args: {
      script:
        'print("Processing data:", data)\nfor item in data["items"]:\n    print("- " + item["name"] + ": " + str(item["value"]))',
      globals: {
        data: {
          items: [
            { name: 'Temperature', value: 22.5 },
            { name: 'Humidity', value: 45 },
            { name: 'Pressure', value: 1013.25 },
          ],
        },
      },
    },
    result:
      'Processing data: {"items": [{"name": "Temperature", "value": 22.5}, {"name": "Humidity", "value": 45}, {"name": "Pressure", "value": 1013.25}]}\n- Temperature: 22.5\n- Humidity: 45\n- Pressure: 1013.25',
    status: { type: 'complete' },
  },
  {
    name: 'execute_script',
    title: 'Execute Script - Long Script Truncated',
    args: {
      script:
        'def complex_calculation(data):\n    """This is a very long script that demonstrates\n    how the UI handles script truncation when the\n    script content is too long to display comfortably.\n    \n    The script continues with many lines of code\n    that would normally be quite lengthy in a\n    real-world automation scenario.\n    \n    It includes complex logic, data processing,\n    error handling, and various computational\n    operations that might be needed for advanced\n    automation tasks."""\n    \n    result = []\n    for i in range(len(data)):\n        processed = data[i] * 2 + 1\n        result.append(processed)\n    \n    return result\n\ndata = [1, 2, 3, 4, 5]\noutput = complex_calculation(data)\nprint("Processed data:", output)',
      globals: null,
    },
    result: 'Processed data: [3, 5, 7, 9, 11]',
    status: { type: 'complete' },
  },
  {
    name: 'execute_script',
    title: 'Execute Script - No Output',
    args: {
      script: 'x = 42\ny = x * 2\n# This script runs but produces no output',
      globals: null,
    },
    result: '',
    status: { type: 'complete' },
  },
  {
    name: 'execute_script',
    title: 'Execute Script - Error',
    args: {
      script: 'undefined_variable = some_missing_var + 5',
      globals: null,
    },
    result: "Error: Starlark execution failed - name 'some_missing_var' is not defined",
    status: { type: 'incomplete', reason: 'error' },
  },

  {
    name: 'schedule_recurring_action',
    title: 'Schedule Recurring Action - Running',
    args: {
      start_time: '2024-12-25T09:00:00+00:00',
      recurrence_rule: 'FREQ=DAILY;INTERVAL=1',
      action_type: 'wake_llm',
      action_config: {
        context: 'Daily morning briefing',
      },
      task_name: 'Daily Briefing',
    },
    result: null,
    status: { type: 'running' },
  },
  {
    name: 'schedule_recurring_action',
    title: 'Schedule Recurring Action - Daily Task',
    args: {
      start_time: '2024-12-25T08:00:00+00:00',
      recurrence_rule: 'FREQ=DAILY;INTERVAL=1',
      action_type: 'wake_llm',
      action_config: {
        context: 'Check weather and provide daily summary',
        additional_instructions: 'Include traffic updates for commute',
      },
      task_name: 'Morning Weather Update',
    },
    result:
      'OK. Recurring wake_llm action (Morning Weather Update) scheduled starting 2024-12-25T08:00:00+00:00',
    status: { type: 'complete' },
  },
  {
    name: 'schedule_recurring_action',
    title: 'Schedule Recurring Action - Weekly Script',
    args: {
      start_time: '2024-12-29T18:00:00+00:00',
      recurrence_rule: 'FREQ=WEEKLY;BYDAY=SU',
      action_type: 'script',
      action_config: {
        script_code:
          'print("Running weekly backup script")\n# Perform backup operations\nprint("Backup completed successfully")',
        environment: 'production',
      },
      task_name: 'Weekly Backup',
    },
    result:
      'OK. Recurring script action (Weekly Backup) scheduled starting 2024-12-29T18:00:00+00:00',
    status: { type: 'complete' },
  },
  {
    name: 'schedule_recurring_action',
    title: 'Schedule Recurring Action - Hourly No Name',
    args: {
      start_time: '2024-12-25T10:00:00+00:00',
      recurrence_rule: 'FREQ=HOURLY;INTERVAL=4',
      action_type: 'wake_llm',
      action_config: {
        context: 'System health check every 4 hours',
      },
    },
    result: 'OK. Recurring wake_llm action scheduled starting 2024-12-25T10:00:00+00:00',
    status: { type: 'complete' },
  },
  {
    name: 'schedule_recurring_action',
    title: 'Schedule Recurring Action - Error',
    args: {
      start_time: '2024-12-20T09:00:00+00:00',
      recurrence_rule: 'FREQ=DAILY;INTERVAL=1',
      action_type: 'wake_llm',
      action_config: {},
      task_name: 'Invalid Task',
    },
    result: "Error: wake_llm action requires 'context' in action_config",
    status: { type: 'incomplete', reason: 'error' },
  },
];

export const ToolTestBench = () => {
  return (
    <div className="test-bench-container">
      <div className="test-bench-header">
        <h1>Tool UI Test Bench</h1>
        <p>Preview and test tool UI components in different states</p>
      </div>

      <div className="tool-grid">
        {sampleToolCalls.map((toolCall, index) => {
          const ToolUI = toolUIsByName[toolCall.name] || ToolFallback;

          return (
            <div key={index} className="tool-test-section">
              <h3>{toolCall.title}</h3>
              <ToolUI
                toolName={toolCall.name}
                args={toolCall.args}
                result={toolCall.result}
                status={toolCall.status}
              />
            </div>
          );
        })}
      </div>

      <div className="test-bench-footer">
        <h2>Available Tools</h2>
        <p>Total tools: {Object.keys(toolUIsByName).length}</p>
        <details>
          <summary>Tool List</summary>
          <ul>
            {Object.keys(toolUIsByName)
              .sort()
              .map((toolName) => (
                <li key={toolName}>
                  <code>{toolName}</code>
                  {toolUIsByName[toolName] === ToolFallback && ' (using fallback)'}
                </li>
              ))}
          </ul>
        </details>
      </div>
    </div>
  );
};
