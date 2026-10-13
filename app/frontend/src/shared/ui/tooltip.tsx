import {joinClasses} from './cx';

export type TooltipPlacement = 'top' | 'right' | 'bottom' | 'left';

interface TooltipOptions {
  placement?: TooltipPlacement;
  // Long text wraps at a reading width instead of one line.
  wrap?: boolean;
  // Anchors wider than their tooltip align it to their start edge.
  alignStart?: boolean;
}

// The tooltip is the anchor's own ::after (styles/tooltips.css), so it shows on
// hover and keyboard focus without a portal or a listener.
export function tooltipClassNames({
  className,
  placement = 'top',
  wrap,
  alignStart,
}: TooltipOptions & {className?: string}): string {
  return joinClasses(
    className,
    'ucs-tooltip-anchor',
    `ucs-tooltip-${placement}`,
    wrap ? 'ucs-tooltip-wrap' : 'ucs-tooltip-nowrap',
    alignStart && 'ucs-tooltip-align-start',
  );
}
