import {captureViewport, expect, test} from './fixtures';
import {API_URL} from './paths';

export function customModelCases(themes: readonly ('light' | 'dark')[]): void {
  for (const theme of themes) {
    for (const width of [1440, 390]) {
      test(`Other models validate, persist, and reach the run at ${width}px in ${theme}`, async ({
        page,
      }) => {
        await page.setViewportSize({width, height: 900});
        await page.addInitScript(mode => {
          localStorage.setItem('cosci-theme', mode);
          localStorage.setItem('cosci-api-provider', 'openrouter');
        }, theme);
        await page.goto('/');
        async function openModelSettings() {
          if (width <= 700)
            await page.getByRole('button', {name: 'Open navigation'}).click();
          await page
            .getByRole('button', {name: 'Settings', exact: true})
            .click();
          await page
            .getByRole('menuitem', {name: 'Model', exact: true})
            .click();
        }
        await openModelSettings();
        const dialog = page.getByRole('dialog', {
          name: 'Settings',
          exact: true,
        });
        const keyField = dialog.getByLabel('OpenRouter API key', {exact: true});
        await keyField.fill('synthetic-e2e-custom-key');
        await keyField.press('Tab');
        for (const tier of ['worker', 'supervisor']) {
          await dialog
            .getByRole('button', {name: new RegExp(`${tier} model`, 'i')})
            .click();
          await dialog
            .getByRole('menuitemradio', {name: 'Other', exact: true})
            .click();
          const input = dialog.getByLabel(
            new RegExp(`Custom ${tier} model ID`),
          );
          await expect(input).toHaveAttribute(
            'placeholder',
            'provider/model-name',
          );
          await input.fill('test/missing');
          await input.press('Enter');
          await expect(
            dialog.getByText('Model not found for this provider', {
              exact: true,
            }),
          ).toBeVisible();
          await input.fill(`test/custom-${tier}`);
          await input.press('Tab');
          await expect(input).toHaveValue(`openrouter/test/custom-${tier}`);
        }
        await expect(
          dialog.getByText('Model checked', {exact: true}),
        ).toHaveCount(2);
        expect(
          await dialog.evaluate(node => node.scrollWidth <= node.clientWidth),
        ).toBe(true);
        await captureViewport(page, {
          width,
          height: 900,
          name: `custom-models-${theme}-${width}.png`,
        });
        await dialog.getByRole('button', {name: 'Close settings'}).click();
        await page.reload();
        await openModelSettings();
        await expect(keyField).toHaveValue('synthetic-e2e-custom-key');
        const anotherTab = await page.context().newPage();
        await anotherTab.goto('/');
        await anotherTab
          .getByRole('button', {name: 'Settings', exact: true})
          .click();
        await anotherTab
          .getByRole('menuitem', {name: 'Model', exact: true})
          .click();
        await expect(
          anotherTab.getByLabel('OpenRouter API key', {exact: true}),
        ).toHaveValue('synthetic-e2e-custom-key');
        await anotherTab.close();
        await expect(dialog.getByLabel(/Custom worker model ID/)).toHaveValue(
          'openrouter/test/custom-worker',
        );
        await expect(
          dialog.getByLabel(/Custom supervisor model ID/),
        ).toHaveValue('openrouter/test/custom-supervisor');
        await dialog.getByRole('button', {name: 'Close settings'}).click();
        // Use the same stored choices and headers as the workbench's run submit;
        // the real API consumes the fake provider list and persists provenance.
        const run = await page.evaluate(async apiUrl => {
          const keys = JSON.parse(localStorage.getItem('cosci-api-keys')!);
          const worker = JSON.parse(localStorage.getItem('cosci-api-model')!);
          const supervisor = JSON.parse(
            localStorage.getItem('cosci-api-supervisor-model')!,
          );
          const response = await fetch(`${apiUrl}/api/runs`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-Client-ID': localStorage.getItem('co_scientist_client_id')!,
              'X-LLM-Provider': worker.provider,
              'X-LLM-API-Key': keys[worker.provider],
              'X-LLM-Model': worker.model,
              'X-LLM-Supervisor-Model': supervisor.model,
            },
            body: JSON.stringify({
              research_goal: 'Explain repair of replication stress',
              tier: 'express',
            }),
          });
          if (!response.ok) throw new Error(await response.text());
          return response.json();
        }, API_URL);
        expect(run.config.byok_models).toEqual({
          worker: 'openrouter/test/custom-worker',
          supervisor: 'openrouter/test/custom-supervisor',
        });
        expect(JSON.stringify(run)).not.toContain('synthetic-e2e-custom-key');
        await page.evaluate(
          async ({apiUrl, runId}) => {
            const response = await fetch(`${apiUrl}/api/runs/${runId}/start`, {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                'X-Client-ID': localStorage.getItem('co_scientist_client_id')!,
              },
              body: '{}',
            });
            if (!response.ok) throw new Error(await response.text());
          },
          {apiUrl: API_URL, runId: run.id},
        );
        await expect
          .poll(
            () =>
              page.evaluate(
                async ({apiUrl, runId}) => {
                  const response = await fetch(`${apiUrl}/api/runs/${runId}`, {
                    headers: {
                      'X-Client-ID': localStorage.getItem(
                        'co_scientist_client_id',
                      )!,
                    },
                  });
                  return (await response.json()).status;
                },
                {apiUrl: API_URL, runId: run.id},
              ),
            {timeout: 60_000},
          )
          .toBe('completed');
      });
    }
  }
}
