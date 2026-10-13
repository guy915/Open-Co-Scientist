import {render, screen} from '@testing-library/react';
import {expect, it} from 'vitest';
import {DocumentSkeleton} from './skeleton';

it('announces a loading document once and hides its placeholder shapes', () => {
  const {container} = render(<DocumentSkeleton label="Loading report…" />);
  expect(screen.getByRole('status')).toHaveTextContent('Loading report…');
  expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument();
  const shapes = container.querySelectorAll('.ui-skeleton');
  expect(shapes.length).toBeGreaterThan(4);
  for (const shape of shapes) {
    expect(shape).toHaveAttribute('aria-hidden', 'true');
  }
});
