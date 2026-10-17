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
      await page.route("http://qr-upload.test/**", async route => {
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
      await page.goto("http://qr-upload.test/#" + token);
      await page.waitForFunction(() => !document.getElementById("files").disabled);
      assert.equal(await page.evaluate(() => window.isSecureContext), false);
      assert.match(await page.locator(".privacy").textContent(), /传输未加密，仅限可信 Wi-Fi/);
      assert.match(await page.locator(".formats").textContent(), /200 MB.*USB/);
      assert.equal(new URL(page.url()).hash, "");
      for (const size of [100000001, 200000000, 200000001]) {
        await page.evaluate(size => {
          const input = document.getElementById("files");
          const data = new DataTransfer();
          data.items.add(new File(["%PDF-1.4"], "boundary.pdf", { type: "application/pdf" }));
          input.files = data.files;
          // Exercise admission without allocating a 200 MB browser fixture.
          Object.defineProperty(input.files[0], "size", { value: size });
          input.dispatchEvent(new Event("change"));
        }, size);
        assert.equal(await page.locator("#send").isDisabled(), size > 200000000);
        if (size > 200000000) {
          assert.match(await page.locator(".state").textContent(), /200 MB.*USB/);
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
          await page.screenshot({ path: path.join(output, `oversize-${width}.png`), fullPage: true });
        } else {
          assert.equal(await page.locator(".state").textContent(), "待发送");
        }
      }
      assert.deepEqual(uploads, []);
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
      for (const name of ["中".repeat(90) + ".epub", "bad:name.epub", "\nbook.pdf", "\u202e书籍.epub", " .epub "]) {
        await page.locator("#files").setInputFiles({ name, mimeType: "application/epub+zip", buffer: fixture });
        assert.equal(await page.locator("#send").isDisabled(), true);
        assert.match(await page.locator(".state").textContent(), /文件名/);
      }
      assert.deepEqual(uploads, [longName]);
      const paddedName = " 书籍.epub ";
      await page.locator("#files").setInputFiles({ name: paddedName, mimeType: "application/epub+zip", buffer: fixture });
      await page.locator("#send").click();
      await page.waitForFunction(() => document.getElementById("result").textContent.includes("全部文档"));
      assert.deepEqual(uploads, [longName, paddedName]);
      fail = true;
      await page.locator("#files").setInputFiles([
        { name: "失败.pdf", mimeType: "application/pdf", buffer: fixture },
        { name: "不要自动发送.pdf", mimeType: "application/pdf", buffer: fixture }
      ]);
      await page.locator("#send").click();
      await page.waitForFunction(() => document.getElementById("result").textContent.includes("暂停"));
      assert.deepEqual(uploads, [longName, paddedName, "失败.pdf"]);
      assert.equal(await page.locator("li[data-state=error]").count(), 1);
      assert.equal(await page.locator("li[data-state=pending]").count(), 1);
      assert.equal(await page.locator("#send").isDisabled(), true);
      await page.screenshot({ path: path.join(output, `failure-${width}.png`), fullPage: true });
      assert.deepEqual(errors, []);
      await context.close();
    }
    // Simulate a file-provider stall before the browser can send any bytes.
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    await page.addInitScript(() => {
      window.XMLHttpRequest = class {
        constructor() { this.upload = {}; }
        open() {}
        setRequestHeader() {}
        send(file) {
          window.testXHR = this;
          window.testDirectFile = file instanceof File;
          if (window.testReadFailure) throw new Error("file provider unavailable");
        }
        abort() { this.onabort(); this.onloadend(); }
      };
    });
    await page.route("http://qr-upload.test/**", async route => {
      const name = new URL(route.request().url()).pathname;
      if (name === "/status") return route.fulfill({ json: { message: "设备已连接" } });
      const assets = { "/": ["index.html", "text/html"], "/app.js": ["app.js", "text/javascript"], "/style.css": ["style.css", "text/css"] };
      const [file, contentType] = assets[name];
      return route.fulfill({ contentType, body: await fs.readFile(path.join(root, "web", file)) });
    });
    await page.goto("http://qr-upload.test/#" + token);
    await page.waitForFunction(() => !document.getElementById("files").disabled);
    await page.locator("#files").setInputFiles({ name: "大文件.pdf", mimeType: "application/pdf", buffer: Buffer.alloc(2000000) });
    await page.locator("#send").click();
    await page.waitForFunction(() => document.querySelector(".state").textContent.includes("已等待 1 秒"));
    assert.equal(await page.evaluate(() => window.testDirectFile), true);
    assert.equal(await page.locator("#progress").getAttribute("value"), null);
    await page.evaluate(() => window.testXHR.upload.onprogress({ loaded: 1000000, total: 2000000, lengthComputable: true }));
    assert.match(await page.locator(".state").textContent(), /发送中 50%.*1.0 \/ 2.0 MB/);
    assert.equal(await page.locator("#progress").getAttribute("value"), "50");
    await page.screenshot({ path: path.join(output, "receiving-390.png"), fullPage: true });
    await page.evaluate(() => window.testXHR.upload.onload());
    assert.match(await page.locator(".state").textContent(), /等待设备校验和系统导入/);
    assert.equal(await page.locator("#progress").getAttribute("value"), null);
    await page.evaluate(() => {
      Object.assign(window.testXHR, { status: 200, responseText: JSON.stringify({ message: "系统已接受导入" }) });
      window.testXHR.onload();
      window.testXHR.onloadend();
    });
    await page.waitForFunction(() => document.getElementById("result").textContent.includes("全部文档"));
    await page.waitForTimeout(1100);
    assert.equal(await page.locator(".state").textContent(), "系统已接受导入");
    await page.evaluate(() => { window.testReadFailure = true; });
    await page.locator("#files").setInputFiles({ name: "未下载.pdf", mimeType: "application/pdf", buffer: fixture });
    await page.locator("#send").click();
    await page.waitForFunction(() => document.getElementById("result").textContent.includes("暂停"));
    await page.waitForTimeout(1100);
    assert.match(await page.locator(".state").textContent(), /无法读取所选文件/);
    await context.close();
    console.log("Phone UI checks passed at 320, 390 and 1280px; uploads are mocked, not hardware-verified.");
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
