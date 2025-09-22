/**
 * TypeScript interfaces for attachment objects used throughout the application.
 * These interfaces define the structure of attachments received from the API.
 */

export interface BaseAttachment {
  attachment_id: string;
  mime_type: string;
  description?: string;
  content_url?: string;
}

export interface ToolResultAttachment extends BaseAttachment {
  type: 'tool_result';
}

export interface UserAttachment extends BaseAttachment {
  type: 'user';
  filename: string;
  size: number;
}

export interface ImageAttachment extends BaseAttachment {
  type: 'image';
  content_url: string; // Required for image attachments
}

export type Attachment = ToolResultAttachment | UserAttachment | ImageAttachment;

/**
 * Type guard to check if an object is a valid attachment
 */
export function isAttachment(obj: unknown): obj is Attachment {
  if (!obj || typeof obj !== 'object') {
    return false;
  }

  const attachment = obj as Record<string, unknown>;

  // Check common required fields
  if (
    typeof attachment.attachment_id !== 'string' ||
    typeof attachment.mime_type !== 'string' ||
    (attachment.description !== undefined && typeof attachment.description !== 'string') ||
    (attachment.content_url !== undefined && typeof attachment.content_url !== 'string')
  ) {
    return false;
  }

  // Check type-specific fields
  if (attachment.type === 'tool_result') {
    return true; // ToolResultAttachment has no additional required fields
  } else if (attachment.type === 'user') {
    // UserAttachment requires filename and size
    return typeof attachment.filename === 'string' && typeof attachment.size === 'number';
  } else if (attachment.type === 'image') {
    // ImageAttachment requires content_url
    return typeof attachment.content_url === 'string';
  }

  return false;
}

/**
 * Type guard specifically for tool result attachments
 */
export function isToolResultAttachment(obj: unknown): obj is ToolResultAttachment {
  return isAttachment(obj) && obj.type === 'tool_result';
}

/**
 * Generate a stable key for an attachment object
 * Uses attachment_id if available, otherwise creates a deterministic key from other properties
 */
export function getAttachmentKey(attachment: unknown, fallbackIndex?: number): string {
  if (isAttachment(attachment)) {
    return attachment.attachment_id;
  }

  // For objects that don't match the expected structure, warn and create a fallback key
  if (attachment && typeof attachment === 'object') {
    const obj = attachment as Record<string, unknown>;

    // Log warning when attachment_id is missing
    if (!obj.attachment_id) {
      console.warn('Attachment missing attachment_id:', attachment);
    }

    // Generate stable key from available properties
    const keyParts = [
      obj.content_url || '',
      obj.mime_type || '',
      obj.description || '',
      obj.type || '',
      obj.filename || '', // For UserAttachment
      obj.size ? String(obj.size) : '', // For UserAttachment
    ].filter(Boolean);

    if (keyParts.length > 0) {
      // Create a simple hash from the combined properties
      const combined = keyParts.join('|');
      let hash = 0;
      for (let i = 0; i < combined.length; i++) {
        const char = combined.charCodeAt(i);
        hash = (hash << 5) - hash + char;
        hash = hash & hash; // Convert to 32-bit integer
      }
      return `attachment-${Math.abs(hash)}`;
    }
  }

  // Ultimate fallback - warn about using index
  if (fallbackIndex !== undefined) {
    console.warn('Using index as attachment key - this may cause React rendering issues:', {
      attachment,
      fallbackIndex,
    });
    return `attachment-index-${fallbackIndex}`;
  }

  return 'attachment-unknown-no-identifying-properties';
}
