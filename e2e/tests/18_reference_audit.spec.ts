import {createCompletedRun, expect, test} from '../support/fixtures';

test.describe('reference audit', () => {
  test('completed research keeps its decisions and match history inspectable', async ({
    page,
    api,
  }) => {
    const id = await createCompletedRun(api, {
      research_goal: 'Offline audit of a public enzyme-stability hypothesis',
      tier: 'standard',
    });

    await page.goto(`/runs/${id}/specifications`);
    await expect(
      page.getByRole('heading', {name: /run specifications/i}),
    ).toBeVisible();

    await page.getByRole('link', {name: 'All Ideas', exact: true}).click();
    await expect(
      page.getByRole('heading', {name: 'Match history'}),
    ).toBeVisible();
    await expect(
      page.getByText('Elo change:', {exact: true}).first(),
    ).toBeVisible();
  });
});
