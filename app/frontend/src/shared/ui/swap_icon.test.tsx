import {render} from '@testing-library/react';
import {expect, it} from 'vitest';
import {SwapIcon} from './swap_icon';

it('fades only icons that replace an earlier one', () => {
  const {container, rerender} = render(<SwapIcon name="content_copy" />);
  expect(container.querySelector('svg')).not.toHaveClass('ui-motion-swap');
  rerender(<SwapIcon name="check" />);
  expect(container.querySelector('svg')).toHaveClass('ui-motion-swap');
  rerender(<SwapIcon name="content_copy" />);
  expect(container.querySelector('svg')).toHaveClass('ui-motion-swap');
});
