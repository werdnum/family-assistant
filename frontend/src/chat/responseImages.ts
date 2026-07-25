import type { MessageContent } from './types';

export interface ResponseImage {
  /** Attachment id when known; falls back to the URL so keys stay stable. */
  key: string;
  url: string;
  name: string;
  mimeType: string;
}

function readString(source: Record<string, unknown>, key: string): string | undefined {
  const value = source[key];
  return typeof value === 'string' && value ? value : undefined;
}

/**
 * Normalize one attachment blob from a tool-call part.
 *
 * The same attachment reaches the UI through several producers (per-file
 * `attachment` stream events, enriched `attach_to_response` tool results, and
 * synthesized history parts), and they disagree on which keys are populated:
 * `content_url` vs `url`, `mime_type` vs `content_type`, and sometimes only an
 * `attachment_id`. Resolve all of those to one shape here.
 */
function normalizeAttachment(raw: unknown): ResponseImage | null {
  if (!raw || typeof raw !== 'object') {
    return null;
  }
  const attachment = raw as Record<string, unknown>;

  const mimeType =
    readString(attachment, 'mime_type') ?? readString(attachment, 'content_type') ?? '';
  if (!mimeType.startsWith('image/')) {
    return null;
  }

  const attachmentId = readString(attachment, 'attachment_id') ?? readString(attachment, 'id');
  const url =
    readString(attachment, 'content_url') ??
    readString(attachment, 'url') ??
    (attachmentId ? `/api/attachments/${encodeURIComponent(attachmentId)}` : undefined);
  if (!url) {
    return null;
  }

  const name =
    readString(attachment, 'description') ??
    readString(attachment, 'filename') ??
    readString(attachment, 'name') ??
    'Attachment';

  return { key: attachmentId ?? url, url, name, mimeType };
}

/**
 * Collect the image attachments carried by an assistant message so they can be
 * shown inline in the response instead of only inside the collapsed tool group.
 */
export function collectResponseImages(content: unknown): ResponseImage[] {
  if (!Array.isArray(content)) {
    return [];
  }

  const images: ResponseImage[] = [];
  const seen = new Set<string>();

  for (const part of content as MessageContent[]) {
    if (!part || part.type !== 'tool-call') {
      continue;
    }
    const attachments = part.attachments ?? part.artifact?.attachments;
    if (!Array.isArray(attachments)) {
      continue;
    }
    for (const raw of attachments) {
      const image = normalizeAttachment(raw);
      if (image && !seen.has(image.key)) {
        seen.add(image.key);
        images.push(image);
      }
    }
  }

  return images;
}
