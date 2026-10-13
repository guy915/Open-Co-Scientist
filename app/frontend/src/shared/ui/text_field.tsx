import type {InputHTMLAttributes, Ref, TextareaHTMLAttributes} from 'react';
import {joinClasses} from './cx';

export type FieldVariant = 'outlined' | 'bare';

// `bare` fields sit inside a surface that already draws the box (the
// composer, the bubble editor); that surface owns their focus treatment.
const VARIANT_CLASSES: Record<FieldVariant, string> = {
  outlined:
    'rounded-xl border border-field-border bg-transparent px-3.5 ' +
    'py-2.5 text-[0.9rem] text-cosci-fg focus-visible:border-field-focus ' +
    'focus-visible:outline-1 focus-visible:outline-offset-0 ' +
    'focus-visible:outline-field-focus disabled:cursor-default disabled:opacity-60',
  bare: 'border-0 bg-transparent p-0 text-cosci-fg outline-none',
};

export function fieldClasses(variant: FieldVariant = 'outlined'): string {
  return joinClasses(
    'w-full font-[inherit] placeholder:text-field-placeholder',
    VARIANT_CLASSES[variant],
  );
}

interface Shared {
  variant?: FieldVariant;
  layoutClassName?: string;
}

export function TextField({
  variant,
  layoutClassName,
  ...rest
}: Shared &
  Omit<InputHTMLAttributes<HTMLInputElement>, 'className'> & {
    ref?: Ref<HTMLInputElement>;
  }) {
  return (
    <input
      className={joinClasses(fieldClasses(variant), layoutClassName)}
      {...rest}
    />
  );
}

export function TextArea({
  variant,
  layoutClassName,
  ...rest
}: Shared &
  Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'className'> & {
    ref?: Ref<HTMLTextAreaElement>;
  }) {
  return (
    <textarea
      className={joinClasses(fieldClasses(variant), layoutClassName)}
      {...rest}
    />
  );
}
