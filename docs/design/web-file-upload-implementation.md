# Web File Upload Implementation Plan

## Overview

This document outlines the implementation plan for adding file and photo upload capabilities to the
web chat interface to provide feature parity with the Telegram interface, which already supports
photo uploads. The system will use assistant-ui's attachment framework to handle file uploads and
integrate with the existing backend processing that already supports multi-part content.

## Background

Currently, the Family Assistant application supports photo uploads through the Telegram interface
but lacks this capability in the web chat interface. The Telegram implementation already handles:

- Photo download from Telegram's file API
- Conversion to base64 format
- Multi-part content creation with `image_url` type
- Integration with vision-capable LLMs

The web interface needs similar functionality to provide consistent user experience across all
interfaces.

## Implementation Steps

### 1. Backend API Updates

#### Update ChatPromptRequest Model

**File**: `src/family_assistant/web/models.py`

- Add optional `attachments` field to accept base64-encoded file data
- Support multiple content parts (text + images) similar to Telegram implementation

```python
class ChatPromptRequest(BaseModel):
    prompt: str
    conversation_id: str | None = None
    profile_id: str | None = None
    interface_type: str | None = None
    attachments: list[dict[str, Any]] | None = None  # New field
```

#### Update Streaming Endpoint

**File**: `src/family_assistant/web/routers/chat_api.py`

- Modify `api_chat_send_message_stream` to handle attachments in request
- Build `trigger_content_parts` with both text and image_url types
- Follow same pattern as Telegram:
  `{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_data}"}}`

### 2. Frontend - Install Assistant-UI Attachment Components

```bash
npx shadcn@latest add "https://r.assistant-ui.com/attachment"
```

### 3. Frontend - Implement Attachment Adapter

**File**: `frontend/src/chat/attachmentAdapter.js`

- Implement `SimpleImageAttachmentAdapter` for image files
- Handle file validation (size limits, file types)
- Convert files to base64 format for transmission
- Support common image formats (JPEG, PNG, GIF, WebP)

```javascript
class VisionImageAdapter {
  accept = "image/*";
  
  async add({ file }) {
    // Validate file size, type
    // Return attachment object
  }
  
  async send(attachment) {
    // Convert to base64 data URL
    // Return formatted content
  }
}
```

### 4. Frontend - Update Chat Components

#### Update ChatApp.tsx

- Configure runtime with attachment adapter using `CompositeAttachmentAdapter`
- Pass adapter to `useExternalStoreRuntime`

#### Update Thread Component

**File**: `frontend/src/chat/Thread.tsx`

- Add `ComposerAttachments` component to display attached files
- Add `ComposerAddAttachment` button for file selection
- Update composer to show attachment previews

#### Update useStreamingResponse Hook

**File**: `frontend/src/chat/useStreamingResponse.js`

- Modify `sendStreamingMessage` to accept attachments parameter
- Include attachments in API request body

#### Update handleNew Callback

**File**: ChatApp.tsx

- Process attachments from message content
- Build multi-part content array with text and image parts

### 5. File Validation & Security

- **File size limits**: 10MB per image initially
- **Supported file types**: Images only (JPEG, PNG, GIF, WebP)
- **Image dimension validation**: Reasonable limits for LLM processing
- **Error handling**: Clear user feedback for validation failures

### 6. UI/UX Enhancements

- **Upload progress indicators**: Show upload status
- **Thumbnail previews**: Display attached images before sending
- **Drag-and-drop support**: Intuitive file selection
- **Error messages**: Clear feedback for unsupported files
- **Accessibility**: Keyboard navigation and screen reader support

### 7. Testing

- **Unit tests**: Attachment adapter validation and processing
- **Integration tests**: End-to-end file upload flow
- **Playwright tests**: UI interactions and user workflows
- **Error scenario tests**: Oversized files, unsupported formats
- **Cross-browser compatibility**: Ensure consistent behavior

## Technical Details

### Data Flow

1. User selects file via UI button or drag-and-drop
2. Attachment adapter validates and processes file
3. File converted to base64 data URL
4. Attachment included in message content
5. Frontend sends multi-part content to backend
6. Backend processes content parts (text + images)
7. LLM receives formatted content with vision support
8. Response streamed back to user

### Format Compatibility

The implementation will use the same content format as the Telegram interface:

```javascript
// Text content
{
  "type": "text",
  "text": "User's text message"
}

// Image content
{
  "type": "image_url",
  "image_url": {
    "url": "data:image/jpeg;base64,{base64_data}"
  }
}
```

This ensures the backend processing layer handles both interfaces identically.

### Integration Points

- **LLM Client**: Already supports multi-part content with image_url types
- **Processing Service**: No changes needed - already handles trigger_content_parts
- **Database**: Message storage already supports multi-part content
- **Vision Models**: GPT-4V, Claude 3, Gemini Pro Vision already supported

## Benefits

- **Feature parity**: Web users get same capabilities as Telegram users
- **Enhanced UX**: Visual communication through images and documents
- **LLM vision support**: Leverages vision-capable models
- **Consistent architecture**: Reuses existing backend multi-part content support
- **Assistant-UI integration**: Uses established patterns and components

## Future Enhancements

The following features are not included in the initial implementation but could be added later:

- **Additional file types**: PDFs, documents, audio files
- **Multiple attachments**: Multiple files per message
- **File compression**: Optimize large images automatically
- **Cloud storage**: Store large files externally with links
- **Voice messages**: Recording and transcription support
- **Advanced validation**: Virus scanning, content analysis

## Security Considerations

- **File size limits**: Prevent resource exhaustion
- **Content validation**: Ensure files are valid images
- **Sanitization**: Strip metadata from uploaded images
- **Rate limiting**: Prevent abuse of upload functionality
- **Authentication**: Ensure only authorized users can upload

## Performance Considerations

- **Base64 encoding**: Increases payload size by ~33%
- **Memory usage**: Large images consume client and server memory
- **Network bandwidth**: Consider compression for large uploads
- **Caching**: Implement client-side caching for repeated uploads

## Acceptance Criteria

1. Users can attach image files to chat messages
2. Attached images are displayed as previews before sending
3. Images are processed by vision-capable LLMs
4. File validation prevents unsupported formats/sizes
5. Error handling provides clear user feedback
6. Feature works consistently across modern browsers
7. Comprehensive test coverage includes functional tests
8. Performance remains acceptable with typical image sizes

## Implementation Notes

- Start with image files only to limit scope
- Reuse existing Telegram processing patterns
- Maintain backward compatibility with existing chat functionality
- Follow assistant-ui best practices and patterns
- Ensure accessibility compliance
- Include comprehensive functional tests as requested
