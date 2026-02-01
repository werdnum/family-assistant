/**
 * React Error Boundary component that catches component errors
 * and reports them to the backend.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';
import { reportErrorFromException } from '../api/errorClient';
import { getDiagnosticsUrl } from '../utils/diagnosticsUrl';
import { Button } from './ui/button';

interface ErrorBoundaryProps {
  children: ReactNode;
  componentName?: string;
  fallback?: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

/**
 * Error boundary component that catches errors in its child component tree.
 *
 * When an error is caught:
 * 1. Reports the error to the backend via errorClient
 * 2. Displays a fallback UI (custom or default)
 * 3. Provides a "Try Again" button to reset the error state
 *
 * Usage:
 * ```tsx
 * <ErrorBoundary componentName="ChatApp">
 *   <ChatApp />
 * </ErrorBoundary>
 *
 * // With custom fallback:
 * <ErrorBoundary
 *   componentName="MyComponent"
 *   fallback={<div>Something went wrong</div>}
 * >
 *   <MyComponent />
 * </ErrorBoundary>
 * ```
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // Report the error to the backend
    reportErrorFromException(error, 'component_error', this.props.componentName, {
      componentStack: errorInfo.componentStack,
    });
  }

  handleRetry = (): void => {
    this.setState({ hasError: false, error: null });
  };

  render(): ReactNode {
    if (this.state.hasError) {
      // If a custom fallback is provided, use it
      if (this.props.fallback) {
        return this.props.fallback;
      }

      // Default fallback UI
      return (
        <div className="flex min-h-[200px] flex-col items-center justify-center gap-4 p-8 text-center">
          <div className="text-destructive">
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="48"
              height="48"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
          </div>
          <h2 className="text-lg font-semibold">Something went wrong</h2>
          <p className="text-sm text-muted-foreground">
            {this.state.error?.message || 'An unexpected error occurred'}
          </p>
          <div className="flex gap-2">
            <Button onClick={this.handleRetry} variant="outline">
              Try Again
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <a href={getDiagnosticsUrl()} target="_blank" rel="noopener noreferrer">
                View Diagnostics
              </a>
            </Button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
