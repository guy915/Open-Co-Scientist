import {captureViewport, expect, test} from '../support/fixtures';

for (const theme of ['light', 'dark']) {
  test(`phone feedback opens from the header and submits in ${theme}`, async ({
    page,
  }) => {
    await page.setViewportSize({width: 390, height: 844});
    await page.addInitScript(mode => {
      localStorage.setItem('cosci-theme', mode);
      localStorage.setItem('co_scientist_client_id', `phone-feedback-${mode}`);
    }, theme);
    await page.goto('/');
    const navigation = page.getByRole('button', {name: 'Open navigation'});
    const feedback = page.getByRole('button', {name: 'Feedback', exact: true});
    await expect(feedback).toBeInViewport();
    await feedback.click();
    const dialog = page.getByRole('dialog', {name: 'Feedback', exact: true});
    await expect(dialog.getByLabel('Message')).toBeFocused();
    await expect(navigation).toHaveAttribute('aria-expanded', 'false');
    await dialog.getByLabel('Message').fill(`Phone feedback ${theme}`);
    await captureViewport(page, {
      width: 390,
      height: 844,
      name: `phone-feedback-${theme}.png`,
    });
    const submitted = page.waitForResponse(
      response =>
        response.url().endsWith('/api/feedback') &&
        response.request().method() === 'POST',
    );
    await dialog.getByRole('button', {name: 'Submit', exact: true}).click();
    expect((await submitted).status()).toBe(201);
    await expect(dialog).toHaveCount(0);
    await expect(feedback).toBeFocused();
    await feedback.click();
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(feedback).toBeFocused();
    await expect(page.getByRole('textbox')).toBeEditable();
  });
}
