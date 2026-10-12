import {writeFile} from 'node:fs/promises';
import AxeBuilder from '@axe-core/playwright';
import type {Page} from '@playwright/test';
import {test,expect} from '../support/fixtures';
import {tabTo} from '../support/keyboard';
// WebKit keeps an earlier native smooth scroll running after a later jump, so
// each rail jump must finish before the next key press.
async function pressAndAwaitScroll(page:Page,key:string){
  const pane=page.locator('.ucs-page--home');
  await pane.evaluate(node=>{
    const start=node.scrollTop;
    node.setAttribute('data-test-scroll-ended','false');
    const done=()=>{
      if(Math.abs(node.scrollTop-start)<1)return;
      node.removeEventListener('scrollend',done);
      node.setAttribute('data-test-scroll-ended','true');
    };
    node.addEventListener('scrollend',done);
  });
  await page.keyboard.press(key);
  await expect(pane).toHaveAttribute('data-test-scroll-ended','true');
}
for(const theme of ['light','dark']) {
 test(`@keyboard-menu landing navigation maintains contrast during selection ${theme}`,async({page},info)=>{
  await page.addInitScript(mode=>localStorage.setItem('cosci-theme',mode),theme);
  await page.goto('/');
  const open = page.getByRole('button',{name:/Scroll to see how/});
  await tabTo(page,open);await pressAndAwaitScroll(page,'Enter');
  const nav=page.getByRole('navigation',{name:'Landing sections'});
  await tabTo(page,nav.getByRole('link',{name:'Tiers',exact:true}));await pressAndAwaitScroll(page,'Enter');
  await expect(nav.getByRole('link',{name:'Tiers',exact:true})).toHaveAttribute('aria-current','true');
  // Let the initial native scroll and prior rail selection settle.
  await page.evaluate(()=>new Promise<void>(resolve=>setTimeout(resolve,600)));
  await page.evaluate(()=>{
   const observer=new MutationObserver(()=>{
    const live=document.querySelector('.ucs-landing-header-tabs:not([inert]) nav');
    const tiers=live?.querySelector('[data-section="landing-tiers"]');
    const faq=live?.querySelector('[data-section="faq"]');
    if(!live || !tiers || faq?.getAttribute('aria-current')!=='true' || tiers.getAttribute('aria-current')==='true')return;
    observer.disconnect();
    for(const node of Array.from(live.querySelectorAll('*')))void getComputedStyle(node).color;
    for(const animation of live.getAnimations({subtree:true})) {
     animation.pause();const timing=animation.effect!.getTiming();animation.currentTime=(timing.delay??0)+Number(timing.duration)*.5;
    }
   });
   observer.observe(document.body,{subtree:true,attributes:true,attributeFilter:['class','aria-current']});
  });
  await tabTo(page,nav.getByRole('link',{name:'FAQ',exact:true}));await page.keyboard.press('Enter');
  await expect(nav.getByRole('link',{name:'FAQ',exact:true})).toHaveAttribute('aria-current','true');
  await expect(nav).toBeInViewport();
  const image=info.outputPath('landing-transition.png');await nav.screenshot({path:image});
  const results=await new AxeBuilder({page}).include('nav[aria-label="Landing sections"]').withRules(['color-contrast']).analyze();
  await writeFile(info.outputPath('landing-transition.json'),JSON.stringify({violations:results.violations,geometry:await nav.evaluate(n=>({bounds:n.getBoundingClientRect().toJSON(),animations:n.getAnimations({subtree:true}).map(a=>({time:a.currentTime,timing:a.effect!.getTiming(),frames:(a.effect as KeyframeEffect).getKeyframes()}))}))},null,2));
  await writeFile(info.outputPath('landing-transition.yml'),await nav.ariaSnapshot());
  expect(results.violations).toEqual([]);
 });
}
