import {describe, it, expect} from 'vitest';
import {render, waitFor} from '@testing-library/react';
import {NoIndex} from './not_found_page';

describe('no index', () => {
  describe('NoIndex', () => {
    // NoIndex renders head metadata through an effect rather than DOM content.

    it('marks the page as noindex via the robots meta tag', async () => {
      render(<NoIndex title="Private" />);
      await waitFor(() => {
        const robots = document.head.querySelector('meta[name="robots"]');
        expect(robots).not.toBeNull();
        expect(robots).toHaveAttribute('content', 'noindex, nofollow');
      });
    });
  });
});
