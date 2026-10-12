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
