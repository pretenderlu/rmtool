// Optional development-only Playwright checks; no browser code ships to the tablet.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");

const root = __dirname;
const output = path.resolve(root, "../build/qr-upload/screenshots");
const token = "x".repeat(43);
const fixture = Buffer.from("%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n");

async function run() {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
  try {
    for (const width of [320, 390, 1280]) {
      const context = await browser.newContext({ viewport: { width, height: width < 500 ? 844 : 900 } });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", e => errors.push(e.message));
      const uploads = [];
      let fail = false;
      await page.route("https://qr-upload.test/**", async route => {
        const request = route.request();
        const pathname = new URL(request.url()).pathname;
        const assets = { "/": ["index.html", "text/html"], "/app.js": ["app.js", "text/javascript"], "/style.css": ["style.css", "text/css"] };
        if (assets[pathname]) {
          const [name, contentType] = assets[pathname];
          return route.fulfill({ status: 200, contentType, body: await fs.readFile(path.join(root, "web", name)) });
        }
        assert.equal(request.headers().authorization, "Bearer " + token);
        if (pathname === "/status") {
          return route.fulfill({ json: { message: "设备已连接" } });
        }
        assert.equal(pathname, "/upload");
        assert.equal(request.method(), "POST");
        assert.deepEqual(request.postDataBuffer(), fixture);
        uploads.push(decodeURIComponent(request.headers()["x-filename"]));
        return route.fulfill({ status: fail ? 502 : 200, json: { message: fail ? "无法确认系统导入结果，请先检查设备资料库，避免重复上传" : "系统已接受导入，请在设备资料库查看" } });
      });
      await page.goto("https://qr-upload.test/#" + token);
      await page.waitForFunction(() => !document.getElementById("files").disabled);
      assert.equal(new URL(page.url()).hash, "");
      const longName = "中文书籍和特别长的名字_" + "abcdefghij".repeat(12) + ".pdf";
      await page.locator("#files").setInputFiles({ name: longName, mimeType: "application/pdf", buffer: fixture });
      assert.equal(await page.locator(".name").textContent(), longName);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await page.screenshot({ path: path.join(output, `selected-${width}.png`), fullPage: true });
      await page.locator("#send").click();
      await page.waitForFunction(() => document.getElementById("result").textContent.includes("全部文档"));
      assert.deepEqual(uploads, [longName]);
      assert.equal(await page.locator("#send").isDisabled(), true);
      assert.equal(await page.locator("#progress").isVisible(), false);
      fail = true;
      await page.locator("#files").setInputFiles([
        { name: "失败.pdf", mimeType: "application/pdf", buffer: fixture },
        { name: "不要自动发送.pdf", mimeType: "application/pdf", buffer: fixture }
      ]);
      await page.locator("#send").click();
      await page.waitForFunction(() => document.getElementById("result").textContent.includes("暂停"));
      assert.deepEqual(uploads, [longName, "失败.pdf"]);
      assert.equal(await page.locator("li[data-state=error]").count(), 1);
      assert.equal(await page.locator("li[data-state=pending]").count(), 1);
      assert.equal(await page.locator("#send").isDisabled(), true);
      await page.screenshot({ path: path.join(output, `failure-${width}.png`), fullPage: true });
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log("Phone UI checks passed at 320, 390 and 1280px; uploads are mocked, not hardware-verified.");
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
