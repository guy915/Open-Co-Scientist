import type {ButtonHTMLAttributes, ReactNode, Ref} from 'react';
import {Icon, type IconName} from './icon';
import {joinClasses} from './cx';
import {SwapIcon} from './swap_icon';
import {tooltipClassNames, type TooltipPlacement} from './tooltip';

type ButtonVariant =
  'filled' | 'outlined' | 'accent' | 'tonal' | 'text' | 'disclosure' | 'link';
type ButtonSize = 'sm' | 'md' | 'lg';

// Shape, focus ring and state colours live here so no call site restyles a
// button. No preflight runs, so the UA border and padding are reset too.
const BASE_CLASSES =
  'inline-flex cursor-pointer items-center justify-center rounded-full border ' +
  'text-center font-[inherit] no-underline select-none ' +
  'disabled:cursor-default focus-visible:outline-2 ' +
  'focus-visible:outline-offset-2 focus-visible:outline-th-ring';

const DISABLED_CLASSES =
  'disabled:border-button-disabled-border disabled:bg-button-disabled-bg ' +
  'disabled:text-button-disabled-fg';

// :enabled excludes anchors; :not(:disabled) gives links the same hover state
// while native disabled buttons keep theirs suppressed.
const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  filled:
    'border-button-filled-bg bg-button-filled-bg text-button-filled-fg ' +
    `not-disabled:hover:bg-button-filled-hover ${DISABLED_CLASSES}`,
  outlined:
    'border-button-outlined-border bg-transparent text-button-outlined-fg ' +
    `not-disabled:hover:bg-button-outlined-hover ${DISABLED_CLASSES}`,
  // Suggested next steps: outlined, tinted with the link accent.
  accent:
    'border-button-accent-border bg-transparent text-button-accent-fg ' +
    `not-disabled:hover:bg-button-accent-hover ${DISABLED_CLASSES}`,
  tonal:
    'border-transparent bg-button-tonal-bg text-button-tonal-fg ' +
    'not-disabled:hover:bg-button-tonal-hover aria-expanded:bg-button-tonal-hover ' +
    DISABLED_CLASSES,
  text:
    'border-transparent bg-transparent text-button-text-fg ' +
    'not-disabled:hover:bg-button-text-hover-bg not-disabled:hover:text-button-text-hover-fg ' +
    'disabled:text-button-disabled-fg',
  // Show/hide toggles: quiet text and a chevron, never a filled shape.
  disclosure:
    'border-transparent bg-transparent text-button-text-fg ' +
    'not-disabled:hover:text-button-text-hover-fg disabled:text-button-disabled-fg',
  link:
    'border-transparent bg-transparent text-button-link-fg underline-offset-2 ' +
    'not-disabled:hover:underline disabled:text-button-disabled-fg',
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'min-h-[2.35rem] gap-2 whitespace-nowrap px-3 text-[0.875rem] font-semibold',
  // Long labels wrap on phones rather than overflow.
  md: 'min-h-[2.6rem] gap-2 px-6 py-1.5 font-medium',
  lg: 'min-h-12 gap-2 whitespace-nowrap px-6 text-base font-medium',
};

// Text, disclosure and link buttons sit among text, so they stay compact;
// `md` keeps the surrounding font size.
const COMPACT_VARIANTS = new Set<ButtonVariant>(['text', 'disclosure', 'link']);

const COMPACT_SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'gap-1 px-2 py-1 text-[0.875rem] font-medium',
  md: 'gap-1 px-2 py-1 font-medium',
  lg: 'gap-1.5 px-3 py-1.5 text-base font-medium',
};

const ICON_CLASSES: Record<ButtonSize, string> = {
  sm: 'text-[1.05rem]',
  md: 'text-[1.15rem]',
  lg: 'text-[1.25rem]',
};

export interface ButtonStyle {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

// For router links and anchors that look like buttons.
export function buttonClasses({
  variant = 'filled',
  size = 'md',
}: ButtonStyle = {}): string {
  return joinClasses(
    BASE_CLASSES,
    VARIANT_CLASSES[variant],
    COMPACT_VARIANTS.has(variant)
      ? COMPACT_SIZE_CLASSES[size]
      : SIZE_CLASSES[size],
  );
}

function buttonIconClasses(size: ButtonSize = 'md'): string {
  return joinClasses('flex-none', ICON_CLASSES[size]);
}

export interface ButtonProps
  extends
    ButtonStyle,
    Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className'> {
  icon?: IconName;
  trailingIcon?: IconName;
  tooltip?: string;
  tooltipPlacement?: TooltipPlacement;
  // Layout only (margins, grid placement, width); never colour or shape.
  layoutClassName?: string;
  ref?: Ref<HTMLButtonElement>;
  children?: ReactNode;
}

export function Button({
  variant,
  size = 'md',
  icon,
  trailingIcon,
  tooltip,
  tooltipPlacement = 'top',
  layoutClassName,
  type = 'button',
  children,
  ...rest
}: ButtonProps) {
  const className = joinClasses(
    buttonClasses({variant, size}),
    layoutClassName,
  );
  return (
    <button
      type={type}
      className={
        tooltip
          ? tooltipClassNames({className, placement: tooltipPlacement})
          : className
      }
      data-tooltip={tooltip}
      {...rest}
    >
      {icon && <SwapIcon className={buttonIconClasses(size)} name={icon} />}
      {children}
      {trailingIcon && (
        <Icon className={buttonIconClasses(size)} name={trailingIcon} />
      )}
    </button>
  );
}
