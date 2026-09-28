import { render, screen, fireEvent } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ErrorBoundary } from '../ErrorBoundary';
import { _resetForTesting, forceFlush } from '../../api/errorClient';
import { server } from '../../test/setup.js';

function recordDeliveredReports(): Record<string, unknown>[] {
  const delivered: Record<string, unknown>[] = [];
  server.use(
    http.post('/api/errors/', async ({ request }) => {
      delivered.push((await request.json()) as Record<string, unknown>);
      return HttpResponse.json({ status: 'reported' });
    })
  );
  return delivered;
}

// Component that throws an error
const ThrowingComponent = ({ shouldThrow }: { shouldThrow: boolean }) => {
  if (shouldThrow) {
    throw new Error('Test component error');
  }
  return <div>Child component rendered</div>;
};

describe('ErrorBoundary', () => {
  beforeEach(() => {
    _resetForTesting();
    // Suppress console.error for expected errors
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    _resetForTesting();
    vi.restoreAllMocks();
  });

  it('should render children when there is no error', () => {
    render(
      <ErrorBoundary>
        <div>Child content</div>
      </ErrorBoundary>
    );

    expect(screen.getByText('Child content')).toBeInTheDocument();
  });

  it('should render fallback UI when child throws an error', () => {
    render(
      <ErrorBoundary>
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>
    );

    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    expect(screen.getByText('Test component error')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('should render custom fallback when provided', () => {
    render(
      <ErrorBoundary fallback={<div>Custom error message</div>}>
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>
    );

    expect(screen.getByText('Custom error message')).toBeInTheDocument();
    expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument();
  });

  it('should reset error state when Try Again is clicked', async () => {
    // Use a stateful wrapper to control whether the child throws
    let shouldThrow = true;
    const ControlledComponent = () => {
      if (shouldThrow) {
        throw new Error('Controlled error');
      }
      return <div>Working component</div>;
    };

    const { rerender } = render(
      <ErrorBoundary>
        <ControlledComponent />
      </ErrorBoundary>
    );

    // Verify error UI is shown
    expect(screen.getByText('Something went wrong')).toBeInTheDocument();

    // Stop throwing before clicking Try Again
    shouldThrow = false;

    // Click Try Again - this will reset the error state
    fireEvent.click(screen.getByRole('button', { name: /try again/i }));

    // Force rerender to pick up the new shouldThrow value
    rerender(
      <ErrorBoundary>
        <ControlledComponent />
      </ErrorBoundary>
    );

    expect(screen.getByText('Working component')).toBeInTheDocument();
  });

  it('should report the caught error to the backend as a component_error', async () => {
    const delivered = recordDeliveredReports();

    render(
      <ErrorBoundary componentName="TestComponent">
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>
    );
    await forceFlush();

    expect(delivered).toEqual([
      expect.objectContaining({
        message: 'Test component error',
        error_type: 'component_error',
        component_name: 'TestComponent',
        extra_data: { componentStack: expect.stringContaining('ThrowingComponent') },
      }),
    ]);
    expect(delivered[0]).not.toHaveProperty('severity');
  });

  it('should report a null component_name when no componentName prop is given', async () => {
    const delivered = recordDeliveredReports();

    render(
      <ErrorBoundary>
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>
    );
    await forceFlush();

    expect(delivered).toEqual([
      expect.objectContaining({
        message: 'Test component error',
        error_type: 'component_error',
        component_name: null,
      }),
    ]);
  });
});
