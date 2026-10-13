import type {MouseEventHandler} from 'react';
import {Link, type LinkProps} from 'react-router-dom';
import {Icon, type IconName} from './icon';
import {joinClasses} from './cx';
import {tooltipClassNames} from './tooltip';

// The pressed colour stays in shell_surface.css (`ucs-nav-item`), which is
// unlayered and so beats the hover utility.
const ITEM_CLASSES =
  'ucs-nav-item grid size-[2.5rem] min-h-[2.5rem] min-w-[2.5rem] cursor-pointer place-items-center [border:0] rounded-full bg-transparent p-0 text-cosci-shell-icon no-underline ' +
  'hover:bg-cosci-shell-hover-bg hover:text-cosci-fg focus-visible:bg-cosci-shell-hover-bg focus-visible:text-cosci-fg ' +
  'phone:h-[2.75rem] phone:min-h-[2.75rem] phone:w-full ' +
  'phone:grid-cols-[1.5rem_minmax(0,1fr)] phone:[justify-items:start] phone:gap-x-3 ' +
  'phone:px-3 phone:text-left';

const ITEM_OPEN_CLASSES =
  'above-phone:h-[2.45rem] above-phone:min-h-[2.45rem] above-phone:w-full above-phone:grid-cols-[1.5rem_minmax(0,1fr)] ' +
  'above-phone:[justify-content:stretch] above-phone:[justify-items:start] above-phone:gap-x-3 above-phone:px-3 ' +
  'above-phone:text-left above-phone:leading-none';

const ITEM_COLLAPSED_CLASSES =
  'grid-cols-[1fr] justify-self-center above-phone:h-[2.45rem] above-phone:min-h-[2.45rem]';

const LABEL_CLASSES =
  'text-[0.875rem] font-medium tracking-[0.01em] whitespace-nowrap starting:opacity-0 ' +
  '[transition:opacity_var(--motion-duration-medium)_var(--motion-ease-standard),max-width_var(--motion-duration-long)_var(--motion-ease-standard)]';

// display:none, not visibility:hidden, which would keep a grid row and shift
// the icon. Width and overflow persist on phones, where labels show.
const LABEL_COLLAPSED_CLASSES =
  'hidden invisible w-0 max-w-0 overflow-hidden opacity-0 ' +
  'phone:block phone:max-w-none phone:opacity-100 phone:[visibility:inherit]';

const ICON_CLASSES =
  'grid size-[1.5rem] min-h-[1.5rem] min-w-[1.5rem] place-items-center justify-self-center text-[1.25rem] leading-none';

interface NavItemStyle {
  icon: IconName;
  label: string;
  // An open rail (and the phone drawer) shows labels; a collapsed rail shows
  // only the round icon with the label as its tooltip.
  open: boolean;
  layoutClassName?: string;
}

function itemClasses({open, layoutClassName}: NavItemStyle): string {
  return tooltipClassNames({
    className: joinClasses(
      ITEM_CLASSES,
      open ? ITEM_OPEN_CLASSES : ITEM_COLLAPSED_CLASSES,
      layoutClassName,
    ),
    placement: 'right',
  });
}

function NavItemContent({icon, label, open}: NavItemStyle) {
  return (
    <>
      <Icon className={ICON_CLASSES} name={icon} />
      <span
        className={joinClasses(LABEL_CLASSES, !open && LABEL_COLLAPSED_CLASSES)}
      >
        {label}
      </span>
    </>
  );
}

// A rail destination that acts in place (toggle the rail, open a menu).
export function NavItemButton({
  expanded,
  controls,
  onClick,
  ...style
}: NavItemStyle & {
  expanded?: boolean;
  controls?: string;
  onClick: MouseEventHandler<HTMLButtonElement>;
}) {
  return (
    <button
      type="button"
      className={itemClasses(style)}
      aria-label={style.label}
      data-tooltip={style.label}
      aria-expanded={expanded}
      aria-controls={controls}
      onClick={onClick}
    >
      <NavItemContent {...style} />
    </button>
  );
}

// A rail destination that navigates.
export function NavItemLink({
  icon,
  label,
  open,
  layoutClassName,
  ...rest
}: NavItemStyle & Omit<LinkProps, 'className' | 'children'>) {
  const style = {icon, label, open, layoutClassName};
  return (
    <Link
      {...rest}
      className={itemClasses(style)}
      aria-label={label}
      data-tooltip={label}
    >
      <NavItemContent {...style} />
    </Link>
  );
}
