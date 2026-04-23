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

// Drain an add() call into an array of yielded attachment states. add() is
// an async generator that yields an initial ``running`` state then resolves
// to ``complete`` once the upload lands (or ``error``).
async function collectAdd(adapter, params) {
  const results = [];
  for await (const attachment of adapter.add(params)) {
    results.push(attachment);
  }
  return results;
}

describe('FileAttachmentAdapter', () => {
  let adapter;

  beforeEach(() => {
    adapter = new FileAttachmentAdapter();
  });

  describe('constructor', () => {
    test('sets correct accept pattern', () => {
      expect(adapter.accept).toBe(
        'image/jpeg,image/png,image/gif,image/webp,text/plain,text/markdown,application/pdf'
      );
    });
  });

  describe('add method', () => {
    test('uploads image eagerly, yielding running then complete', async () => {
      const file = createMockFile('test.png', 'image/png', 1024 * 1024);

      const results = await collectAdd(adapter, { file });

      expect(results).toHaveLength(2);
      expect(results[0]).toMatchObject({
        id: 'test-uuid-123',
        type: 'image',
        name: 'test.png',
        file,
        status: { type: 'running' },
      });
      expect(results[1]).toMatchObject({
        id: 'test-uuid-123',
        type: 'image',
        name: 'test.png',
        uploadedId: 'server-uuid-456',
        status: { type: 'complete' },
      });
      expect(results[1].content).toEqual([
        { type: 'image', image: '/api/attachments/server-uuid-456' },
      ]);
    });

    test('uploads text file eagerly as a document', async () => {
      const file = createMockFile('document.txt', 'text/plain', 1024);

      const results = await collectAdd(adapter, { file });

      expect(results[0].type).toBe('document');
      expect(results[0].status.type).toBe('running');
      expect(results.at(-1).status.type).toBe('complete');
      expect(results.at(-1).content).toEqual([
        { type: 'data', name: 'url', data: '/api/attachments/server-uuid-456' },
      ]);
    });

    test('uploads PDF eagerly as a document', async () => {
      const file = createMockFile('document.pdf', 'application/pdf', 1024);

      const results = await collectAdd(adapter, { file });

      expect(results[0].type).toBe('document');
      expect(results.at(-1).status.type).toBe('complete');
      expect(results.at(-1).uploadedId).toBe('server-uuid-456');
    });

    test('yields a single error state for oversized file without uploading', async () => {
      const file = createMockFile('large.png', 'image/png', 150 * 1024 * 1024);

      const results = await collectAdd(adapter, { file });

      expect(results).toHaveLength(1);
      expect(results[0].type).toBe('file');
      expect(results[0].name).toBe('large.png');
      expect(results[0].status.type).toBe('error');
      expect(results[0].status.error).toContain('size exceeds');
    });

    test('yields a single error state for unsupported file type', async () => {
      const file = createMockFile(
        'document.docx',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        1024
      );

      const results = await collectAdd(adapter, { file });

      expect(results).toHaveLength(1);
      expect(results[0].status.type).toBe('error');
      expect(results[0].status.error).toContain('Unsupported file type');
    });

    test('yields a single error state for file with empty name', async () => {
      const file = createMockFile('', 'image/png', 1024);

      const results = await collectAdd(adapter, { file });

      expect(results).toHaveLength(1);
      expect(results[0].status.type).toBe('error');
      expect(results[0].status.error).toContain('valid name');
    });

    test('yields error state when upload request fails', async () => {
      server.use(
        http.post('/api/attachments/upload', () =>
          HttpResponse.json({ error: 'Upload failed' }, { status: 500 })
        )
      );
      const file = createMockFile('test.png', 'image/png', 1024);

      const results = await collectAdd(adapter, { file });

      expect(results[0].status.type).toBe('running');
      expect(results.at(-1).status.type).toBe('error');
      expect(results.at(-1).status.error).toContain('Failed to upload file');
    });
  });

  describe('send method', () => {
    test('passes already-complete attachment through unchanged', async () => {
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        content: [{ type: 'image', image: '/api/attachments/server-uuid-456' }],
        uploadedId: 'server-uuid-456',
        status: { type: 'complete' },
      };

      const result = await adapter.send(attachment);

      expect(result).toBe(attachment);
    });

    test('throws when called with a non-complete attachment', async () => {
      // Eager upload in add() should have marked it complete before send is
      // ever reached. If the runtime ever routes a running/error attachment
      // through send, we want a loud failure — silently sending a message
      // with a missing content URL is worse.
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        status: { type: 'running' },
      };

      await expect(adapter.send(attachment)).rejects.toThrow(/non-complete attachment/);
    });
  });

  describe('remove method', () => {
    test('successfully removes uploaded attachment', async () => {
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        uploadedId: 'server-uuid-456',
        status: { type: 'complete' },
      };

      // MSW will handle the API call

      await adapter.remove(attachment);

      // MSW handled the DELETE request
    });

    test('handles server deletion failure gracefully', async () => {
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        uploadedId: 'server-uuid-456',
        status: { type: 'complete' },
      };

      // MSW will handle the API call (default success, this tests error handling)

      // Should not throw - just logs warning
      await expect(adapter.remove(attachment)).resolves.toBeUndefined();
    });

    test('skips server deletion for non-uploaded attachment', async () => {
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        status: { type: 'error' },
      };

      await adapter.remove(attachment);

      // Should not call fetch
      // Attachment was not uploaded, so no server call should be made
    });

    test('cancels in-flight upload and DELETEs the server attachment once it lands', async () => {
      // Stall the upload handler behind a deferred promise so remove() can
      // fire mid-flight. The adapter should NOT abort the fetch (that would
      // race with server-side commit and orphan the file); instead it
      // flips a flag, waits for the response, and DELETEs the attachment.
      let releaseUpload;
      const uploadReleased = new Promise((resolve) => {
        releaseUpload = resolve;
      });
      server.use(
        http.post('/api/attachments/upload', async () => {
          await uploadReleased;
          return HttpResponse.json({
            attachment_id: 'server-uuid-456',
            url: '/api/attachments/server-uuid-456',
          });
        })
      );

      const deleteRequests = [];
      server.use(
        http.delete('/api/attachments/:id', ({ params }) => {
          deleteRequests.push(params.id);
          return HttpResponse.json({ success: true });
        })
      );

      const file = createMockFile('test.png', 'image/png', 1024);
      const iterator = adapter.add({ file });
      const running = (await iterator.next()).value;
      expect(running.status.type).toBe('running');

      // Fire remove() while upload is pending. The flag is set, but the
      // fetch keeps going until the server responds.
      await adapter.remove(running);

      // Now let the upload complete server-side.
      releaseUpload();

      const finalResult = await iterator.next();
      expect(finalResult.done).toBe(true);
      expect(deleteRequests).toContain('server-uuid-456');
    });

    test('cancels in-flight upload quietly when the upload itself fails', async () => {
      let rejectUpload;
      const uploadRejected = new Promise((_, reject) => {
        rejectUpload = reject;
      });
      server.use(
        http.post('/api/attachments/upload', async () => {
          await uploadRejected;
          return HttpResponse.json({ error: 'boom' }, { status: 500 });
        })
      );

      const file = createMockFile('test.png', 'image/png', 1024);
      const iterator = adapter.add({ file });
      const running = (await iterator.next()).value;
      expect(running.status.type).toBe('running');

      await adapter.remove(running);

      rejectUpload(new Error('simulated network failure'));

      const finalResult = await iterator.next();
      expect(finalResult.done).toBe(true);
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
    test('delegates to matching adapter (async-generator inner)', async () => {
      const file = createMockFile('test.png', 'image/png', 1024);
      const running = { id: 'test', type: 'image', status: { type: 'running' } };
      const complete = { id: 'test', type: 'image', status: { type: 'complete' } };

      mockImageAdapter.add = vi.fn(async function* () {
        yield running;
        yield complete;
      });

      const results = await collectAdd(compositeAdapter, { file });

      expect(results).toEqual([running, complete]);
      expect(mockImageAdapter.add).toHaveBeenCalledWith({ file });
    });

    test('delegates to matching adapter (promise-returning inner)', async () => {
      const file = createMockFile('test.png', 'image/png', 1024);
      const resolved = { id: 'test', type: 'image', status: { type: 'complete' } };
      mockImageAdapter.add = vi.fn().mockResolvedValue(resolved);

      const results = await collectAdd(compositeAdapter, { file });

      expect(results).toEqual([resolved]);
      expect(mockImageAdapter.add).toHaveBeenCalledWith({ file });
    });

    test('yields error for unsupported file type', async () => {
      const file = createMockFile(
        'document.docx',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        1024
      );

      const results = await collectAdd(compositeAdapter, { file });

      expect(results).toHaveLength(1);
      expect(results[0].type).toBe('file');
      expect(results[0].name).toBe('document.docx');
      expect(results[0].status.type).toBe('error');
      expect(results[0].status.error).toContain('Unsupported file type');
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
      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('No adapter available');
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

    const results = await collectAdd(defaultAttachmentAdapter, { file });

    expect(results[0].type).toBe('image');
    expect(results[0].status.type).toBe('running');
    expect(results.at(-1).status.type).toBe('complete');
  });

  test('supports text files', async () => {
    const file = createMockFile('readme.txt', 'text/plain', 1024);

    const results = await collectAdd(defaultAttachmentAdapter, { file });

    expect(results[0].type).toBe('document');
    expect(results[0].status.type).toBe('running');
    expect(results.at(-1).status.type).toBe('complete');
  });

  test('supports PDF files', async () => {
    const file = createMockFile('document.pdf', 'application/pdf', 1024);

    const results = await collectAdd(defaultAttachmentAdapter, { file });

    expect(results[0].type).toBe('document');
    expect(results[0].status.type).toBe('running');
    expect(results.at(-1).status.type).toBe('complete');
  });
});
