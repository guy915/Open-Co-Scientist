import {writeFile} from 'node:fs/promises';
import {test,expect} from '../support/fixtures';
import {tabTo} from '../support/keyboard';
for(const theme of ['light','dark']) {
 test(`@keyboard-menu landing rail preserves native focus across copies ${theme}`,async({page},info)=>{
  await page.addInitScript(mode=>localStorage.setItem('cosci-theme',mode),theme);
  await page.goto('/');
  const open=page.getByRole('button',{name:/Scroll to see how/});
  await tabTo(page,open);await page.keyboard.press('Enter');
  const nav=page.getByRole('navigation',{name:'Landing sections'});
  await tabTo(page,nav.getByRole('link',{name:'Tiers',exact:true}));await page.keyboard.press('Enter');
  await expect(nav.getByRole('link',{name:'Tiers',exact:true})).toHaveAttribute('aria-current','true');
  await tabTo(page,nav.getByRole('link',{name:'FAQ',exact:true}));await page.keyboard.press('Enter');
  const faq=nav.getByRole('link',{name:'FAQ',exact:true});
  await expect(faq).toHaveAttribute('aria-current','true');
  const snapshot=info.outputPath('landing-focus.yml');await writeFile(snapshot,await page.locator('body').ariaSnapshot());
  await writeFile(info.outputPath('landing-focus.json'),JSON.stringify(await page.evaluate(()=>({hasFocus:document.hasFocus(),active:document.activeElement?.outerHTML})),null,2));
  await page.screenshot({path:info.outputPath('landing-focus.png')});
  await expect(faq).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  const tiers=nav.getByRole('link',{name:'Tiers',exact:true});await expect(tiers).toBeFocused();
  await page.keyboard.press('Enter');await expect(tiers).toHaveAttribute('aria-current','true');await expect(tiers).toBeFocused();
 });
}
