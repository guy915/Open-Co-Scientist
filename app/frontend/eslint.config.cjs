/**
 * Copyright 2026 The Co-Scientist Authors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
// GTS registers the TypeScript plugin and parser. Reuse those when layering
// strict rules: dependency layouts may resolve a second plugin instance.
const tseslint = require('typescript-eslint');
const {defineConfig} = require('eslint/config');
const strictRules = tseslint.configs.strict.filter(
  config => config.name !== 'typescript-eslint/base',
);
const stylisticRules = tseslint.configs.stylistic.filter(
  config => config.name !== 'typescript-eslint/base',
);

const HEX_COLOR = String.raw`/#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b/`;
const FUNCTION_COLOR = String.raw`/\b(rgba?|hsla?)\(/`;
const ARBITRARY_RADIUS = String.raw`/rounded(-[a-z]{1,2})?-\[|border-radius:/`;
const ARBITRARY_SPACING = String.raw`/(^|[\s:])-?(p[xytrblse]?|m[xytrblse]?|gap(-[xy])?|space-[xy])-\[[0-9.]+(rem|px)\]/`;
const ARBITRARY_TOKEN = String.raw`/\b[a-z]+-\(--cosci-/`;
const DESTRUCTIVE_RED = String.raw`/\bth-destructive\b/`;
const ARBITRARY_Z = String.raw`/(^|[\s:])-?z-\[/`;
const ARBITRARY_HOVER = String.raw`/\[&:hover\]/`;
const PHONE_BREAKPOINT = String.raw`/(max|min)-\[70[01]px\]|(max|min)-width:70[01]px/`;

function uiBuildingBlockRules() {
  const inText = (pattern, message) => [
    {selector: `Literal[value=${pattern}]`, message},
    {selector: `TemplateElement[value.raw=${pattern}]`, message},
  ];
  return [
    {
      selector: "JSXOpeningElement[name.name='button']",
      message:
        'Use Button, IconButton or another control from @/shared/ui instead of a raw <button>.',
    },
    ...inText(
      HEX_COLOR,
      'Use a theme token instead of a hex colour (styles/tokens.css).',
    ),
    ...inText(
      FUNCTION_COLOR,
      'Use a theme token instead of an rgb()/hsl() colour (styles/tokens.css).',
    ),
    ...inText(
      ARBITRARY_RADIUS,
      'Use a radius from the scale or a named radius token instead of an arbitrary radius.',
    ),
    ...inText(
      ARBITRARY_SPACING,
      'Use the spacing scale (p-4, gap-2.5, mt-px) instead of an arbitrary padding, margin or gap.',
    ),
    ...inText(
      ARBITRARY_TOKEN,
      'Use the named utility (bg-cosci-*, text-cosci-*; bridged in index.css) instead of an arbitrary token reference.',
    ),
    ...inText(
      DESTRUCTIVE_RED,
      'Use StatusText or ErrorNotice (@/shared/ui), whose red is cosci-danger, instead of th-destructive.',
    ),
    ...inText(
      ARBITRARY_Z,
      'Use a named layer (z-header, z-rail, z-dialog, z-toast; index.css) or a small integer inside one component instead of an arbitrary z-index.',
    ),
    ...inText(
      ARBITRARY_HOVER,
      'Use hover:, which applies only to pointers that can hover, instead of [&:hover]: (sticky after a tap).',
    ),
    ...inText(
      PHONE_BREAKPOINT,
      'Use the phone: or above-phone: variant (index.css) instead of writing the breakpoint.',
    ),
    {
      selector:
        "JSXOpeningElement[name.name=/^(?!iframe$)[a-z]/] > JSXAttribute[name.name='title']",
      message:
        'Use a tooltip from @/shared/ui instead of a native title (no keyboard focus, OS delay).',
    },
    {
      selector: "Property[key.name='borderRadius']",
      message:
        'Use a radius from the scale or a named radius token instead of an inline radius.',
    },
  ];
}

const FEATURES = ['access', 'chat', 'diagnostics', 'report', 'runs'];

module.exports = defineConfig([
  {
    ignores: [
      '**/node_modules/**',
      '**/dist/**',
      'scripts/**',
      'eslint.config.cjs',
      '.prettierrc.cjs',
      'vite.config.ts',
    ],
  },
  ...require('gts'),
  // Size ceilings force unrelated helpers and facades out of cohesive modules.
  {
    files: ['src/**/*.ts', 'src/**/*.tsx'],
    extends: [...strictRules, ...stylisticRules],
  },
  // Shape, colour and focus belong to src/shared/ui (app/AGENTS.md, "UI
  // building blocks"). Screens compose those components and theme tokens.
  {
    files: ['src/**/*.ts', 'src/**/*.tsx'],
    ignores: [
      'src/shared/ui/**',
      'src/**/*.test.ts',
      'src/**/*.test.tsx',
    ],
    rules: {
      'no-restricted-syntax': ['error', ...uiBuildingBlockRules()],
    },
  },
  // Phase 7 boundaries: a feature imports only shared/ and its own files, and
  // shared/ imports no feature or the app shell.
  ...FEATURES.map(feature => ({
    files: [`src/features/${feature}/**/*.{ts,tsx}`],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              regex: `^@/(app|features/(?!${feature}/))`,
              message: `features/${feature} may import only @/shared and its own files.`,
            },
            {
              regex: '^\\.\\./',
              message: 'Reach outside this feature through @/shared only.',
            },
          ],
        },
      ],
    },
  })),
  {
    files: ['src/shared/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              regex: '^@/(app|features)/',
              message: 'shared/ must not depend on a feature or the app shell.',
            },
          ],
        },
      ],
    },
  },
  // Test null assertions fail clearly; empty functions are intentional mocks.
  // Configure these once rather than requiring disables at every test call.
  {
    files: ['src/**/*.test.ts', 'src/**/*.test.tsx', 'src/test_setup.ts'],
    rules: {
      '@typescript-eslint/no-non-null-assertion': 'off',
      '@typescript-eslint/no-empty-function': 'off',
    },
  },
]);
