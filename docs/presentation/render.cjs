// Render a figure page to PNG (3840 x 2160) and vector PDF, and fail on layout defects.
// Usage: node render.cjs <name>   (reads <name>.html next to this file)
// Needs Playwright with Chromium (npx playwright install chromium, or a preinstalled browser).
// If Playwright is installed globally, run with NODE_PATH=$(npm root -g).
const { chromium } = require('playwright');
const path = require('node:path');

(async () => {
const dir = __dirname;
const name = process.argv[2];
const page = await (await chromium.launch()).newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 });
await page.goto('file://' + path.join(dir, name + '.html'), { waitUntil: 'networkidle' });
await page.evaluate(() => document.fonts.ready);

const problems = await page.evaluate(() => {
  const out = [];
  if (!document.fonts.check('600 18px "IBM Plex Sans"')) out.push('IBM Plex Sans did not load');
  const r = el => el.getBoundingClientRect();
  const hit = (a, b, pad = 0) => a.left < b.right + pad && b.left < a.right + pad && a.top < b.bottom + pad && b.top < a.bottom + pad;
  const name = el => (el.innerText || '').trim().slice(0, 40).replace(/\n/g, ' / ');
  // 1. text must fit inside its box
  for (const box of document.querySelectorAll('.chk')) {
    if (box.scrollHeight > box.clientHeight + 1 || box.scrollWidth > box.clientWidth + 1) out.push('overflow: ' + name(box));
    for (const t of box.querySelectorAll('.t,.k,.d,.steps div')) {
      const a = r(t), b = r(box);
      if (a.right > b.right - 4 || a.bottom > b.bottom - 2) out.push('text past edge: ' + name(t));
      // nowrap lines: compare the text's real width with its container
      if (t.scrollWidth > t.clientWidth + 1) out.push('line too long: ' + name(t));
    }
  }
  // 2. labels must not touch boxes or tags, nor each other
  const solids = [...document.querySelectorAll('.box:not(.group),.tag,.chip')];
  const labels = [...document.querySelectorAll('.chkl')];
  for (const l of labels) {
    const range = document.createRange(); range.selectNodeContents(l);
    const a = range.getBoundingClientRect();
    for (const s of solids) if (hit(a, r(s), 4)) out.push(`label "${name(l)}" touches "${name(s)}"`);
    for (const m of labels) if (m !== l && hit(a, r(m), 2)) out.push(`label "${name(l)}" touches label "${name(m)}"`);
    // 3. labels must stay 6px clear of every arrow segment
    for (const p of document.querySelectorAll('path.seg')) {
      const n = p.getTotalLength();
      for (let s = 0; s <= n; s += 3) {
        const q = p.getPointAtLength(s);
        if (q.x > a.left - 6 && q.x < a.right + 6 && q.y > a.top - 6 && q.y < a.bottom + 6) {
          out.push(`label "${name(l)}" sits on an arrow`); break;
        }
      }
    }
  }
  // 4. tags must not cover box titles
  for (const t of document.querySelectorAll('.tag')) for (const ti of document.querySelectorAll('.t'))
    { const range = document.createRange(); range.selectNodeContents(ti); if (hit(r(t), range.getBoundingClientRect(), 3)) out.push('tag covers title: ' + name(ti)); }
  return [...new Set(out)];
});

await page.screenshot({ path: path.join(dir, name + '.png') });
await page.pdf({ path: path.join(dir, name + '.pdf'), width: '1920px', height: '1080px', printBackground: true, pageRanges: '1' });
await page.context().browser().close();
if (problems.length) { console.log(problems.join('\n')); process.exitCode = 1; } else console.log('clean: ' + name);
})();
