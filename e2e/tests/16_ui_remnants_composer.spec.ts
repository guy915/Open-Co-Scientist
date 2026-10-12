import {type Page} from '@playwright/test';
import {captureViewport, expect, test} from '../support/fixtures';

const PHONE = {width: 390, height: 844};
const DESKTOP = {width: 1440, height: 900};

const QUESTION = 'Which mechanism should the research prioritize first?';

// A typical guided question: short and long labels, one-line descriptions.
function questionChat(id: string) {
  return {
    id,
    client_id: 'e2e-client',
    status: 'active',
    fields: {
      research_challenge: 'Find repurposable drugs for glioblastoma',
      focus_area: [],
      preferences: [],
      title: null,
    },
    current_question: QUESTION,
    documents: [],
    created_at: 1,
    updated_at: 2,
    completed_at: null,
    turns: [
      {
        id: 1,
        role: 'user',
        content: 'Find repurposable drugs for glioblastoma',
        reasoning: null,
        fallback: false,
        questions: [],
        created_at: 1,
      },
      {
        id: 2,
        role: 'agent',
        content: 'Which mechanism should come first?',
        reasoning: 'Ask which mechanism to prioritise.',
        fallback: false,
        created_at: 2,
        questions: [
          {
            header: 'Mechanism',
            question: QUESTION,
            multi_select: false,
            options: [
              {
                label: 'DNA damage response / repair',
                description:
                  'Compounds that sensitise tumour cells to temozolomide.',
              },
              {
                label: 'Metabolism',
                description: 'Drugs that starve glioma cells of glucose.',
              },
              {
                label: 'Immune modulation',
                description: 'Agents that reawaken the immune response.',
              },
              {
                label: 'Open to any mechanism',
                description: 'Compare mechanisms on evidence.',
              },
            ],
          },
        ],
      },
    ],
  };
}

async function answerUntilPlan(page: Page): Promise<void> {
  const start = page.getByRole('button', {name: 'Start research'});
  const turns = page.getByRole('button', {name: 'Copy response'});
  for (const answer of ['Lipid repair.', 'Organoids.', 'Patients.', 'None.']) {
    if (await start.isVisible().catch(() => false)) return;
    const prior = await turns.count();
    await page.getByRole('textbox').last().fill(answer);
    await page.getByRole('button', {name: 'Send', exact: true}).click();
    await expect(async () => {
      const ready = await start.isVisible().catch(() => false);
      expect(ready || (await turns.count()) > prior).toBeTruthy();
    }).toPass({timeout: 30_000});
  }
  await expect(start).toBeVisible();
}

for (const theme of ['light', 'dark']) {
  test.describe(`composer and question card in ${theme}`, () => {
    test.beforeEach(async ({page}) => {
      await page.addInitScript(
        mode => localStorage.setItem('cosci-theme', mode),
        theme,
      );
    });

    for (const [name, viewport] of [
      ['phone', PHONE],
      ['desktop', DESKTOP],
    ] as const) {
      test(`a typical question fits its card at ${name} width`, async ({
        page,
      }) => {
        await page.setViewportSize(viewport);
        await page.route('**/api/interviews/typical-question', route =>
          route.fulfill({json: questionChat('typical-question')}),
        );
        await page.goto('/chats/typical-question');
        const panel = page.getByRole('region', {name: 'Answer options'});
        await expect(panel).toBeVisible();

        // A scrollbar shows only on a scrollable box that overflows.
        const overflow = await panel.evaluate(section =>
          [section, ...Array.from(section.querySelectorAll('*'))].map(el => {
            const style = getComputedStyle(el);
            const scrolls = (value: string) =>
              value === 'auto' || value === 'scroll';
            return {
              horizontal:
                scrolls(style.overflowX) && el.scrollWidth > el.clientWidth + 1,
              vertical:
                scrolls(style.overflowY) &&
                el.scrollHeight > el.clientHeight + 1,
            };
          }),
        );
        expect(overflow.some(box => box.horizontal)).toBe(false);
        if (name === 'desktop') {
          expect(overflow.some(box => box.vertical)).toBe(false);
        }

        for (const row of await panel.locator('label').all()) {
          const [labelBox, descriptionBox] = await Promise.all([
            row.locator('strong').boundingBox(),
            row.locator('small').boundingBox(),
          ]);
          expect(descriptionBox!.y).toBeGreaterThanOrEqual(
            labelBox!.y + labelBox!.height - 1,
          );
        }

        const field = panel.getByPlaceholder('Type your own answer');
        const send = panel.getByRole('button', {name: 'Send answer'});
        await expect(send).toBeDisabled();
        // One send answers every question, so it sits in the head beside
        // minimize, never inside the free-text field.
        const minimize = panel.getByRole('button', {name: 'Minimize the questions'});
        const [fieldBox, sendBox, minimizeBox] = await Promise.all([
          field.boundingBox(),
          send.boundingBox(),
          minimize.boundingBox(),
        ]);
        expect(sendBox!.y + sendBox!.height).toBeLessThanOrEqual(fieldBox!.y);
        expect(
          Math.abs(
            sendBox!.y + sendBox!.height / 2 - (minimizeBox!.y + minimizeBox!.height / 2),
          ),
        ).toBeLessThanOrEqual(1);
        expect(sendBox!.x + sendBox!.width).toBeLessThanOrEqual(minimizeBox!.x);
        await panel.getByText('Metabolism', {exact: true}).click();
        await expect(send).toBeEnabled();
        await captureViewport(page, {
          ...viewport,
          name: `ui-remnants-question-${name}-${theme}.png`,
        });
      });
    }

    test('the composer placeholder follows the conversation and the jump button keeps its gap', async ({
      page,
    }) => {
      await page.setViewportSize(DESKTOP);
      await page.goto('/');
      const composer = page.getByRole('textbox').last();
      await expect(composer).toHaveAttribute(
        'placeholder',
        'Start a new research goal to begin',
      );
      await composer.fill('Which checkpoints govern ferroptosis escape?');
      await composer.press('Enter');
      await expect(
        page.getByRole('button', {name: 'Copy response'}).first(),
      ).toBeVisible({timeout: 30_000});
      await expect(page.getByRole('textbox').last()).toHaveAttribute(
        'placeholder',
        'Ask Co-Scientist',
      );
      await answerUntilPlan(page);
      await expect(page.getByRole('textbox').last()).toHaveAttribute(
        'placeholder',
        'Ask Co-Scientist',
      );
      await page.getByRole('button', {name: 'Start research'}).click();
      await expect(
        page.getByRole('region', {name: 'Started research session'}),
      ).toBeVisible({timeout: 30_000});
      await expect(page.getByRole('textbox').last()).toHaveAttribute(
        'placeholder',
        'Ask Co-Scientist',
      );
      await page.reload();
      await expect(
        page.getByRole('region', {name: 'Started research session'}),
      ).toBeVisible({timeout: 30_000});
      await expect(page.getByRole('textbox').last()).toHaveAttribute(
        'placeholder',
        'Ask Co-Scientist',
      );

      await page
        .locator('.reference-chat-timeline')
        .evaluate(el => el.scrollTo({top: 0}));
      const jump = page.getByRole('button', {name: 'Jump to latest message'});
      await expect(jump).toBeVisible();
      // One spacing step (mb-3) between the button and the composer's top,
      // measured once the button's enter motion has settled.
      await expect(async () => {
        const [jumpBox, composerBox] = await Promise.all([
          jump.boundingBox(),
          page.locator('form.reference-composer').last().boundingBox(),
        ]);
        const gap = composerBox!.y - (jumpBox!.y + jumpBox!.height);
        expect(gap).toBeGreaterThanOrEqual(8);
        expect(gap).toBeLessThanOrEqual(16);
      }).toPass();
      await captureViewport(page, {
        ...DESKTOP,
        name: `ui-remnants-jump-${theme}.png`,
      });
    });
  });
}
