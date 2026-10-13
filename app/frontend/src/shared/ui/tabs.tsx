import {useRef, type ReactNode} from 'react';
import {Link, type LinkProps} from 'react-router-dom';
import {Icon, type IconName} from './icon';
import {
  type IndicatorBox,
  useSlidingIndicator,
} from '@/shared/hooks/use_sliding_indicator';
import {joinClasses} from './cx';

// One moving thumb owns the selected fill, so a change of selection slides
// instead of one background vanishing and another appearing. The track
// measures (not the thumb): a child's layout effect runs before its parent's
// ref is attached.
function SlidingThumb({
  box,
  className,
  children,
}: {
  box: IndicatorBox | null;
  className: string;
  children?: ReactNode;
}) {
  if (!box) return null;
  return (
    <span
      aria-hidden="true"
      data-animate={box.animate}
      className={joinClasses('ui-sliding-thumb', className)}
      style={{width: box.width, translate: `${box.left}px 0`}}
    >
      {children}
    </span>
  );
}

export interface SegmentOption<T extends string> {
  value: T;
  label: ReactNode;
  icon?: IconName;
}

export type SegmentedSize = 'md' | 'lg';

const SEGMENT_TRACK_CLASSES: Record<SegmentedSize, string> = {
  md: 'grid auto-cols-fr grid-flow-col gap-1 border border-segmented-border p-0.5',
  // Landing tiers size to their labels and scroll on narrow screens.
  lg: 'inline-flex max-w-full gap-1 overflow-x-auto p-1 [scrollbar-width:none]',
};

const SEGMENT_THUMB_CLASSES: Record<SegmentedSize, string> = {
  md: 'inset-y-[0.18rem]',
  lg: 'inset-y-1',
};

const SEGMENT_ITEM_CLASSES: Record<SegmentedSize, string> = {
  md: 'min-h-[2.6rem] px-2 text-[0.875rem] font-semibold',
  lg: 'h-10 flex-none px-5 text-[0.9375rem] font-medium max-[900px]:px-3.5',
};

// A pressed-button group for choosing one value in place (theme, tier). Links
// that change the route use TabNav instead.
export function SegmentedControl<T extends string>({
  label,
  options,
  value,
  onChange,
  size = 'md',
  layoutClassName,
}: {
  label: string;
  options: readonly SegmentOption<T>[];
  value: T;
  onChange: (value: T) => void;
  size?: SegmentedSize;
  layoutClassName?: string;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const box = useSlidingIndicator(trackRef, '[aria-pressed="true"]', value);
  return (
    <div
      ref={trackRef}
      role="group"
      aria-label={label}
      className={joinClasses(
        'relative rounded-full bg-segmented-track',
        SEGMENT_TRACK_CLASSES[size],
        layoutClassName,
      )}
    >
      <SlidingThumb
        box={box}
        className={joinClasses(
          'pointer-events-none absolute left-0 rounded-full bg-segmented-thumb',
          SEGMENT_THUMB_CLASSES[size],
        )}
      />
      {options.map(option => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
          className={joinClasses(
            'relative z-1 inline-flex min-w-0 cursor-pointer items-center',
            'justify-center gap-2 rounded-full border-0 bg-transparent',
            'font-[inherit] text-segmented-fg',
            'aria-[pressed=false]:hover:bg-segmented-hover',
            'aria-pressed:text-segmented-selected-fg focus-visible:outline-2',
            'focus-visible:outline-offset-2 focus-visible:outline-th-ring',
            SEGMENT_ITEM_CLASSES[size],
          )}
        >
          {option.icon && (
            <Icon className="text-[1.15rem]" name={option.icon} />
          )}
          <span>{option.label}</span>
        </button>
      ))}
    </div>
  );
}

// Switches between sections of one surface (Settings). Vertical on wide
// screens, a scrolling row on phones.
export function SectionNav<T extends string>({
  label,
  items,
  value,
  onChange,
}: {
  label: string;
  items: readonly SegmentOption<T>[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <nav
      className="grid gap-1.5 [align-content:start] phone:flex phone:overflow-x-auto phone:pb-0.5 phone:[scrollbar-width:none]"
      aria-label={label}
    >
      {items.map(item => (
        <button
          key={item.value}
          type="button"
          aria-current={item.value === value ? 'true' : undefined}
          onClick={() => onChange(item.value)}
          className={joinClasses(
            'flex min-h-11 cursor-pointer items-center gap-3 rounded-full border-0',
            'bg-transparent px-4 text-left font-[inherit] text-[0.875rem] font-medium',
            'text-cosci-fg hover:bg-cosci-menu-row-hover',
            'aria-[current=true]:bg-segmented-thumb aria-[current=true]:text-segmented-selected-fg',
            'focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-th-ring',
            'phone:min-h-10 phone:flex-none phone:gap-1.5',
            'phone:px-3 phone:text-[0.82rem] max-[360px]:!px-3',
          )}
        >
          {item.icon && (
            <Icon
              className="flex-none text-[1.25rem] max-[360px]:hidden"
              name={item.icon}
            />
          )}
          <span>{item.label}</span>
        </button>
      ))}
    </nav>
  );
}

export type TabNavVariant = 'underline' | 'pill';

const TRACK_CLASSES: Record<TabNavVariant, string> = {
  underline:
    'relative grid grid-flow-col auto-cols-fr border-b border-cosci-border',
  pill: 'relative isolate inline-grid grid-flow-col auto-cols-fr rounded-full bg-tab-pill-track',
};

const THUMB_CLASSES: Record<TabNavVariant, string> = {
  underline: 'absolute bottom-0 left-0 h-[0.18rem]',
  pill: 'absolute inset-y-0 left-0 rounded-full bg-tab-pill-thumb',
};

const LINK_CLASSES: Record<TabNavVariant, string> = {
  underline:
    'relative grid min-w-0 cursor-pointer content-center justify-items-center ' +
    'gap-1.5 py-2 text-sm no-underline text-cosci-muted ' +
    'hover:text-cosci-fg aria-[current=page]:text-cosci-blue',
  // The hover fill sits under the thumb and runs beneath its rounded end, so
  // a hovered segment fills its slot with no track showing between the two.
  pill:
    'relative flex h-full min-w-0 items-center justify-center ' +
    'gap-2 rounded-full px-3 no-underline text-tab-pill-fg ' +
    'aria-[current=page]:text-tab-pill-selected-fg ' +
    'before:absolute before:inset-0 before:-z-1 before:rounded-full ' +
    "before:content-[''] not-aria-[current=page]:hover:before:bg-tab-pill-hover " +
    '[&:has(+[aria-current=page])]:before:-right-5 ' +
    '[[aria-current=page]+&]:before:-left-5',
};

function tabLinkClasses(variant: TabNavVariant): string {
  return joinClasses(
    LINK_CLASSES[variant],
    'focus-visible:outline-2 focus-visible:-outline-offset-2 ' +
      'focus-visible:outline-th-ring',
  );
}

// Navigation between routes: real links with aria-current, which is what deep
// links and new tabs need; the indicator follows the current link.
export function TabNav({
  label,
  variant,
  current,
  layoutClassName,
  children,
}: {
  label: string;
  variant: TabNavVariant;
  // Changes whenever the current link changes, so the indicator re-measures.
  current: string;
  layoutClassName?: string;
  children: ReactNode;
}) {
  const trackRef = useRef<HTMLElement>(null);
  const box = useSlidingIndicator(trackRef, '[aria-current="page"]', current);
  return (
    <nav
      ref={trackRef}
      aria-label={label}
      className={joinClasses(TRACK_CLASSES[variant], layoutClassName)}
    >
      <SlidingThumb box={box} className={THUMB_CLASSES[variant]}>
        {variant === 'underline' && (
          <span className="mx-4 block h-full rounded-t-full bg-cosci-blue-strong" />
        )}
      </SlidingThumb>
      {children}
    </nav>
  );
}

export function TabNavLink({
  variant,
  current,
  className,
  ...rest
}: LinkProps & {variant: TabNavVariant; current: boolean}) {
  return (
    <Link
      {...rest}
      aria-current={current ? 'page' : undefined}
      className={joinClasses(tabLinkClasses(variant), className)}
    />
  );
}
