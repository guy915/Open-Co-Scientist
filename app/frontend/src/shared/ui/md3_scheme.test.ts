import {
  argbFromHex,
  hexFromArgb,
  themeFromSourceColor,
} from '@material/material-color-utilities';
import {describe, expect, it} from 'vitest';
import {MD3_SCHEMES} from './md3_scheme';

const MD3_SEED = '#1A6B6B';

function roles(scheme: {toJSON(): Record<string, number>}) {
  return Object.fromEntries(
    Object.entries(scheme.toJSON()).map(([key, value]) => [
      `--md-sys-color-${key.replace(/([a-z])([A-Z])/g, '$1-$2').toLowerCase()}`,
      hexFromArgb(value),
    ]),
  );
}

describe('MD3_SCHEMES', () => {
  it('matches the schemes the library derives from the seed', () => {
    const theme = themeFromSourceColor(argbFromHex(MD3_SEED));
    expect(MD3_SCHEMES.light).toEqual(roles(theme.schemes.light));
    expect(MD3_SCHEMES.dark).toEqual(roles(theme.schemes.dark));
  });
});
