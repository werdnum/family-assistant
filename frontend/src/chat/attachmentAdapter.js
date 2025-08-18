/**
 * Attachment adapter for handling image files in the chat interface
 * Validates and processes image files for sending to the backend
 */

import { generateUUID } from '../utils/uuid.js';

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB limit
const SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];

/**
 * Converts a File object to a base64 data URL
 * @param {File} file - The file to convert
 * @returns {Promise<string>} Base64 data URL
 */
const fileToBase64DataURL = (file) => {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('Failed to read file'));
    reader.readAsDataURL(file);
  });
};

/**
 * Validates an image file against size and type constraints
 * @param {File} file - The file to validate
 * @throws {Error} If validation fails
 */
const validateImageFile = (file) => {
  // Check file size
  if (file.size > MAX_FILE_SIZE) {
    throw new Error(`File size exceeds ${MAX_FILE_SIZE / (1024 * 1024)}MB limit`);
  }

  // Check file type
  if (!SUPPORTED_IMAGE_TYPES.includes(file.type)) {
    throw new Error(`Unsupported file type. Supported types: ${SUPPORTED_IMAGE_TYPES.join(', ')}`);
  }

  // Basic file name validation
  if (!file.name || file.name.trim() === '') {
    throw new Error('File must have a valid name');
  }
};

/**
 * Simple Image Attachment Adapter for assistant-ui
 * Handles image file validation, processing, and conversion to base64
 */
export class SimpleImageAttachmentAdapter {
  constructor() {
    this.accept = 'image/*';
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
      validateImageFile(file);

      // Create initial attachment object
      const attachment = {
        id: generateUUID(),
        type: 'image',
        name: file.name,
        file,
        status: { type: 'running' },
      };

      return attachment;
    } catch (error) {
      // Return attachment with error status
      return {
        id: generateUUID(),
        type: 'image',
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
   * Process attachment for sending (convert to base64)
   * @param {Object} attachment - The attachment to process
   * @returns {Promise<Object>} Processed attachment with content
   */
  async send(attachment) {
    try {
      // Convert file to base64 data URL
      const base64DataURL = await fileToBase64DataURL(attachment.file);

      // Return completed attachment with content
      return {
        id: attachment.id,
        type: 'image',
        name: attachment.name,
        content: base64DataURL, // This will be sent to the backend
        status: { type: 'complete' },
      };
    } catch (error) {
      console.error('Error processing attachment for sending:', error);

      // Return attachment with error status
      return {
        id: attachment.id,
        type: 'image',
        name: attachment.name,
        status: {
          type: 'error',
          error: `Failed to process image: ${error.message}`,
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
    // Since the file is only stored in memory on the client-side,
    // there's no need to make a server call.
    // The runtime will remove the attachment from its internal state.
    console.log(`Removing attachment: ${attachment.name}`);
    return Promise.resolve();
  }
}

/**
 * Composite attachment adapter that combines multiple adapters
 */
export class CompositeAttachmentAdapter {
  constructor(adapters = []) {
    this.adapters = adapters;
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
    // For now, assume it's an image since we only have the image adapter
    // In the future, we could store adapter info in the attachment
    const adapter = this.getAdapterForType(attachment.file?.type || 'image/jpeg');

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
    const adapter = this.getAdapterForType(attachment.file?.type || 'image/jpeg');

    if (!adapter) {
      console.error('No adapter available for this file type');
      return;
    }

    return adapter.remove(attachment);
  }
}

// Export a pre-configured composite adapter with image support
export const defaultAttachmentAdapter = new CompositeAttachmentAdapter([
  new SimpleImageAttachmentAdapter(),
]);
