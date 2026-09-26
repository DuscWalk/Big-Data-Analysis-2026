// Export repository-native SVG figures with local fonts. No app or model requests.
const { chromium } = require("playwright");
const fs = require("node:fs/promises");
const path = require("node:path");

(async () => {
  const root = path.resolve(__dirname, "../..");
  const dir = path.join(root, "docs/architecture/diagrams");
  const browser = await chromium.launch({ headless: true });
  try {
    for (const file of (await fs.readdir(dir)).filter(name => name.endsWith(".svg")).sort()) {
      const source = await fs.readFile(path.join(dir, file), "utf8");
      const page = await browser.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      await page.setContent('<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;}svg{display:block;}@page{margin:0;}</style></head><body>' + source.replace(/<\?xml[^>]*>|<!DOCTYPE[\s\S]*?>/g, "") + '</body></html>');
      const size = await page.evaluate(() => {
        const svg = document.querySelector("svg"), box = svg.viewBox.baseVal;
        svg.setAttribute("width", Math.ceil(box.width));
        svg.setAttribute("height", Math.ceil(box.height));
        return { width: Math.ceil(box.width), height: Math.ceil(box.height) };
      });
      await page.setViewportSize(size);
      await page.evaluate(() => document.fonts.ready);
      const clipped = await page.evaluate(() => [...document.querySelectorAll("g.module")].flatMap(group => {
        const rect = group.querySelector("rect").getBBox();
        return [...group.querySelectorAll("text")].filter(text => {
          const box = text.getBBox();
          return box.x < rect.x || box.y < rect.y || box.x + box.width > rect.x + rect.width - 7 || box.y + box.height > rect.y + rect.height - 4;
        }).map(text => ({ module: group.id, text: text.textContent }));
      }));
      if (errors.length || clipped.length) throw new Error(JSON.stringify({ file, errors, clipped }));
      const basename = file.slice(0, -4);
      await page.screenshot({ path: path.join(dir, basename + ".png"), fullPage: true });
      await page.pdf({ path: path.join(dir, basename + ".pdf"), width: size.width + "px", height: size.height + "px", printBackground: true, margin: { top: 0, right: 0, bottom: 0, left: 0 } });
      console.log(JSON.stringify({ diagram: basename, ...size, clipped_text: clipped, browser_errors: errors }));
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
