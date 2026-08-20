/**
 * Attachment adapter for handling files in the chat interface
 * Validates and processes attachments for sending to the backend
 */

import { generateUUID } from '../utils/uuid.js';

// MAX_FILE_SIZE can be configured via the VITE_MAX_FILE_SIZE environment variable (in bytes). Defaults to 100MB to match backend.
const MAX_FILE_SIZE =
  typeof import.meta.env !== 'undefined' && import.meta.env.VITE_MAX_FILE_SIZE
    ? Number(import.meta.env.VITE_MAX_FILE_SIZE)
    : 100 * 1024 * 1024; // 100MB default - matches backend AttachmentService

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

  // Basic file name validation
  if (!file.name || file.name.trim() === '') {
    throw new Error('File must have a valid name');
  }
};

/**
 * Determines the attachment type the backend will process the file as.
 *
 * Anything that is not recognisable media is a document: that is the label the
 * backend keeps in message history, so a file of a type this client has no
 * case for still reaches the model and survives into later turns.
 *
 * @param {File} file - The file being attached
 * @returns {string} Attachment type
 */
const classifyAttachment = (file) => {
  if (file.type.startsWith('image/')) {
    return 'image';
  }
  if (file.type.startsWith('audio/')) {
    return 'audio';
  }
  if (file.type.startsWith('video/')) {
    return 'video';
  }
  return 'document';
};

/**
 * Builds a complete attachment pointing at the uploaded file
 * @param {Object} params - id, type, file and the upload service response
 * @returns {Object} Complete attachment
 */
const completeAttachment = ({ id, type, file, uploadResponse }) => {
  const url = uploadResponse.url;

  // Content is an array of ThreadUserMessagePart objects, as
  // @assistant-ui/react requires, rather than a plain string URL. Non-images
  // use a data part carrying the URL so the runtime passes it through to our
  // handleNew callback.
  const content =
    type === 'image' ? [{ type: 'image', image: url }] : [{ type: 'data', name: 'url', data: url }];

  return {
    id,
    type,
    name: file.name,
    file, // Kept so remove() can route back to this adapter and delete the file
    content,
    uploadedId: uploadResponse.attachment_id, // Server-side ID, for cleanup
    status: { type: 'complete' },
  };
};

/**
 * Builds an attachment carrying an error the composer renders on the file
 * @param {Object} params - id, type, file and the message to show
 * @returns {Object} Attachment in the runtime's incomplete/error state
 */
const errorAttachment = ({ id, type, file, message }) => ({
  id,
  type,
  name: file.name,
  file,
  content: [],
  status: { type: 'incomplete', reason: 'error', message },
});

/**
 * File Attachment Adapter for assistant-ui
 * Handles file validation, processing, and upload to backend service
 */
export class FileAttachmentAdapter {
  constructor() {
    // Any file type: what a model cannot read directly, the assistant can still
    // open with its attachment tools or hand to a profile that can.
    this.accept = '*/*';
  }

  /**
   * Accept a file into the composer, pending the send that uploads it.
   *
   * The attachment must land in a state the composer can leave: it refuses to
   * send while an attachment is 'running', so reporting an upload here would
   * lock the composer unless that upload also finished here -- and an upload
   * running alongside the composer can be sent or dropped midway, which the
   * runtime handles by uploading the file a second time or by re-adding an
   * attachment it has already sent. Uploading in send() has no such window.
   *
   * @param {Object} params - The parameters object
   * @param {File} params.file - The file being added
   * @returns {Promise<Object>} Attachment object awaiting send
   */
  async add({ file }) {
    try {
      validateFile(file);
    } catch (error) {
      return errorAttachment({ id: generateUUID(), type: 'file', file, message: error.message });
    }

    return {
      id: generateUUID(),
      type: classifyAttachment(file),
      name: file.name,
      file,
      status: { type: 'requires-action', reason: 'composer-send' },
    };
  }

  /**
   * Upload an attachment as the message carrying it is sent
   * @param {Object} attachment - The attachment to process
   * @returns {Promise<Object>} Processed attachment with content parts array
   */
  async send(attachment) {
    try {
      // Re-checked because send() is also reached by an attachment add()
      // rejected: the composer renders the error but does not block sending.
      validateFile(attachment.file);

      const uploadResponse = await uploadFileToService(attachment.file);

      return completeAttachment({
        id: attachment.id,
        type: attachment.type,
        file: attachment.file,
        uploadResponse,
      });
    } catch (error) {
      console.error('Error processing attachment for sending:', error);

      return errorAttachment({
        id: attachment.id,
        type: attachment.type || 'file',
        file: attachment.file,
        message: `Failed to upload file: ${error.message}`,
      });
    }
  }

  /**
   * Remove an attachment.
   *
   * Nothing is on the server to clean up: the runtime only routes an
   * attachment here while it is still pending, and a pending attachment has
   * not been uploaded yet.
   *
   * @param {Object} _attachment - The attachment to remove
   * @returns {Promise<void>}
   */
  async remove(_attachment) {
    // The runtime removes the attachment from its own state.
  }
}

/**
 * Tests one entry of an `accept` list against a MIME type.
 * A bare or full wildcard matches everything; `image/*` matches by prefix.
 * @param {string} acceptType - A single accept entry
 * @param {string} type - The MIME type to test
 * @returns {boolean} Whether the entry accepts the type
 */
const matchesAcceptType = (acceptType, type) => {
  if (acceptType === '*' || acceptType === '*/*') {
    return true;
  }
  if (acceptType.endsWith('/*')) {
    return type.startsWith(acceptType.slice(0, -1));
  }
  return acceptType === type;
};

/**
 * Composite attachment adapter that combines multiple adapters
 */
export class CompositeAttachmentAdapter {
  constructor(adapters = []) {
    this.adapters = adapters;
    // Let our adapter validate files and render inline errors.
    this.accept = '*';
  }

  /**
   * Get the first adapter that accepts the given file type
   * @param {string} type - MIME type to check
   * @returns {Object|null} Matching adapter or null
   */
  getAdapterForType(type) {
    return this.adapters.find((adapter) =>
      adapter.accept
        .split(',')
        .map((acceptType) => acceptType.trim())
        .some((acceptType) => matchesAcceptType(acceptType, type))
    );
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
      return errorAttachment({
        id: generateUUID(),
        type: 'file',
        file,
        message: `Unsupported file type: ${file.type}`,
      });
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
        content: [],
        status: {
          type: 'incomplete',
          reason: 'error',
          message: 'No adapter available for this file type',
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
