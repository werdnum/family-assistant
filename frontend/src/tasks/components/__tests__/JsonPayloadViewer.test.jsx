import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import JsonPayloadViewer, { LARGE_PAYLOAD_THRESHOLD } from '../JsonPayloadViewer';

// Mock vanilla-jsoneditor dynamic import
const mockDestroy = vi.fn();
const mockUpdate = vi.fn();
vi.mock('vanilla-jsoneditor', () => ({
  JSONEditor: class MockJSONEditor {
    constructor({ target }) {
      const el = document.createElement('div');
      el.setAttribute('data-testid', 'json-editor');
      target.appendChild(el);
    }
    destroy = mockDestroy;
    update = mockUpdate;
  },
}));

const smallPayload = { name: 'test', value: 42 };

function makeLargePayload() {
  const obj = {};
  let i = 0;
  while (JSON.stringify(obj).length <= LARGE_PAYLOAD_THRESHOLD) {
    obj[`key_${i}`] = `value_${'x'.repeat(100)}_${i}`;
    i++;
  }
  return obj;
}

describe('JsonPayloadViewer', () => {
  it('renders collapsed by default', () => {
    render(<JsonPayloadViewer data={smallPayload} taskId="1" />);
    expect(screen.getByText('Task Payload')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /show/i })).toBeInTheDocument();
    expect(screen.queryByTestId('json-editor')).not.toBeInTheDocument();
  });

  it('shows rich editor directly for small payloads when expanded', async () => {
    const user = userEvent.setup();
    render(<JsonPayloadViewer data={smallPayload} taskId="1" />);

    await user.click(screen.getByRole('button', { name: /show/i }));

    expect(await screen.findByTestId('json-editor')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /load rich editor/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/"name"/)).not.toBeInTheDocument(); // no <pre> fallback
  });

  it('shows pre fallback with Load Rich Editor button for large payloads', async () => {
    const user = userEvent.setup();
    const largePayload = makeLargePayload();
    render(<JsonPayloadViewer data={largePayload} taskId="1" />);

    await user.click(screen.getByRole('button', { name: /show/i }));

    expect(screen.getByRole('button', { name: /load rich editor/i })).toBeInTheDocument();
    expect(screen.queryByTestId('json-editor')).not.toBeInTheDocument();
    // Verify pre content is rendered
    const preElement = document.querySelector('pre');
    expect(preElement).toBeInTheDocument();
    expect(preElement.textContent).toContain('key_0');
  });

  it('switches from pre to rich editor when Load Rich Editor is clicked', async () => {
    const user = userEvent.setup();
    const largePayload = makeLargePayload();
    render(<JsonPayloadViewer data={largePayload} taskId="1" />);

    await user.click(screen.getByRole('button', { name: /show/i }));
    expect(document.querySelector('pre')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /load rich editor/i }));

    expect(await screen.findByTestId('json-editor')).toBeInTheDocument();
    expect(document.querySelector('pre')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /load rich editor/i })).not.toBeInTheDocument();
  });

  it('toggles between show and hide', async () => {
    const user = userEvent.setup();
    render(<JsonPayloadViewer data={smallPayload} taskId="1" />);

    await user.click(screen.getByRole('button', { name: /show/i }));
    expect(screen.getByRole('button', { name: /hide/i })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /hide/i }));
    expect(screen.getByRole('button', { name: /show/i })).toBeInTheDocument();
    expect(screen.queryByTestId('json-editor')).not.toBeInTheDocument();
  });
});
