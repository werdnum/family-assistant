/**
 * @vitest-environment jsdom
 */

import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { server } from '../../test/setup.js';
import {
  CompositeAttachmentAdapter,
  defaultAttachmentAdapter,
  FileAttachmentAdapter,
} from '../attachmentAdapter';

// Mock crypto.randomUUID
Object.defineProperty(global, 'crypto', {
  value: {
    randomUUID: () => 'test-uuid-123',
  },
});

// Mock FileReader (still used as fallback)
global.FileReader = class {
  constructor() {
    this.result = null;
    this.onload = null;
    this.onerror = null;
  }

  readAsDataURL(file) {
    // Simulate async behavior
    setTimeout(() => {
      if (this.onload) {
        this.result = `data:${file.type};base64,dGVzdA==`; // "test" in base64
        this.onload();
      }
    }, 0);
  }
};

// Mock fetch will be handled by MSW server from setup.js

// Create mock file helper
function createMockFile(name, type, size, content = 'test content') {
  const file = new File([content], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

describe('FileAttachmentAdapter', () => {
  let adapter;

  beforeEach(() => {
    adapter = new FileAttachmentAdapter();
  });

  describe('constructor', () => {
    test('sets correct accept pattern', () => {
      expect(adapter.accept).toBe(
        'image/jpeg,image/png,image/gif,image/webp,text/plain,text/markdown,application/pdf,audio/mpeg,audio/wav,audio/ogg,audio/webm,video/mp4,video/webm,video/ogg'
      );
    });

    // A type absent here is rejected before upload, so the backend transcription
    // handoff never sees it. These must stay in step with
    // attachment_config.allowed_mime_types in defaults.yaml.
    test('accepts the audio and video the assistant can transcribe', () => {
      for (const type of [
        'audio/mpeg',
        'audio/wav',
        'audio/ogg',
        'audio/webm',
        'video/mp4',
        'video/webm',
        'video/ogg',
      ]) {
        expect(adapter.accept).toContain(type);
      }
    });
  });

  describe('add method', () => {
    // The composer refuses to send while an attachment is 'running', so an
    // attachment reporting an upload it does not finish here locks the
    // composer: the file reads "Uploading..." forever and the message can
    // never be sent. Pending until send is the state the composer can leave.
    test('adds a valid image file, pending the send that uploads it', async () => {
      const file = createMockFile('test.png', 'image/png', 1024 * 1024); // 1MB

      const result = await adapter.add({ file });

      expect(result.id).toBe('test-uuid-123');
      expect(result.type).toBe('image');
      expect(result.name).toBe('test.png');
      expect(result.file).toBe(file);
      expect(result.status).toEqual({ type: 'requires-action', reason: 'composer-send' });
    });

    test('does not upload the file when it is added', async () => {
      const uploads = [];
      server.use(
        http.post('/api/attachments/upload', () => {
          uploads.push('upload');
          return HttpResponse.json({ attachment_id: 'x', url: '/api/attachments/x' });
        })
      );
      const file = createMockFile('test.png', 'image/png', 1024);

      await adapter.add({ file });

      expect(uploads).toEqual([]);
    });

    // The backend processes image, video, audio and document and ignores
    // anything else, so a media file left as 'file' is uploaded and then dropped
    // from the turn: the model answers without it and nothing reports an error.
    test('classifies audio and video as their backend types', async () => {
      for (const [mimeType, expected] of [
        ['audio/mpeg', 'audio'],
        ['audio/ogg', 'audio'],
        ['audio/wav', 'audio'],
        ['audio/webm', 'audio'],
        ['video/mp4', 'video'],
        ['video/webm', 'video'],
        ['video/ogg', 'video'],
      ]) {
        const file = createMockFile(`clip.${expected}`, mimeType, 1024);

        const result = await adapter.add({ file });

        expect(result.type).toBe(expected);
      }
    });

    test('successfully adds valid text file', async () => {
      const file = createMockFile('document.txt', 'text/plain', 1024);

      const result = await adapter.add({ file });

      expect(result.id).toBe('test-uuid-123');
      expect(result.type).toBe('document');
      expect(result.name).toBe('document.txt');
      expect(result.file).toBe(file);
      expect(result.status.type).toBe('requires-action');
    });

    test('successfully adds valid PDF file', async () => {
      const file = createMockFile('document.pdf', 'application/pdf', 1024);

      const result = await adapter.add({ file });

      expect(result.id).toBe('test-uuid-123');
      expect(result.type).toBe('document');
      expect(result.name).toBe('document.pdf');
      expect(result.file).toBe(file);
      expect(result.status.type).toBe('requires-action');
    });

    test('returns error for oversized file', async () => {
      const file = createMockFile('large.png', 'image/png', 150 * 1024 * 1024); // 150MB (exceeds 100MB limit)

      const result = await adapter.add({ file });

      expect(result.type).toBe('file');
      expect(result.name).toBe('large.png');
      expect(result.status.type).toBe('incomplete');
      expect(result.status.message).toContain('size exceeds');
    });

    test('returns error for invalid file type', async () => {
      const file = createMockFile(
        'document.docx',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        1024
      );

      const result = await adapter.add({ file });

      expect(result.type).toBe('file');
      expect(result.name).toBe('document.docx');
      expect(result.status.type).toBe('incomplete');
      expect(result.status.message).toContain('Unsupported file type');
    });

    test('returns error for file with empty name', async () => {
      const file = createMockFile('', 'image/png', 1024);

      const result = await adapter.add({ file });

      expect(result.status.type).toBe('incomplete');
      expect(result.status.message).toContain('valid name');
    });
  });

  describe('send method', () => {
    // An attachment add() rejected stays in the composer, and the composer does
    // not block sending on it, so send() is reached with a file the client has
    // already refused to upload.
    test('refuses to upload a file that fails validation', async () => {
      const uploads = [];
      server.use(
        http.post('/api/attachments/upload', () => {
          uploads.push('upload');
          return HttpResponse.json({ attachment_id: 'x', url: '/api/attachments/x' });
        })
      );
      const file = createMockFile('large.png', 'image/png', 150 * 1024 * 1024);

      const result = await adapter.send({ id: 'test-id', type: 'file', name: 'large.png', file });

      expect(uploads).toEqual([]);
      expect(result.status.type).toBe('incomplete');
      expect(result.status.message).toContain('size exceeds');
    });

    test('successfully processes attachment', async () => {
      const file = createMockFile('test.png', 'image/png', 1024);
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        file,
        status: { type: 'incomplete', reason: 'error', message: 'Failed to upload file' },
      };

      // MSW will handle the API call

      const result = await adapter.send(attachment);

      expect(result.id).toBe('test-id');
      expect(result.type).toBe('image');
      expect(result.name).toBe('test.png');
      // content is now ThreadUserMessagePart[] as required by @assistant-ui/react 0.12.15+
      expect(result.content).toEqual([
        { type: 'image', image: '/api/attachments/server-uuid-456' },
      ]);
      expect(result.uploadedId).toBe('server-uuid-456');
      expect(result.status.type).toBe('complete');

      // MSW handled the API call
    });

    test('handles upload failure gracefully', async () => {
      // Override the upload handler to return an error
      server.use(
        http.post('/api/attachments/upload', () => {
          return HttpResponse.json({ error: 'Upload failed' }, { status: 500 });
        })
      );

      const file = new File(['test content'], 'test.png', { type: 'image/png' });
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        file,
        status: { type: 'incomplete', reason: 'error', message: 'Failed to upload file' },
      };

      const result = await adapter.send(attachment);

      expect(result.id).toBe('test-id');
      expect(result.status.type).toBe('incomplete');
      expect(result.status.message).toContain('Failed to upload file');
    });

    test('handles network error gracefully', async () => {
      // Override the upload handler to return a network error
      server.use(
        http.post('/api/attachments/upload', () => {
          return HttpResponse.error();
        })
      );

      const file = new File(['test content'], 'test.png', { type: 'image/png' });
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        file,
        status: { type: 'incomplete', reason: 'error', message: 'Failed to upload file' },
      };

      const result = await adapter.send(attachment);

      expect(result.id).toBe('test-id');
      expect(result.status.type).toBe('incomplete');
      expect(result.status.message).toContain('Failed to upload file');
    });
  });

  // Uploads happen at send time, so an attachment the composer hands back has
  // nothing on the server to clean up.
  describe('remove method', () => {
    test('makes no server call for a pending attachment', async () => {
      const deleted = [];
      server.use(
        http.delete('/api/attachments/:attachmentId', ({ params }) => {
          deleted.push(params.attachmentId);
          return HttpResponse.json({ success: true });
        })
      );
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        status: { type: 'requires-action', reason: 'composer-send' },
      };

      await expect(adapter.remove(attachment)).resolves.toBeUndefined();

      expect(deleted).toEqual([]);
    });
  });
});

describe('CompositeAttachmentAdapter', () => {
  let mockImageAdapter;
  let compositeAdapter;

  beforeEach(() => {
    mockImageAdapter = {
      accept: 'image/*',
      add: vi.fn(),
      send: vi.fn(),
      remove: vi.fn(),
    };
    compositeAdapter = new CompositeAttachmentAdapter([mockImageAdapter]);
  });

  describe('getAdapterForType', () => {
    test('finds adapter for exact match', () => {
      const result = compositeAdapter.getAdapterForType('image/png');
      expect(result).toBe(mockImageAdapter);
    });

    test('finds adapter for comma-separated types', () => {
      mockImageAdapter.accept = 'image/jpeg,image/png,text/plain';
      const result = compositeAdapter.getAdapterForType('text/plain');
      expect(result).toBe(mockImageAdapter);
    });

    test('returns null for no match', () => {
      const result = compositeAdapter.getAdapterForType(
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
      );
      expect(result).toBeUndefined();
    });
  });

  describe('add method', () => {
    test('delegates to matching adapter', async () => {
      const file = createMockFile('test.png', 'image/png', 1024);
      const expectedResult = { id: 'test', type: 'image' };
      mockImageAdapter.add.mockResolvedValue(expectedResult);

      const result = await compositeAdapter.add({ file });

      expect(result).toBe(expectedResult);
      expect(mockImageAdapter.add).toHaveBeenCalledWith({ file });
    });

    test('returns error for unsupported file type', async () => {
      const file = createMockFile(
        'document.docx',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        1024
      );

      const result = await compositeAdapter.add({ file });

      expect(result.type).toBe('file');
      expect(result.name).toBe('document.docx');
      expect(result.status.type).toBe('incomplete');
      expect(result.status.message).toContain('Unsupported file type');
    });
  });

  describe('send method', () => {
    test('delegates to matching adapter', async () => {
      const attachment = {
        id: 'test',
        type: 'image',
        file: createMockFile('test.png', 'image/png', 1024),
      };
      const expectedResult = { id: 'test', status: { type: 'complete' } };
      mockImageAdapter.send.mockResolvedValue(expectedResult);

      const result = await compositeAdapter.send(attachment);

      expect(result).toBe(expectedResult);
      expect(mockImageAdapter.send).toHaveBeenCalledWith(attachment);
    });

    test('returns error for no matching adapter', async () => {
      const attachment = {
        id: 'test',
        type: 'document',
        file: createMockFile(
          'doc.docx',
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          1024
        ),
      };

      const result = await compositeAdapter.send(attachment);

      expect(result.id).toBe('test');
      expect(result.status.type).toBe('incomplete');
      expect(result.status.message).toContain('No adapter available');
    });
  });
});

describe('defaultAttachmentAdapter', () => {
  test('exports a pre-configured composite adapter', () => {
    expect(defaultAttachmentAdapter).toBeInstanceOf(CompositeAttachmentAdapter);
    expect(defaultAttachmentAdapter.adapters).toHaveLength(1);
    expect(defaultAttachmentAdapter.adapters[0]).toBeInstanceOf(FileAttachmentAdapter);
  });

  test('supports image files', async () => {
    const file = createMockFile('test.jpg', 'image/jpeg', 1024);

    const result = await defaultAttachmentAdapter.add({ file });

    expect(result.type).toBe('image');
    expect(result.status.type).toBe('requires-action');
  });

  test('supports text files', async () => {
    const file = createMockFile('readme.txt', 'text/plain', 1024);

    const result = await defaultAttachmentAdapter.add({ file });

    expect(result.type).toBe('document');
    expect(result.status.type).toBe('requires-action');
  });

  test('supports PDF files', async () => {
    const file = createMockFile('document.pdf', 'application/pdf', 1024);

    const result = await defaultAttachmentAdapter.add({ file });

    expect(result.type).toBe('document');
    expect(result.status.type).toBe('requires-action');
  });
});
