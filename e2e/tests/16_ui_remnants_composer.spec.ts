import {type Page} from '@playwright/test';
import {captureViewport, expect, test} from '../support/fixtures';

const PHONE = {width: 390, height: 844};
const SHORT_PHONE = {width: 375, height: 667};
const DESKTOP = {width: 1440, height: 900};
const MAC = {width: 1512, height: 982};
const EXTERNAL = {width: 1920, height: 1080};

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

function tallQuestionChat(id: string) {
  const chat = questionChat(id);
  return {
    ...chat,
    turns: chat.turns.map(turn =>
      turn.id === 2
        ? {
            ...turn,
            content: Array.from(
              {length: 20},
              (_, index) =>
                `Evidence checkpoint ${index + 1}: The comparative review ` +
                'keeps the candidate mechanism connected to its observed ' +
                'cellular response, checks whether the response depends on ' +
                'the treatment context, and records which findings still ' +
                'need an independent replication before they can support ' +
                'a useful research decision.',
            ).join('\n\n'),
          }
        : turn,
    ),
  };
}

async function expectAnswerAboveComposer(
  page: Page,
  stage: string,
): Promise<void> {
  const timeline = page.locator('.reference-chat-timeline');
  const answer = page.locator('.reference-model-bubble').last();
  const responseActions = answer
    .locator('xpath=../..')
    .locator('.reference-message-actions');
  await expect(answer).toContainText('Evidence checkpoint 20');
  let latestGeometry: Record<string, boolean | number | string> | undefined;
  const readGeometry = async () => {
        const [answerBox, actionsBox, layout] = await Promise.all([
          answer.boundingBox(),
          responseActions.boundingBox(),
          timeline.evaluate(el => {
            const composer = el.nextElementSibling?.querySelector<HTMLElement>(
              'form.reference-composer',
            );
            return {
              composerTop: composer?.getBoundingClientRect().top ?? -1,
              scrollTop: el.scrollTop,
              maxScrollTop: el.scrollHeight - el.clientHeight,
              clientHeight: el.clientHeight,
            };
          }),
        ]);
        latestGeometry = {
          proseClear: Boolean(
            answerBox && answerBox.y + answerBox.height <= layout.composerTop + 1,
          ),
          actionsBelow: Boolean(
            actionsBox && actionsBox.y >= layout.composerTop + 0.5,
          ),
          answerBottom: answerBox ? answerBox.y + answerBox.height : -1,
          actionsTop: actionsBox?.y ?? -1,
          ...layout,
        };
        return latestGeometry;
  };
  try {
    await expect
      .poll(readGeometry, {message: `Answer geometry after ${stage}`, timeout: 10_000})
      .toMatchObject({proseClear: true, actionsBelow: true});
  } catch (error) {
    throw new Error(
      `Answer geometry after ${stage}: ${JSON.stringify(latestGeometry)} (${String(error)})`,
    );
  }
}

async function appendLateAnswerText(page: Page): Promise<number> {
  const answer = page.locator('.reference-model-bubble').last();
  return answer.evaluate(element => {
    const timeline = element.closest('.reference-chat-timeline');
    const previousHeight = timeline?.scrollHeight ?? 0;
    for (let index = 1; index <= 4; index += 1) {
      const paragraph = document.createElement('p');
      paragraph.textContent =
        `Late streamed evidence ${index} preserves the full response above the composer while the answer grows. ` +
        'The new paragraph adds enough content to exercise automatic following after the previous scroll position has been clamped.';
      element.append(paragraph);
    }
    return (timeline?.scrollHeight ?? previousHeight) - previousHeight;
  });
}

async function expectActionsManuallyReachable(page: Page): Promise<void> {
  const timeline = page.locator('.reference-chat-timeline');
  const answer = page.locator('.reference-model-bubble').last();
  const responseActions = answer
    .locator('xpath=../..')
    .locator('.reference-message-actions');
  const timelineBox = await timeline.boundingBox();
  expect(timelineBox).not.toBeNull();
  await page.mouse.move(
    timelineBox!.x + timelineBox!.width / 2,
    timelineBox!.y + 48,
  );
  await page.mouse.wheel(0, 3000);
  let latestGeometry: Record<string, number | boolean> | undefined;
  const readGeometry = async () => {
    const [actionsBox, layout] = await Promise.all([
      responseActions.boundingBox(),
      timeline.evaluate(el => {
        const composer = el.nextElementSibling?.querySelector<HTMLElement>(
          'form.reference-composer',
        );
        const outerComposer = el.nextElementSibling as HTMLElement | null;
        const scrollerBox = el.getBoundingClientRect();
        const parentBox = el.parentElement?.getBoundingClientRect();
        return {
          composerTop: composer?.getBoundingClientRect().top ?? -1,
          outerComposerTop: outerComposer?.getBoundingClientRect().top ?? -1,
          outerComposerHeight: outerComposer?.getBoundingClientRect().height ?? -1,
          scrollerBottom: scrollerBox.bottom,
          parentBottom: parentBox?.bottom ?? -1,
          viewportHeight: window.innerHeight,
          offsetHeight: (el as HTMLElement).offsetHeight,
          paddingBottom: Number.parseFloat(getComputedStyle(el).paddingBottom),
          scrollTop: el.scrollTop,
          maxScrollTop: el.scrollHeight - el.clientHeight,
          clientHeight: el.clientHeight,
        };
      }),
    ]);
    latestGeometry = {
      actionsClear: Boolean(
        actionsBox && actionsBox.y + actionsBox.height <= layout.composerTop + 1,
      ),
      actionsTop: actionsBox?.y ?? -1,
      actionsHeight: actionsBox?.height ?? -1,
      actionsBottom: actionsBox ? actionsBox.y + actionsBox.height : -1,
      ...layout,
    };
    return latestGeometry;
  };
  try {
    await expect
      .poll(readGeometry, {timeout: 10_000})
      .toMatchObject({actionsClear: true});
  } catch (error) {
    throw new Error(
      `Manual action geometry: ${JSON.stringify(latestGeometry)} (${String(error)})`,
    );
  }
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
      ['short phone', SHORT_PHONE],
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
        const panelBox = await panel.boundingBox();
        const maxPanelHeight =
          name === 'desktop'
            ? 450
            : name === 'phone'
              ? 390
              : SHORT_PHONE.height * 0.56;
        expect(panelBox).not.toBeNull();
        expect(panelBox!.height).toBeLessThanOrEqual(maxPanelHeight);

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
          const [labelBox, descriptionBox, rowBox] = await Promise.all([
            row.locator('strong').boundingBox(),
            row.locator('small').boundingBox(),
            row.boundingBox(),
          ]);
          expect(descriptionBox).not.toBeNull();
          expect(labelBox).not.toBeNull();
          expect(rowBox).not.toBeNull();
          expect(rowBox!.height).toBeGreaterThanOrEqual(44);
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
        await panel
          .getByText(
            name === 'short phone' ? 'Open to any mechanism' : 'Metabolism',
            {exact: true},
          )
          .click();
        await expect(send).toBeEnabled();
        await captureViewport(page, {
          ...viewport,
          name: `ui-remnants-question-${name}-${theme}.png`,
        });
      });
    }

    for (const [name, viewport] of [
      ['Mac', MAC],
      ['external display', EXTERNAL],
      ['phone', PHONE],
      ['short phone', SHORT_PHONE],
    ] as const) {
      test(`keeps a long answer above the growing composer at ${name} size`, async ({
        page,
      }) => {
        await page.setViewportSize(viewport);
        await page.route('**/api/interviews/covered-answer', route =>
          route.fulfill({json: tallQuestionChat('covered-answer')}),
        );
        await page.goto('/chats/covered-answer');
        const panel = page.getByRole('region', {name: 'Answer options'});
        const jumpButton = page.getByRole('button', {
          name: 'Jump to latest message',
        });
        await expect(panel).toBeVisible();
        await expectAnswerAboveComposer(page, 'initial render');
        await expect(jumpButton).toBeHidden();
        if (name === 'Mac') {
          await captureViewport(page, {
            ...viewport,
            name: `question-prose-desktop-${theme}.png`,
          });
        }
        if (name === 'phone') {
          await captureViewport(page, {
            ...viewport,
            name: `question-prose-phone-${theme}.png`,
          });
        }
        if (name === 'Mac') {
          const streamedGrowth = await appendLateAnswerText(page);
          expect(streamedGrowth).toBeGreaterThan(64);
          await expectAnswerAboveComposer(page, 'late streamed content');
          await page.setViewportSize({
            width: viewport.width,
            height: viewport.height - 100,
          });
          await expectAnswerAboveComposer(page, 'viewport shrink');
          await page.setViewportSize(viewport);
          await expectAnswerAboveComposer(page, 'viewport restore');
        }
        await panel
          .getByRole('button', {name: 'Minimize the questions'})
          .click();
        await panel.getByRole('button', {name: 'Show the questions'}).click();
        await expectAnswerAboveComposer(page, 'question panel expanded');
        await expectActionsManuallyReachable(page);

        const timeline = page.locator('.reference-chat-timeline');
        const timelineBox = await timeline.boundingBox();
        expect(timelineBox).not.toBeNull();
        await page.mouse.move(
          timelineBox!.x + timelineBox!.width / 2,
          timelineBox!.y + 48,
        );
        await page.mouse.wheel(0, -10_000);
        await expect(async () => {
          expect(await timeline.evaluate(el => el.scrollTop)).toBeLessThan(200);
        }).toPass({timeout: 10_000});
        await expect(jumpButton).toBeVisible();
        if (name === 'Mac') {
          const readerGrowth = await appendLateAnswerText(page);
          expect(readerGrowth).toBeGreaterThan(64);
          await expect(async () => {
            const position = await timeline.evaluate(el => ({
              scrollTop: el.scrollTop,
              maxScrollTop: el.scrollHeight - el.clientHeight,
            }));
            expect(position.scrollTop).toBeLessThan(200);
            expect(position.maxScrollTop - position.scrollTop).toBeGreaterThan(
              64,
            );
          }).toPass({timeout: 10_000});
        }
        await panel
          .getByRole('button', {name: 'Minimize the questions'})
          .click();
        await panel.getByRole('button', {name: 'Show the questions'}).click();
        await expect(async () => {
          const position = await timeline.evaluate(el => ({
            scrollTop: el.scrollTop,
            maxScrollTop: el.scrollHeight - el.clientHeight,
          }));
          expect(position.scrollTop).toBeLessThan(200);
          expect(position.maxScrollTop - position.scrollTop).toBeGreaterThan(
            64,
          );
        }).toPass({timeout: 10_000});
        await expect(jumpButton).toBeVisible();
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
