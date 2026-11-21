import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import {spawn} from "node:child_process";
import {setTimeout as pause} from "node:timers/promises";

const [executable, profile, url] = process.argv.slice(2);
let socket, sequence = 0;
const pending = new Map();
async function eventually(read, label) {
  for (let index = 0; index < 100; index++) {
    const value = await read();
    if (value) return value;
    await pause(100);
  }
  throw new Error("Timed out: " + label);
}
// Launch Chromium with a fresh profile and wait for the DevTools endpoint.
async function startBrowser() {
  const failures = [];
  for (let attempt = 1; attempt <= 2; attempt++) {
    const directory = `${profile}-${attempt}`;
    const chrome = spawn(executable, [
      "--headless", "--remote-debugging-port=0", "--no-first-run", "--disable-background-networking",
      "--disable-extensions", "--enable-logging=stderr", "--user-data-dir=" + directory, "about:blank",
    ], {stdio: ["ignore", "ignore", "pipe"]});
    let spawnError, stderr = "";
    chrome.once("error", error => { spawnError = error; });
    chrome.stderr.setEncoding("utf8");
    chrome.stderr.on("data", chunk => { stderr = (stderr + chunk).slice(-8192); });
    let ready = false;
    try {
      const deadline = performance.now() + 25_000;
      while (performance.now() < deadline) {
        if (spawnError) throw spawnError;
        if (chrome.exitCode !== null || chrome.signalCode !== null) {
          throw new Error(`Chromium exited during startup (code=${chrome.exitCode}, signal=${chrome.signalCode})`);
        }
        const portFile = await fs.readFile(path.join(directory, "DevToolsActivePort"), "utf8").catch(error => {
          // Windows may briefly lock the file while Chromium publishes the port.
          if (error.code === "ENOENT" || error.code === "EBUSY") return "";
          throw error;
        });
        const port = portFile.split("\n")[0].trim();
        if (/^[0-9]+$/.test(port)) {
          const endpoint = `http://127.0.0.1:${port}/json/list`;
          const pages = await fetch(endpoint, {signal: AbortSignal.timeout(500)})
            .then(response => response.ok ? response.json() : null)
            .catch(() => null);
          const page = pages?.find(item => item.type === "page" && item.webSocketDebuggerUrl);
          if (page) {
            ready = true;
            return {chrome, debuggerUrl: page.webSocketDebuggerUrl};
          }
        }
        await pause(100);
      }
      throw new Error("Timed out waiting for Chromium DevTools");
    } catch (error) {
      failures.push(`Attempt ${attempt}: ${error.message}\n${stderr.trim() || "(no browser stderr)"}`);
    } finally {
      if (!ready && chrome.exitCode === null && chrome.signalCode === null) chrome.kill();
    }
  }
  throw new Error(`Chromium did not start:\n${failures.join("\n")}`);
}

const {chrome, debuggerUrl} = await startBrowser();
try {
  socket = new WebSocket(debuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, {once: true});
    socket.addEventListener("error", reject, {once: true});
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    pending.get(message.id)?.(message);
  });
  async function cdp(method, params = {}) {
    const id = ++sequence;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        pending.delete(id);
        reject(new Error("Browser command timed out: " + method));
      }, 10_000);
      pending.set(id, message => {
        clearTimeout(timeout);
        pending.delete(id);
        if (message.error) reject(new Error(message.error.message));
        else resolve(message.result);
      });
      socket.send(JSON.stringify({id, method, params}));
    });
  }
  function pageLoaded() {
    return new Promise((resolve, reject) => {
      const receive = event => {
        if (JSON.parse(event.data).method !== "Page.loadEventFired") return;
        clearTimeout(timer);
        socket.removeEventListener("message", receive);
        resolve();
      };
      const timer = setTimeout(() => {
        socket.removeEventListener("message", receive);
        reject(new Error("Browser page did not finish loading"));
      }, 15_000);
      socket.addEventListener("message", receive);
    });
  }
  async function navigate(method, params) {
    await Promise.all([pageLoaded(), cdp(method, params)]);
  }
  async function evaluate(expression) {
    const result = await cdp("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text);
    return result.result.value;
  }
  const wait = expression => eventually(async () => {
    try {
      return await evaluate(expression);
    } catch (error) {
      // Chrome may replace the JavaScript context during a real navigation.
      if (!/Execution context was destroyed|Cannot find context|Inspected target navigated or closed/.test(error.message)) throw error;
      return false;
    }
  }, expression);
  const field = id => `document.getElementById(${JSON.stringify(id)})`;
  const click = id => evaluate(`${field(id)}.click()`);
  const change = (id, value, event = "change") => evaluate(`
    (element => { element.value = ${JSON.stringify(value)}; element.dispatchEvent(new Event(${JSON.stringify(event)}, {bubbles: true})); })(${field(id)})
  `);
  const value = id => evaluate(`${field(id)}.value`);
  const folders = "settings-context-folders";
  const save = 'document.querySelector("#workspace-config-form button[type=submit]")';
  const toml = field("workspace-config");
  console.log("Browser:", (await cdp("Browser.getVersion")).product);
  await cdp("Runtime.enable");
  await cdp("Page.enable");
  await navigate("Page.navigate", {url});
  await wait(`document.readyState === "complete" && document.documentElement.dataset.liveReady === "true" && typeof window.harnessEditorCanSwitch === "function" && !!window.harnessRuns && ${field("tab-settings")}?.offsetWidth`);
  await click("tab-progress");
  const checkerNote = "a previous checker finding is still present";
  const checkerPresentation = async reason => evaluate(`(() => {
    renderCheckerStatus(${JSON.stringify({ready: true, status: "checker_needs_review",
      checker_review_reason: reason, error: checkerNote, resumable: true})});
    return [${field("checker-review-reason")}.textContent, ${field("checker-review-technical")}.textContent];
  })()`);
  assert.match((await checkerPresentation("finding_repeated"))[0], /同じチェッカーの指摘/);
  assert.deepEqual(await checkerPresentation(null), ["チェッカーの指摘が残っているため、確認が必要です。", checkerNote]);
  await wait(`!${field("recovery-summary")}.hidden && ${field("recovery-next")}.textContent.includes("実行結果不明")`);
  assert(await evaluate(`${field("recovery-checkpoint")}.textContent.includes("1 / 3")`));
  assert(await evaluate(`${field("recovery-pending")}.textContent.includes("指示 1 件")`));
  assert(await evaluate(`${field("recovery-next")}.textContent.includes("before_migration.log")`));
  await click("recovery-editor");
  assert(await evaluate(`${field("tab-editor")}.getAttribute("aria-selected") === "true"`));
  await evaluate("window.beforeRecoveryReload = true");
  await navigate("Page.reload");
  await wait(`!window.beforeRecoveryReload && document.readyState === "complete" && document.documentElement.dataset.liveReady === "true"`);
  await click("tab-progress");
  await wait(`!${field("recovery-summary")}.hidden && ${field("recovery-next")}.textContent.includes("実行結果不明")`);
  console.log("Browser recovery: checkpoint, pending instruction, blocked hook and reload");
  await click("tab-settings");
  await wait(`!${field("settings-context-roots")}.matches(":disabled")`);
  await evaluate('document.querySelector("#settings-project-details > summary").click()');
  for (const width of [390, 768, 1280]) {
    await cdp("Emulation.setDeviceMetricsOverride", {width, height: 900, deviceScaleFactor: 1, mobile: false});
    const layout = await evaluate('JSON.stringify({viewport: innerWidth, width: document.documentElement.scrollWidth, tabs: document.querySelector(".workspace-tabs").getBoundingClientRect().right, overflow: [...document.querySelectorAll("body *")].filter(e => e.getClientRects().length && e.getBoundingClientRect().right > innerWidth + 1).slice(0, 16).map(e => [e.id || e.className, e.getBoundingClientRect().right])})');
    assert(await evaluate(`document.documentElement.lang === "ja" && document.documentElement.scrollWidth <= ${width}`), layout);
  }
  for (const id of ["settings-context-scope", "settings-context-exclude", "settings-context-recursive",
    "settings-context-max-files", "settings-context-max-total", "settings-context-max-file",
    "settings-context-preview-actions", "settings-input-warning", "settings-input-limit"]) {
    assert.equal(await evaluate(`${field(id)} === null`), true, `${id} should not be shown`);
  }
  await change("settings-context-roots", "default");
  await change(folders, "Shared\nExtra", "input");
  await wait(`${toml}.value.includes('"Extra"') && !${save}.disabled`);
  await change("settings-viewer-enabled", "false", "input");
  assert(await evaluate(`${field("settings-viewer-html")}.disabled`));
  await wait(`!${save}.disabled`);
  await evaluate(`${save}.click()`);
  await wait(`${field("workspace-config-feedback")}.textContent.includes("設定を保存しました")`);
  await click("workspace-config-reload");
  await wait(`!${field("workspace-config-reload")}.disabled`);
  assert.equal(await value(folders), "Shared\nExtra");
  assert.equal(await value("settings-context-roots"), "default");
  await change("settings-viewer-enabled", "", "input");
  assert.equal(await value("settings-viewer-html"), "viewer.html");
  assert.equal(await value("settings-anchor-reference"), "anchors.json");
  assert(!(await evaluate(`${field("settings-viewer-html")}.disabled`)));
  console.log("Browser settings: simplified controls, saved values and viewer path retention");
  await evaluate(`(() => {
    const original = window.fetch;
    window.fixturePermissionQueue = [
      {run_id: "different-run", unit_id: "Different unit", operation_id: "op-one", id: "one",
        title: "Junie の操作を確認", detail: "Read a file", options: [{id: "allow", label: "読む", kind: "allow_once"}]},
      {run_id: "third-run", unit_id: "Third unit", operation_id: "op-two", id: "two",
        title: "Junie の操作を確認", detail: "Other request", options: [{id: "allow", label: "読む", kind: "allow_once"}]},
    ];
    window.fetch = (endpoint, options) => {
      if (endpoint !== "junie-requests") return original(endpoint, options);
      if (options?.method === "POST") {
        window.fixturePermissionAnswer = JSON.parse(options.body);
        window.fixturePermissionQueue = window.fixturePermissionQueue.filter(row => row.id !== window.fixturePermissionAnswer.request_id);
        return Promise.resolve(Response.json({}, {status: 202}));
      }
      return Promise.resolve(Response.json(window.fixturePermissionQueue));
    };
    const input = ${field(folders)};
    input.focus();
    input.setSelectionRange(2, 4);
  })()`);
  const juniePopup = field("junie-confirmation");
  await wait(`${juniePopup}.open && ${field("junie-confirmation-run")}.textContent.includes("different-run")`);
  assert.equal(await evaluate(`${field("tab-settings")}.getAttribute("aria-selected")`), "true");
  await evaluate(`${juniePopup}.querySelector("[data-confirmation-later]").click()`);
  await wait(`${juniePopup}.open && ${field("junie-confirmation-run")}.textContent.includes("third-run")`);
  await evaluate(`${juniePopup}.querySelector("[data-confirmation-later]").click()`);
  await wait(`!${juniePopup}.open && document.activeElement === ${field(folders)}`);
  assert.deepEqual(await evaluate(`[${field(folders)}.selectionStart, ${field(folders)}.selectionEnd]`), [2, 4]);
  assert.equal(await value(folders), "Shared\nExtra");
  await evaluate('document.querySelector("[aria-controls=junie-confirmation]").click()');
  await evaluate(`${juniePopup}.querySelector(".junie-choices button").click()`);
  await evaluate(`${juniePopup}.querySelector(".junie-choices button").click()`);
  await wait(`window.fixturePermissionAnswer && !${juniePopup}.open`);
  assert.deepEqual(await evaluate("window.fixturePermissionAnswer"), {
    run_id: "different-run", operation_id: "op-one", request_id: "one", option_id: "allow",
  });
  console.log("Browser Junie checks: cross-Run queue, exact answer, active tab and editor selection retained");
  await cdp("Browser.close").catch(() => {});
} finally {
  socket?.close();
  if (chrome.exitCode === null) chrome.kill();
}
