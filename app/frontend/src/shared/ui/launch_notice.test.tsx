import {render, screen} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {LaunchNotice} from './launch_notice';

afterEach(() => vi.useRealTimers());

it('announces plain text safely and does not invent a return time', () => {
  render(<LaunchNotice message="Research paused: <script>example</script>" />);
  const notice = screen.getByRole('status');
  expect(notice).toHaveTextContent('<script>example</script>');
  expect(notice.querySelector('script')).toBeNull();
  expect(notice.querySelector('time')).toBeNull();
  expect(notice).toHaveTextContent('We will update this notice');
  expect(notice).toHaveAttribute('aria-live', 'polite');
});

it('shows the expected return as a machine-readable instant with a timezone', () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-08T10:00:00Z'));
  render(
    <LaunchNotice
      message="Free capacity is used up."
      resumesAt={Date.parse('2026-10-09T00:00:00Z') / 1000}
    />,
  );
  const time = screen.getByRole('status').querySelector('time');
  expect(time).toHaveAttribute('dateTime', '2026-10-09T00:00:00.000Z');
  expect(time?.textContent).toMatch(/UTC|GMT|[A-Z]{2,5}/);
});

it.each([NaN, Infinity, -1])(
  'uses an unknown return time for invalid or expired timestamps (%s)',
  resumesAt => {
    render(<LaunchNotice message="Research paused." resumesAt={resumesAt} />);
    expect(screen.getByRole('status').querySelector('time')).toBeNull();
  },
);
