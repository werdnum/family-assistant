# Notes Application - Frontend

This directory contains the React-based notes management interface for Family Assistant.

## Components

### NotesApp.jsx

Main routing component for the notes application. Handles routes for:

- `/notes` - Notes list view
- `/notes/add` - Create new note
- `/notes/edit/:title` - Edit existing note

### NotesListWithDataTable.tsx

Displays all notes in a sortable, searchable data table with:

- Title, content preview, and status columns
- Attachment count indicator
- Edit and delete actions
- Pagination and search functionality

### NotesForm.jsx

Form component for creating and editing notes. Features:

- Title and content fields
- "Include in system prompt" checkbox
- Attachment management (upload, preview, remove)
- Automatic saving with validation

### AttachmentPreview.tsx

Displays attachment previews with:

- Thumbnail for images
- File icon and metadata for non-images
- Click-to-enlarge for images
- Remove button (when enabled)
- Automatic metadata fetching

### AttachmentUpload.tsx

Handles file uploads for attachments:

- Multi-file selection
- File type and size validation
- Upload progress indication
- Error handling
- Supported types: images (JPEG, PNG, GIF, WebP), text, markdown, PDF
- Maximum file size: 100MB per file

## Features

### Attachment Support

Notes can have multiple attachments associated with them. The system:

1. Uploads files to the attachment service (`/api/attachments/upload`)
2. Stores attachment IDs with the note
3. Displays attachments in the note editor and list view
4. Allows removing attachments before saving

### API Integration

The notes frontend integrates with the following backend endpoints:

- `GET /api/notes/` - List all notes
- `GET /api/notes/:title` - Get a specific note
- `POST /api/notes/` - Create or update a note
- `DELETE /api/notes/:title` - Delete a note
- `POST /api/attachments/upload` - Upload an attachment
- `GET /api/attachments/:id` - Get attachment metadata or file
- `DELETE /api/attachments/:id` - Delete an attachment

## Data Model

```typescript
interface Note {
  title: string;
  content: string;
  include_in_prompt: boolean;
  attachment_ids: string[];  // UUIDs of associated attachments
}
```

## Testing

All components have comprehensive test coverage:

- `__tests__/AttachmentPreview.test.tsx` - Preview component tests
- `__tests__/AttachmentUpload.test.tsx` - Upload functionality tests

Tests use MSW (Mock Service Worker) for API mocking. See `frontend/src/test/mocks/handlers.ts` for
mock implementations.

## Usage

### Creating a Note with Attachments

1. Navigate to `/notes/add`
2. Fill in title and content
3. Click "Add Attachments" to upload files
4. Preview shows thumbnails for images, file icons for others
5. Remove unwanted attachments with the X button
6. Click "Save" to create the note

### Editing a Note

1. Click "Edit" on any note in the list
2. Existing attachments are loaded automatically
3. Add new attachments or remove existing ones
4. Changes are saved when you click "Save"

### Supported File Types

- **Images**: JPEG, PNG, GIF, WebP
- **Documents**: PDF, plain text, markdown

## Architecture

The notes application follows the standard React + TypeScript architecture:

- Components are organized by feature
- State management uses React hooks
- API calls use native `fetch`
- Styling uses Tailwind CSS and shadcn/ui components
- Tests use Vitest + React Testing Library + MSW
