export interface Attachment {
  id?: string;
  attachment_id?: string;
  type?: 'tool_result' | 'image' | 'file' | 'document' | string;
  mime_type?: string;
  content_url?: string;
  url?: string;
  description?: string;
  name?: string;
  filename?: string;
  size?: number;
  [key: string]: any;
}

export const isAttachment = (obj: any): obj is Attachment => {
  return (
    typeof obj === 'object' &&
    obj !== null &&
    ('id' in obj || 'attachment_id' in obj || 'content_url' in obj)
  );
};

export const getAttachmentKey = (attachment: Attachment, index: number): string => {
  if (attachment.id) {
    return attachment.id;
  }
  if (attachment.attachment_id) {
    return attachment.attachment_id;
  }
  if (attachment.content_url) {
    return attachment.content_url;
  }
  // Fallback for attachments that might not have a unique server ID yet
  return `attachment-index-${index}`;
};