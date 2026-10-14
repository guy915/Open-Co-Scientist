import {captureViewport, expect, test} from '../support/fixtures';

for (const theme of ['light', 'dark']) {
  test(`long interview question stays reachable on a phone in ${theme}`, async ({page}) => {
    const viewport = {width: 375, height: 812};
    await page.setViewportSize(viewport);
    await page.addInitScript(mode => localStorage.setItem('cosci-theme', mode), theme);
    const question = 'Which mechanism should the research investigate, and how should the evidence distinguish an immune relay from a direct effect?';
    await page.route('**/api/interviews/mobile-question', route => route.fulfill({json: {
      id: 'mobile-question', client_id: 'e2e-client', status: 'active',
      fields: {research_challenge: 'Investigate immune mechanisms', focus_area: [], preferences: [], title: null},
      current_question: question, documents: [], created_at: 1, updated_at: 2, completed_at: null,
      turns: [{id: 1, role: 'agent', content: question, created_at: 2, questions: [{
        header: 'Mechanism', question, multi_select: false,
        options: ['Immune relay', 'Open to mechanisms', 'Direct effect'].map(label => ({label,
          description: 'Compare mechanisms across tissues and time points, including human evidence, immune cell interactions, relevant controls, and alternative causal explanations. '.repeat(3)})),
      }]}],
    }}));
    await page.goto('/chats/mobile-question');
    const panel = page.getByRole('region', {name: 'Answer options'});
    await expect(panel).toBeVisible();
    const bounds = await panel.boundingBox();
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.height).toBeLessThan(viewport.height * 0.6);
    await expect(panel.locator('legend')).toBeInViewport();
    await panel.getByPlaceholder('Type your own answer').scrollIntoViewIfNeeded();
    await expect(panel.getByPlaceholder('Type your own answer')).toBeInViewport();
    await expect(panel.getByRole('button', {name: 'Send answer'})).toBeInViewport();
    await expect(page.locator('textarea').last()).toBeInViewport();
    await captureViewport(page, {...viewport, name: `mobile-question-${theme}.png`});
  });
}

for (const theme of ['light', 'dark']) {
  test(`keyless plan keeps tier descriptions and disables paid tiers in ${theme}`, async ({page}) => {
    await page.setViewportSize({width: 1440, height: 1050});
    await page.addInitScript(mode => localStorage.setItem('cosci-theme', mode), theme);
    await page.route('**/api/interviews/plan-card', route => route.fulfill({json: {
      id: 'plan-card', client_id: 'e2e-client', status: 'completed',
      fields: {research_challenge: 'Investigate immune mechanisms', focus_area: ['Immune relay'], preferences: ['Human evidence'], title: 'Derived title'},
      current_question: null, documents: [], created_at: 1, updated_at: 2, completed_at: 2,
      turns: [{id: 1, role: 'agent', content: 'The research plan is ready.', created_at: 2}],
    }}));
    await page.goto('/chats/plan-card');
    await expect(page.getByRole('radio', {name: /Express/})).toBeEnabled();
    for (const label of ['Standard', 'Extended', 'Ultra']) {
      const radio = page.getByRole('radio', {name: new RegExp(label)});
      await expect(radio).toBeDisabled();
      await expect(radio.locator('..')).toHaveCSS('opacity', '0.5');
    }
    await expect(page.getByText('Suitable for medium-sized research questions and experiments.')).toBeVisible();
    await expect(page.getByText('Title:', {exact: true})).toHaveCount(0);
    await page.getByRole('button', {name: 'Edit plan', exact: true}).click();
    await expect(page.getByLabel('Title', {exact: true})).toHaveCount(0);
    await page.getByRole('button', {name: 'Cancel', exact: true}).first().click();
    await page.getByRole('radio', {name: /Standard/}).scrollIntoViewIfNeeded();
    await captureViewport(page, {width: 1440, height: 1050, name: `plan-card-${theme}.png`});
  });
}

for (const theme of ['light', 'dark']) {
  for (const viewport of [{width: 1440, height: 900}, {width: 390, height: 844}]) {
    test(`math and compact interview options at ${viewport.width} in ${theme}`, async ({page}) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(mode => localStorage.setItem('cosci-theme', mode), theme);
      let showMath = false;
      const mathRequests: string[] = [];
      page.on('request', request => {
        if (request.url().includes('markdown_math')) mathRequests.push(request.url());
      });
      await page.route('**/api/interviews/rendering-check', route => route.fulfill({json: {
        id: 'rendering-check', client_id: 'e2e-client', status: 'active',
        fields: {research_challenge: 'Study water transport', focus_area: [], preferences: [], title: null},
        current_question: 'Which water transport model?', documents: [], created_at: 1, updated_at: 2, completed_at: null,
        turns: [{id: 1, role: 'agent', content: showMath ? 'Compare H<sub>2</sub>O uptake and \\(x_{1}^2\\).\n\n\\[\nx^2\n\ny^2\n\\]' : 'Compare H<sub>2</sub>O uptake. Code stays literal: `\\(x_{1}\\)`. ', created_at: 2, questions: [{
          header: 'Model', question: 'Which **H<sub>2</sub>O** model should we use?', multi_select: false,
          options: [
            {label: 'Root cells', description: showMath ? 'Measure $x_{1}$ in isolated cells.' : 'Measure uptake in isolated cells.'},
            {label: 'Whole plant', description: 'Compare water uptake across the plant.'},
          ],
        }]}],
      }}));
      await page.goto('/chats/rendering-check');
      const panel = page.getByRole('region', {name: 'Answer options'});
      await expect(panel.locator('sub').first()).toHaveText('2');
      expect(mathRequests).toEqual([]);
      showMath = true;
      await page.reload();
      await expect(panel).toBeVisible();
      await expect(panel.locator('sub').first()).toHaveText('2');
      await expect(panel.locator('.katex-html')).toHaveCount(1);
      const options = panel.locator('label').filter({has: page.getByRole('radio')});
      const first = await options.nth(0).boundingBox();
      const second = await options.nth(1).boundingBox();
      expect(Math.abs(second!.y - (first!.y + first!.height))).toBeLessThanOrEqual(1);
      expect(first!.height).toBeGreaterThanOrEqual(48);
      await expect(page.locator('.katex-display')).toHaveCount(1);
      await expect(page.locator('.katex-error')).toHaveCount(0);
      expect(mathRequests.length).toBeGreaterThan(0);
      await captureViewport(page, {...viewport, name: `interview-rendering-${viewport.width}-${theme}.png`});
    });
  }
}
