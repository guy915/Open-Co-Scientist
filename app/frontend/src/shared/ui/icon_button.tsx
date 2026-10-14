import type {ButtonHTMLAttributes, Ref} from 'react';
import type {IconName} from './icon';
import {joinClasses} from './cx';
import {SwapIcon} from './swap_icon';
import {tooltipClassNames, type TooltipPlacement} from './tooltip';

type IconButtonVariant = 'ghost' | 'elevated';
type IconButtonSize = 'xs' | 'sm' | 'md';

// Always round. Hover colours come from --icon-button-* so a surface whose
// background equals the default hover (the user bubble, the nav rail) can
// re-point them in its own scope.
const BASE_CLASSES =
  'inline-grid flex-none cursor-pointer place-items-center rounded-full p-0 ' +
  'leading-none disabled:cursor-default disabled:text-icon-button-disabled-fg ' +
  'focus-visible:outline-2 focus-visible:outline-offset-2 ' +
  'focus-visible:outline-th-ring';

const VARIANT_CLASSES: Record<IconButtonVariant, string> = {
  ghost:
    'border-0 bg-transparent text-icon-button-fg ' +
    'enabled:hover:bg-icon-button-hover-bg enabled:hover:text-icon-button-hover-fg ' +
    'aria-expanded:bg-icon-button-hover-bg aria-expanded:text-icon-button-hover-fg',
  // Floats over scrolling content, so it carries a border and elevation.
  elevated:
    'border border-cosci-border bg-cosci-bg text-cosci-fg ' +
    'shadow-floating enabled:hover:bg-cosci-hover',
};

const SIZE_CLASSES: Record<IconButtonSize, string> = {
  xs: 'size-6 text-[1rem]',
  sm: 'size-8 text-[1.15rem]',
  md: 'size-10 text-[1.35rem]',
};

interface IconButtonStyle {
  variant?: IconButtonVariant;
  size?: IconButtonSize;
}

// For router links that look like icon buttons.
function iconButtonClasses({
  variant = 'ghost',
  size = 'sm',
}: IconButtonStyle = {}): string {
  return joinClasses(
    BASE_CLASSES,
    VARIANT_CLASSES[variant],
    SIZE_CLASSES[size],
  );
}

export interface IconButtonProps
  extends
    IconButtonStyle,
    Omit<
      ButtonHTMLAttributes<HTMLButtonElement>,
      'className' | 'children' | 'aria-label'
    > {
  icon: IconName;
  // The accessible name, and the tooltip unless `tooltip` overrides it.
  label: string;
  // `null` suppresses the tooltip where a visible label already names it.
  tooltip?: string | null;
  tooltipPlacement?: TooltipPlacement;
  layoutClassName?: string;
  ref?: Ref<HTMLButtonElement>;
}

export function IconButton({
  icon,
  label,
  tooltip,
  tooltipPlacement = 'top',
  variant,
  size,
  layoutClassName,
  type = 'button',
  ...rest
}: IconButtonProps) {
  const className = joinClasses(
    iconButtonClasses({variant, size}),
    layoutClassName,
  );
  const tip = tooltip === undefined ? label : tooltip;
  return (
    <button
      type={type}
      aria-label={label}
      className={
        tip
          ? tooltipClassNames({className, placement: tooltipPlacement})
          : className
      }
      data-tooltip={tip ?? undefined}
      {...rest}
    >
      <SwapIcon name={icon} />
    </button>
  );
}
