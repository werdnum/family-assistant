import { render, screen, fireEvent } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ErrorBoundary } from '../ErrorBoundary';
import { _resetForTesting } from '../../api/errorClient';

// Mock the errorClient module
vi.mock('../../api/errorClient', async () => {
  const actual = await vi.importActual('../../api/errorClient');
  return {
    ...actual,
    reportErrorFromException: vi.fn(),
  };
});

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
    vi.clearAllMocks();
    // Suppress console.error for expected errors
    vi.spyOn(console, 'error').mockImplementation(() => {});
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

  it('should report errors via errorClient', async () => {
    const { reportErrorFromException } = await import('../../api/errorClient');

    render(
      <ErrorBoundary componentName="TestComponent">
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>
    );

    expect(reportErrorFromException).toHaveBeenCalledWith(
      expect.any(Error),
      'component_error',
      'TestComponent',
      expect.objectContaining({
        componentStack: expect.any(String),
      })
    );
  });

  it('should include component name in error report', async () => {
    const { reportErrorFromException } = await import('../../api/errorClient');

    render(
      <ErrorBoundary componentName="MySpecificComponent">
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>
    );

    expect(reportErrorFromException).toHaveBeenCalledWith(
      expect.any(Error),
      'component_error',
      'MySpecificComponent',
      expect.any(Object)
    );
  });

  it('should work without componentName prop', async () => {
    const { reportErrorFromException } = await import('../../api/errorClient');

    render(
      <ErrorBoundary>
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>
    );

    expect(reportErrorFromException).toHaveBeenCalledWith(
      expect.any(Error),
      'component_error',
      undefined,
      expect.any(Object)
    );
  });
});
