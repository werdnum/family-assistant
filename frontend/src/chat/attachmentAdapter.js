/**
 * Attachment adapter for handling image files in the chat interface
 * Validates and processes image files for sending to the backend
 */

import { generateUUID } from '../utils/uuid.js';

// MAX_FILE_SIZE can be configured via the VITE_MAX_FILE_SIZE environment variable (in bytes). Defaults to 100MB to match backend.
const MAX_FILE_SIZE =
  typeof import.meta.env !== 'undefined' && import.meta.env.VITE_MAX_FILE_SIZE
    ? Number(import.meta.env.VITE_MAX_FILE_SIZE)
    : 100 * 1024 * 1024; // 100MB default - matches backend AttachmentService
const SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];

// Additional supported file types from backend AttachmentService
const SUPPORTED_FILE_TYPES = [
  ...SUPPORTED_IMAGE_TYPES,
  'text/plain',
  'text/markdown',
  'application/pdf',
];

/**
 * Uploads a file to the attachment service
 * @param {File} file - The file to upload
 * @returns {Promise<Object>} Upload response with attachment metadata
 */
const uploadFileToService = async (file) => {
  const formData = new globalThis.FormData();
  formData.append('file', file);

  const response = await fetch('/api/attachments/upload', {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: 'Upload failed' }));
    throw new Error(errorData.detail || `Upload failed with status ${response.status}`);
  }

  return await response.json();
};

/**
 * Validates a file against size and type constraints
 * @param {File} file - The file to validate
 * @throws {Error} If validation fails
 */
const validateFile = (file) => {
  // Check file size
  if (file.size > MAX_FILE_SIZE) {
    throw new Error(`File size exceeds ${MAX_FILE_SIZE / (1024 * 1024)}MB limit`);
  }

  // Check file type
  if (!SUPPORTED_FILE_TYPES.includes(file.type)) {
    throw new Error(`Unsupported file type. Supported types: ${SUPPORTED_FILE_TYPES.join(', ')}`);
  }

  // Basic file name validation
  if (!file.name || file.name.trim() === '') {
    throw new Error('File must have a valid name');
  }
};

/**
 * File Attachment Adapter for assistant-ui
 * Handles file validation, processing, and upload to backend service
 */
export class FileAttachmentAdapter {
  constructor() {
    // Accept all supported file types
    this.accept = SUPPORTED_FILE_TYPES.join(',');
  }

  /**
   * Process a file when it's added to the composer. Uploads eagerly: yields
   * a ``running`` attachment first so the UI can show progress, then yields
   * the ``complete`` attachment with the server URL once the upload lands.
   * Splitting the upload into send-time would leave the UI claiming
   * "Uploading..." with no network activity until the user hits send.
   * @param {Object} params - The parameters object
   * @param {File} params.file - The file being added
   */
  async *add({ file }) {
    const id = generateUUID();
    try {
      validateFile(file);
    } catch (error) {
      yield {
        id,
        type: 'file',
        name: file.name,
        file,
        status: { type: 'error', error: error.message },
      };
      return;
    }

    let attachmentType = 'file';
    if (SUPPORTED_IMAGE_TYPES.includes(file.type)) {
      attachmentType = 'image';
    } else if (file.type === 'application/pdf' || file.type.startsWith('text/')) {
      attachmentType = 'document';
    }

    yield {
      id,
      type: attachmentType,
      name: file.name,
      file,
      status: { type: 'running' },
    };

    try {
      const uploadResponse = await uploadFileToService(file);
      const url = uploadResponse.url;
      const contentParts =
        attachmentType === 'image'
          ? [{ type: 'image', image: url }]
          : [{ type: 'data', name: 'url', data: url }];

      yield {
        id,
        type: attachmentType,
        name: file.name,
        file,
        content: contentParts,
        uploadedId: uploadResponse.attachment_id,
        status: { type: 'complete' },
      };
    } catch (error) {
      console.error('Error uploading attachment:', error);
      yield {
        id,
        type: attachmentType,
        name: file.name,
        file,
        content: [],
        status: {
          type: 'error',
          error: `Failed to upload file: ${error.message}`,
        },
      };
    }
  }

  /**
   * Pass-through for send. The upload already happened in ``add`` so the
   * attachment reaches here already marked complete with its content parts.
   * The base composer runtime short-circuits ``adapter.send`` for
   * already-complete attachments, so this should rarely be invoked — it's
   * kept defensively for any non-complete attachment the runtime still routes
   * through send.
   * @param {Object} attachment
   * @returns {Promise<Object>}
   */
  async send(attachment) {
    return attachment;
  }

  /**
   * Remove an attachment
   * @param {Object} attachment - The attachment to remove
   * @returns {Promise<void>}
   */
  async remove(attachment) {
    try {
      // If the attachment was successfully uploaded, clean it up from the server
      if (attachment.uploadedId && attachment.status?.type === 'complete') {
        const response = await fetch(`/api/attachments/${attachment.uploadedId}`, {
          method: 'DELETE',
        });

        if (!response.ok) {
          console.error(`Failed to delete attachment ${attachment.uploadedId} from server`);
        }
      }
    } catch (error) {
      console.error(`Error removing attachment from server: ${error.message}`);
    }

    // The runtime will remove the attachment from its internal state
  }
}

/**
 * Composite attachment adapter that combines multiple adapters
 */
export class CompositeAttachmentAdapter {
  constructor(adapters = []) {
    this.adapters = adapters;
    // Aggregate accept types from all adapters
    this.accept = adapters
      .map((adapter) => adapter.accept)
      .filter(Boolean)
      .join(',');
  }

  /**
   * Get the first adapter that accepts the given file type
   * @param {string} type - MIME type to check
   * @returns {Object|null} Matching adapter or null
   */
  getAdapterForType(type) {
    return this.adapters.find((adapter) => {
      // Check if adapter's accept pattern matches the type
      const acceptPattern = adapter.accept;

      // Handle comma-separated types
      if (acceptPattern.includes(',')) {
        const acceptedTypes = acceptPattern.split(',').map((t) => t.trim());
        return acceptedTypes.some((acceptType) => {
          if (acceptType === type) {
            return true;
          }
          if (acceptType.endsWith('/*')) {
            const prefix = acceptType.slice(0, -2);
            return type.startsWith(prefix);
          }
          return false;
        });
      }

      // Handle single type or wildcard
      if (acceptPattern === type) {
        return true;
      }
      if (acceptPattern.endsWith('/*')) {
        const prefix = acceptPattern.slice(0, -2);
        return type.startsWith(prefix);
      }
      return false;
    });
  }

  /**
   * Add a file using the appropriate adapter
   * @param {Object} params - The parameters object
   * @param {File} params.file - The file being added
   * @returns {Promise<Object>} Attachment object
   */
  async *add({ file }) {
    const adapter = this.getAdapterForType(file.type);

    if (!adapter) {
      yield {
        id: generateUUID(),
        type: 'file',
        name: file.name,
        file,
        status: {
          type: 'error',
          error: `Unsupported file type: ${file.type}`,
        },
      };
      return;
    }

    // Delegate to the inner adapter. It may be a generator (eager upload) or
    // a plain async function — handle both to stay forward-compatible.
    const result = adapter.add({ file });
    if (result && typeof result === 'object' && Symbol.asyncIterator in result) {
      yield* result;
    } else {
      yield await result;
    }
  }

  /**
   * Send an attachment using its original adapter
   * @param {Object} attachment - The attachment to send
   * @returns {Promise<Object>} Processed attachment
   */
  async send(attachment) {
    // Find adapter that can handle this file type
    const adapter = this.getAdapterForType(attachment.file?.type || 'application/octet-stream');

    if (!adapter) {
      return {
        ...attachment,
        status: {
          type: 'error',
          error: 'No adapter available for this file type',
        },
      };
    }

    return adapter.send(attachment);
  }

  /**
   * Remove an attachment using its original adapter
   * @param {Object} attachment - The attachment to remove
   * @returns {Promise<void>}
   */
  async remove(attachment) {
    const adapter = this.getAdapterForType(attachment.file?.type || 'application/octet-stream');

    if (!adapter) {
      console.error('No adapter available for this file type');
      return;
    }

    return adapter.remove(attachment);
  }
}

// Export a pre-configured composite adapter with file support
export const defaultAttachmentAdapter = new CompositeAttachmentAdapter([
  new FileAttachmentAdapter(),
]);
