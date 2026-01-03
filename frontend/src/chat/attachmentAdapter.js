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
   * Process a file when it's added to the composer
   * @param {Object} params - The parameters object
   * @param {File} params.file - The file being added
   * @returns {Promise<Object>} Attachment object
   */
  async add({ file }) {
    try {
      // Validate the file
      validateFile(file);

      // Determine attachment type based on file MIME type
      let attachmentType = 'file';
      if (SUPPORTED_IMAGE_TYPES.includes(file.type)) {
        attachmentType = 'image';
      } else if (file.type === 'application/pdf' || file.type.startsWith('text/')) {
        attachmentType = 'document';
      }

      // Create initial attachment object
      const attachment = {
        id: generateUUID(),
        type: attachmentType,
        name: file.name,
        file,
        status: { type: 'running' },
      };

      return attachment;
    } catch (error) {
      // Return attachment with error status
      return {
        id: generateUUID(),
        type: 'file',
        name: file.name,
        file,
        status: {
          type: 'error',
          error: error.message,
        },
      };
    }
  }

  /**
   * Process attachment for sending (upload to service)
   * @param {Object} attachment - The attachment to process
   * @returns {Promise<Object>} Processed attachment with content URL
   */
  async send(attachment) {
    try {
      // Upload file to attachment service
      const uploadResponse = await uploadFileToService(attachment.file);

      // Return completed attachment with served URL
      return {
        id: attachment.id,
        type: attachment.type, // Use the type determined during add()
        name: attachment.name,
        content: uploadResponse.url, // URL to serve the file from backend
        uploadedId: uploadResponse.attachment_id, // Store server-side ID for potential cleanup
        status: { type: 'complete' },
      };
    } catch (error) {
      console.error('Error processing attachment for sending:', error);

      // Return attachment with error status
      return {
        id: attachment.id,
        type: attachment.type || 'file',
        name: attachment.name,
        status: {
          type: 'error',
          error: `Failed to upload file: ${error.message}`,
        },
      };
    }
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
  async add({ file }) {
    const adapter = this.getAdapterForType(file.type);

    if (!adapter) {
      return {
        id: generateUUID(),
        type: 'file',
        name: file.name,
        file,
        status: {
          type: 'error',
          error: `Unsupported file type: ${file.type}`,
        },
      };
    }

    return adapter.add({ file });
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
