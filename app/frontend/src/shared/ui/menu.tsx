import {
  useLayoutEffect,
  useRef,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
  type RefObject,
} from 'react';
import {Icon, type IconName} from './icon';
import {useDismiss} from '@/shared/hooks/use_dismiss';
import {joinClasses} from './cx';
import {fieldClasses} from './text_field';
import {presenceProps, usePresence} from './use_presence';

// A plain grid track grows to an unbreakable label's width, so rows could
// never ellipsize; a minmax(0) track holds them to the menu's width.
const SURFACE_CLASSES =
  'ui-motion-pop grid grid-cols-[minmax(0,1fr)] gap-0.5 overflow-y-auto rounded-2xl border ' +
  'border-cosci-border bg-cosci-menu-bg p-1.5 text-cosci-fg shadow-floating';

const ITEM_SELECTOR =
  '[role="menuitem"]:not([disabled]), [role="menuitemradio"]:not([disabled]), ' +
  '[role="menuitemcheckbox"]:not([disabled])';

// Arrow keys, Home and End move focus between items, as `role="menu"`
// promises.
function onMenuKeyDown(event: KeyboardEvent<HTMLElement>) {
  const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End'];
  if (!keys.includes(event.key)) return;
  const items = Array.from(
    event.currentTarget.querySelectorAll<HTMLElement>(ITEM_SELECTOR),
  );
  if (items.length === 0) return;
  event.preventDefault();
  const current = items.indexOf(document.activeElement as HTMLElement);
  const last = items.length - 1;
  const next = {
    ArrowDown: current < 0 || current === last ? 0 : current + 1,
    ArrowUp: current <= 0 ? last : current - 1,
    Home: 0,
    End: last,
  }[event.key as 'ArrowDown' | 'ArrowUp' | 'Home' | 'End'];
  items[next].focus();
}

// Closing with focus inside the menu would drop it on <body>; hand it back to
// the trigger instead.
function returnFocus(
  menu: HTMLElement | null,
  anchors: RefObject<HTMLElement | null>[],
) {
  if (!menu?.contains(document.activeElement)) return;
  const anchor = anchors[0]?.current;
  const trigger = anchor?.matches('button')
    ? anchor
    : anchor?.querySelector<HTMLElement>('button, [href]');
  trigger?.focus();
}

interface MenuProps {
  open: boolean;
  onClose: () => void;
  label: string;
  // Pointer presses inside these (the trigger first) do not count as outside.
  anchorRefs: RefObject<HTMLElement | null>[];
  // Position, size, stacking and transform origin only.
  layoutClassName: string;
  // Measured placement for menus anchored with fixed positioning.
  style?: CSSProperties;
  menuRef?: RefObject<HTMLDivElement | null>;
  children: ReactNode;
}

export function Menu({
  open,
  onClose,
  label,
  anchorRefs,
  layoutClassName,
  style,
  menuRef,
  children,
}: MenuProps) {
  const {mounted, state} = usePresence(open);
  const ownRef = useRef<HTMLDivElement>(null);
  const ref = menuRef ?? ownRef;
  const anchors = useRef(anchorRefs);
  anchors.current = anchorRefs;
  useLayoutEffect(() => {
    if (!open) return;
    const menu = ref.current;
    const items = Array.from(
      menu?.querySelectorAll<HTMLElement>(ITEM_SELECTOR) ?? [],
    );
    const selected = items.find(
      item => item.getAttribute('aria-checked') === 'true',
    );
    // Anchored menus are placed by their parent after this layout effect.
    // Focusing now must not scroll an ancestor toward the initial position.
    (selected ?? items[0])?.focus({preventScroll: true});
    return () => returnFocus(menu, anchors.current);
  }, [open, ref]);
  useDismiss(open, () => {
    returnFocus(ref.current, anchorRefs);
    onClose();
  }, [...anchorRefs, ref]);
  if (!mounted) return null;
  return (
    <div
      ref={ref}
      role="menu"
      aria-label={label}
      style={style}
      className={joinClasses(SURFACE_CLASSES, layoutClassName)}
      onKeyDown={event => {
        if (event.key === 'Tab') {
          returnFocus(ref.current, anchorRefs);
          onClose();
        } else {
          onMenuKeyDown(event);
        }
      }}
      {...presenceProps(state)}
    >
      {children}
    </div>
  );
}

const MENU_ITEM_CLASSES =
  'flex min-h-[2.5rem] w-full cursor-pointer items-center gap-3 ' +
  'rounded-xl border-0 bg-transparent px-3 text-left font-[inherit] ' +
  'text-[0.875rem] text-cosci-fg no-underline hover:bg-cosci-menu-row-hover ' +
  'focus-visible:bg-cosci-menu-row-hover focus-visible:outline-2 ' +
  'focus-visible:-outline-offset-2 focus-visible:outline-th-ring ' +
  'disabled:cursor-default disabled:text-cosci-muted';

interface MenuItemProps extends Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  'className' | 'role'
> {
  kind?: 'item' | 'radio' | 'checkbox';
  checked?: boolean;
  icon?: IconName;
  // Radio items show a check when chosen; checkbox items show a switch.
  indicator?: boolean;
  ref?: Ref<HTMLButtonElement>;
}

const ROLE = {
  item: 'menuitem',
  radio: 'menuitemradio',
  checkbox: 'menuitemcheckbox',
} as const;

// The knob slides between ends rather than jumping.
function SwitchIndicator({on}: {on: boolean}) {
  return (
    <span
      aria-hidden="true"
      data-on={on}
      className={joinClasses(
        'relative ml-auto h-[0.95rem] w-[1.6rem] flex-none rounded-full',
        'transition-colors duration-medium ease-standard',
        on ? 'bg-cosci-toggle-on-track' : 'bg-cosci-toggle-off-track',
      )}
    >
      <span
        className={joinClasses(
          'absolute top-[0.15rem] left-[0.18rem] size-[0.65rem] rounded-full',
          'transition-[translate,background-color] duration-medium ease-standard',
          on
            ? 'translate-x-[0.59rem] bg-cosci-toggle-on-knob'
            : 'bg-cosci-toggle-off-knob',
        )}
      />
    </span>
  );
}

export function MenuItem({
  kind = 'item',
  checked,
  icon,
  indicator = false,
  children,
  type = 'button',
  ...rest
}: MenuItemProps) {
  return (
    <button
      type={type}
      role={ROLE[kind]}
      aria-checked={kind === 'item' ? undefined : Boolean(checked)}
      className={MENU_ITEM_CLASSES}
      {...rest}
    >
      {icon && (
        <Icon
          className="flex-none text-[1.15rem] text-cosci-menu-icon"
          name={icon}
        />
      )}
      {children}
      {indicator && kind === 'checkbox' && <SwitchIndicator on={!!checked} />}
      {indicator && kind === 'radio' && checked && (
        <Icon
          className="ml-auto flex-none text-[1.05rem] text-cosci-blue"
          name="check"
        />
      )}
    </button>
  );
}

// A field-shaped button that opens a menu of choices (the select pattern).
export function SelectTrigger({
  open,
  children,
  type = 'button',
  ...rest
}: Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className'> & {
  open: boolean;
  ref?: Ref<HTMLButtonElement>;
}) {
  return (
    <button
      type={type}
      aria-haspopup="menu"
      aria-expanded={open}
      className={joinClasses(
        fieldClasses(),
        'flex cursor-pointer items-center justify-between gap-3 text-left',
        'enabled:hover:bg-cosci-menu-row-hover',
      )}
      {...rest}
    >
      {children}
      <Icon
        className={joinClasses(
          'flex-none text-[1.15rem] text-cosci-muted',
          'transition-[rotate] duration-medium ease-standard',
          open && 'rotate-180',
        )}
        name="expand_more"
      />
    </button>
  );
}
