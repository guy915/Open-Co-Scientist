import type {ButtonHTMLAttributes, HTMLAttributes, ReactNode} from 'react';
import {joinClasses} from './cx';

type CardTone = 'neutral' | 'raised' | 'warning' | 'danger';
type CardSize = 'block' | 'tile' | 'panel';

// Cards are tonal layers, never shadows (elevation is for overlays). A block
// is a notice, a tile a stat or summary, and a panel groups controls.
const TONE_CLASSES: Record<CardTone, string> = {
  neutral: 'bg-cosci-panel text-cosci-fg',
  raised: 'bg-cosci-settings-card-bg text-cosci-fg',
  warning: 'bg-th-warning-container text-th-on-warning-container',
  danger:
    'border border-cosci-danger-border bg-cosci-danger-bg text-cosci-danger-fg',
};

const SIZE_CLASSES: Record<CardSize, string> = {
  block: 'rounded-md px-4 py-3',
  tile: 'rounded-md p-4',
  panel: 'rounded-2xl px-6 pt-5 pb-6',
};

export interface CardStyle {
  tone?: CardTone;
  size?: CardSize;
  outlined?: boolean;
}

export function cardClasses({
  tone = 'neutral',
  size = 'block',
  outlined = false,
}: CardStyle = {}): string {
  return joinClasses(
    TONE_CLASSES[tone],
    SIZE_CLASSES[size],
    outlined && tone !== 'danger' && 'border border-cosci-border',
  );
}

export function Card({
  tone,
  size,
  outlined,
  as: Tag = 'div',
  layoutClassName,
  children,
  ...rest
}: CardStyle &
  Omit<HTMLAttributes<HTMLElement>, 'className'> & {
    as?: 'div' | 'section' | 'article' | 'aside';
    layoutClassName?: string;
    children: ReactNode;
  }) {
  return (
    <Tag
      className={joinClasses(
        cardClasses({tone, size, outlined}),
        layoutClassName,
      )}
      {...rest}
    >
      {children}
    </Tag>
  );
}

// The phone layout turns the card into a borderless pill row, so the
// highlighted row must also win over the resting transparent background.
const CARD_BUTTON_CLASSES =
  'cursor-pointer rounded-2xl border border-cosci-border text-left text-cosci-fg [outline:0] ' +
  'transition-[background-color,border-color] duration-short ease-standard ' +
  'hover:bg-cosci-hover focus-visible:bg-cosci-hover focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-th-ring ' +
  'phone:rounded-full phone:[border:0] phone:hover:[border-color:transparent] phone:focus-visible:[border-color:transparent]';

const CARD_BUTTON_REST_CLASSES = 'bg-cosci-suggestion-bg phone:bg-transparent';

const CARD_BUTTON_HIGHLIGHTED_CLASSES =
  'bg-cosci-hover phone:[&&]:[border-color:transparent]';

// A card that is one action as a whole (home suggestions); a list row on
// phones. `highlighted` holds the hover look while its preview shows.
export function CardButton({
  highlighted = false,
  layoutClassName,
  children,
  ...rest
}: Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className' | 'type'> & {
  highlighted?: boolean;
  layoutClassName?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className={joinClasses(
        CARD_BUTTON_CLASSES,
        highlighted
          ? CARD_BUTTON_HIGHLIGHTED_CLASSES
          : CARD_BUTTON_REST_CLASSES,
        layoutClassName,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
