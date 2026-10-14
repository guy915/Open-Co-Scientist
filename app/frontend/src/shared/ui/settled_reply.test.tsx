import {render, screen} from '@testing-library/react';
import {expect, it} from 'vitest';
import {SettledReply} from './settled_reply';

it('keeps saved history quiet and announces a completed new reply once', () => {
  const {rerender} = render(
    <SettledReply busy={false} reply="Saved history" revision="old" />,
  );
  const status = screen.getByRole('status', {name: 'Research reply'});
  expect(status).toBeEmptyDOMElement();
  rerender(<SettledReply busy reply="Saved history" revision="old" />);
  expect(status).toBeEmptyDOMElement();
  rerender(<SettledReply busy reply="New answer" revision="new" />);
  expect(status).toBeEmptyDOMElement();
  rerender(<SettledReply busy={false} reply="New answer" revision="new" />);
  expect(status).toHaveTextContent('New answer');
  rerender(<SettledReply busy={false} reply="New answer" revision="new" />);
  expect(status).toHaveTextContent('New answer');
  rerender(<SettledReply busy reply="New answer" revision="new" />);
  expect(status).toBeEmptyDOMElement();
  rerender(
    <SettledReply busy={false} reply="New answer" revision="another-turn" />,
  );
  expect(status).toHaveTextContent('New answer');
});

it('does not repeat the previous reply when a turn fails or returns no prose', () => {
  const {rerender} = render(
    <SettledReply busy reply="Previous" revision="old" />,
  );
  rerender(<SettledReply busy={false} reply="Previous" revision="old" />);
  expect(screen.getByRole('status')).toBeEmptyDOMElement();
  rerender(<SettledReply busy reply="Previous" revision="old" />);
  rerender(<SettledReply busy={false} reply="" revision="empty" />);
  expect(screen.getByRole('status')).toBeEmptyDOMElement();
});
