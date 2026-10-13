import {describe, it, expect, vi, beforeEach, afterEach} from 'vitest';
import {render, screen} from '@testing-library/react';
import {ErrorBoundary} from './error_boundary';

function Boom(): never {
  throw new Error('boom');
}

describe('ErrorBoundary', () => {
  beforeEach(() => {
    // React logs caught render errors; silence only the expected noise.
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the default fallback when a child throws', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
  });
});
