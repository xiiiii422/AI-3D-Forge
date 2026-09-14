/**
 * 极简 CDP（Chrome DevTools Protocol）客户端 —— 只为本项目的前端验收使用。
 *
 * 为什么自己写：环境里没有 puppeteer/playwright，而我们只需要
 * 「打开页面 + 真实输入 + 截图 + 读 DOM」这几件事。Node 22 自带 WebSocket 与 fetch，
 * 因此零依赖即可驱动系统自带的 Edge（headless）。
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';

const BROWSER_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
];

export function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

export function findBrowser() {
  return BROWSER_CANDIDATES.find((p) => existsSync(p)) ?? null;
}

/** 启动 headless 浏览器，返回 { port, info, kill() } */
export async function launchBrowser({ port = 9222, width = 1280, height = 800, userDataDir } = {}) {
  const exe = findBrowser();
  if (!exe) throw new Error('找不到 Edge/Chrome 可执行文件');

  const profile = userDataDir ?? join(tmpdir(), `aiforge-cdp-${port}`);
  rmSync(profile, { recursive: true, force: true });
  mkdirSync(profile, { recursive: true });

  const child = spawn(
    exe,
    [
      '--headless=new',
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      `--window-size=${width},${height}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--disable-background-timer-throttling',
      '--disable-renderer-backgrounding',
      '--force-device-scale-factor=1',
      '--hide-scrollbars',
      // headless 下没有真实 GPU，用 SwiftShader 软件渲染保证 WebGL 可用
      '--enable-unsafe-swiftshader',
      '--use-gl=angle',
      '--use-angle=swiftshader',
      'about:blank',
    ],
    { stdio: 'ignore' },
  );

  let info = null;
  for (let i = 0; i < 80; i += 1) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) {
        info = await res.json();
        break;
      }
    } catch {
      /* 端口还没起来 */
    }
    await sleep(250);
  }
  if (!info) {
    child.kill();
    throw new Error('浏览器调试端口没有就绪');
  }

  return {
    port,
    info,
    pid: child.pid,
    async kill() {
      try {
        if (process.platform === 'win32') {
          spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
        } else {
          child.kill();
        }
      } catch {
        /* 忽略 */
      }
      await sleep(500);
      try {
        rmSync(profile, { recursive: true, force: true });
      } catch {
        /* 文件可能还被占用，忽略 */
      }
    },
  };
}

/** 新建标签页并连接，返回小客户端 */
export async function connectPage(port, url = 'about:blank') {
  const res = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, {
    method: 'PUT',
  });
  if (!res.ok) throw new Error(`创建标签页失败：${res.status}`);
  const target = await res.json();

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', () => reject(new Error('WebSocket 连接失败')), { once: true });
  });

  let nextId = 1;
  const pending = new Map();
  const listeners = new Map();

  ws.addEventListener('message', (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id !== undefined) {
      const entry = pending.get(msg.id);
      if (!entry) return;
      pending.delete(msg.id);
      if (msg.error) entry.reject(new Error(`${entry.method}: ${msg.error.message}`));
      else entry.resolve(msg.result);
      return;
    }
    const handlers = listeners.get(msg.method);
    if (handlers) for (const h of [...handlers]) h(msg.params);
  });

  const send = (method, params = {}, timeoutMs = 30000) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`${method} 超时（${timeoutMs}ms）`));
      }, timeoutMs);
      pending.set(id, {
        method,
        resolve: (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      ws.send(JSON.stringify({ id, method, params }));
    });

  const on = (method, handler) => {
    if (!listeners.has(method)) listeners.set(method, new Set());
    listeners.get(method).add(handler);
    return () => listeners.get(method).delete(handler);
  };

  const waitForEvent = (method, timeoutMs = 15000) =>
    new Promise((resolve, reject) => {
      const off = on(method, (params) => {
        off();
        clearTimeout(timer);
        resolve(params);
      });
      const timer = setTimeout(() => {
        off();
        reject(new Error(`等待事件 ${method} 超时`));
      }, timeoutMs);
    });

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');

  const client = {
    targetId: target.id,
    send,
    on,

    async setViewport(width, height) {
      await send('Emulation.setDeviceMetricsOverride', {
        width,
        height,
        deviceScaleFactor: 1,
        mobile: false,
      });
    },

    async goto(pageUrl, { timeoutMs = 30000, settleMs = 350 } = {}) {
      const loaded = waitForEvent('Page.loadEventFired', timeoutMs).catch(() => null);
      await send('Page.navigate', { url: pageUrl });
      await loaded;
      await sleep(settleMs);
    },

    async reload({ settleMs = 500 } = {}) {
      const loaded = waitForEvent('Page.loadEventFired', 20000).catch(() => null);
      await send('Page.reload', { ignoreCache: false });
      await loaded;
      await sleep(settleMs);
    },

    async evaluate(expression) {
      const result = await send('Runtime.evaluate', {
        expression,
        returnByValue: true,
        awaitPromise: true,
        userGesture: true,
      });
      if (result.exceptionDetails) {
        throw new Error(
          `页面脚本异常: ${
            result.exceptionDetails.exception?.description ?? result.exceptionDetails.text
          }`,
        );
      }
      return result.result.value;
    },

    async waitForFunction(expression, { timeoutMs = 20000, pollMs = 200, label } = {}) {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        let ok = false;
        try {
          ok = await client.evaluate(
            `(() => { try { return !!(${expression}); } catch (e) { return false; } })()`,
          );
        } catch {
          ok = false;
        }
        if (ok) return true;
        if (Date.now() > deadline) throw new Error(`等待条件超时：${label ?? expression}`);
        await sleep(pollMs);
      }
    },

    async waitForBox(selector, { index = 0, timeoutMs = 10000 } = {}) {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const box = await client.evaluate(`(() => {
          const el = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
          if (!el) return null;
          const r = el.getBoundingClientRect();
          if (r.width < 1 || r.height < 1) return null;
          return { x: r.x, y: r.y, width: r.width, height: r.height };
        })()`);
        if (box) return box;
        if (Date.now() > deadline) throw new Error(`元素不可见：${selector}[${index}]`);
        await sleep(200);
      }
    },

    /** 把元素滚到视口中间，保证后续命中测试与坐标点击可靠 */
    async scrollIntoView(selector, { index = 0 } = {}) {
      const moved = await client.evaluate(`(() => {
        const el = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
        if (!el) return false;
        el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
        return true;
      })()`);
      if (moved) await sleep(180);
      return moved;
    },

    /**
     * 真实鼠标点击。
     * @param selector CSS 选择器
     * @param finder   每次点击前重新解析目标的函数（默认返回 selector）。
     *                 React 重渲染会替换掉被标记的节点，所以必须在派发事件「之前」重新取。
     */
    async click(selector, { index = 0, timeoutMs = 10000, finder = null } = {}) {
      const resolve = async () => (finder ? await finder() : selector);
      const boxOf = async (sel) => client.waitForBox(sel, { index, timeoutMs });
      const hitTest = async (sel, x, y) =>
        client.evaluate(`(() => {
          const el = document.querySelectorAll(${JSON.stringify(sel)})[${index}];
          const top = document.elementFromPoint(${x}, ${y});
          return { ok: !!top && !!el && (top === el || el.contains(top) || top.contains(el)), by: top ? top.tagName + '.' + (typeof top.className === 'string' ? top.className : '') : null };
        })()`);

      let sel = await resolve();
      let box = await boxOf(sel);

      // 长页面里目标可能在视口外：先滚到中间再点
      if (box.y < 0 || box.y + box.height > 800) {
        await client.evaluate(`(() => {
          const el = document.querySelectorAll(${JSON.stringify(sel)})[${index}];
          if (el) el.scrollIntoView({ block: 'center', behavior: 'instant' });
        })()`);
        await sleep(250);
        sel = await resolve(); // 重新解析：React 可能已经换掉了节点
        box = await boxOf(sel);
      }

      let x = Math.round(box.x + box.width / 2);
      let y = Math.round(box.y + box.height / 2);
      let hit = await hitTest(sel, x, y);

      if (!hit.ok && finder) {
        sel = await resolve();
        box = await boxOf(sel);
        x = Math.round(box.x + box.width / 2);
        y = Math.round(box.y + box.height / 2);
        hit = await hitTest(sel, x, y);
      }
      if (!hit.ok) throw new Error(`${sel} 中心点(${x},${y})被 ${hit.by} 遮挡`);

      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none' });
      await send('Input.dispatchMouseEvent', {
        type: 'mousePressed',
        x,
        y,
        button: 'left',
        buttons: 1,
        clickCount: 1,
      });
      await send('Input.dispatchMouseEvent', {
        type: 'mouseReleased',
        x,
        y,
        button: 'left',
        buttons: 0,
        clickCount: 1,
      });
      await sleep(150);
      return { x, y, width: box.width, height: box.height };
    },

    /**
     * 给「文字所在的最深元素」打标记，返回「取选择器的函数」。
     * 返回函数而不是一次性字符串：滚动/重渲染后标记可能失效，调用方可以重新取。
     *
     * 两个坑：
     *  1. 不能取 document.querySelectorAll('*').find(...)，那样第一个命中的永远是 <html>；
     *  2. 必须排除 <style>/<script> 等不可渲染节点，否则 CSS 源码里的文字也会命中
     *     （例如样式表里的 [data-theme='dark'] 会被当成界面上的「深色」）。
     */
    async markByText(
      text,
      { tag = '*', exact = false, clickable = true, timeoutMs = 10000 } = {},
    ) {
      const pick = `(() => {
        const target = ${JSON.stringify(text)};
        const SKIP = new Set(['STYLE', 'SCRIPT', 'TEMPLATE', 'NOSCRIPT', 'HEAD', 'TITLE', 'META', 'LINK']);
        const CLICKABLE = 'button, a, [role="button"], [role="menuitem"], [role="switch"], label, summary';
        const matches = (el) => {
          if (SKIP.has(el.tagName)) return false;
          const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
          return ${exact ? 't === target' : 't.includes(target)'};
        };
        const all = [...document.querySelectorAll(${JSON.stringify(tag)})].filter(matches);
        const deepest = all.filter(el => ![...el.children].some(matches));
        // 优先取「能点到」的那些：否则说明性文字（例如提示里出现的按钮名）会抢先命中，
        // 例如诊断页底部提示里的「自动修复」会盖过真正的按钮。
        const clickable =
          ${clickable}
            ? deepest.map(el => el.closest(CLICKABLE)).filter(Boolean)
            : [];
        const el = clickable[0] ?? deepest[0] ?? all[0];
        if (!el) return null;
        const clickTarget = ${clickable} ? (el.closest(CLICKABLE) ?? el) : el;
        const m = 'cdp' + Math.random().toString(36).slice(2, 8);
        clickTarget.setAttribute('data-cdp', m);
        return m;
      })()`;

      const deadline = Date.now() + timeoutMs;
      let mark = null;
      for (;;) {
        mark = await client.evaluate(pick);
        if (mark) break;
        if (Date.now() > deadline) throw new Error(`找不到包含「${text}」的 ${tag}`);
        await sleep(200);
      }

      const finder = async () => {
        // 直接重新打一次标记：滚动或重渲染后旧标记可能已经不在文档里了
        const again = await client.evaluate(pick);
        if (again) return `[data-cdp="${again}"]`;
        return `[data-cdp="${mark}"]`;
      };

      return { selector: `[data-cdp="${mark}"]`, finder };
    },

    /**
     * 点击「包含指定文字」的元素，并可选地等待点击生效。
     * @param expect 一个返回布尔值的页面表达式；点击后若为 false 会重新标记再点一次，
     *               用来兜住「React 把节点换掉导致这一下点空」的情况。
     */
    async clickText(text, { expect = null, attempts = 3, ...options } = {}) {
      let lastError = null;
      for (let i = 0; i < attempts; i += 1) {
        const mark = await client.markByText(text, options);
        try {
          await client.click(mark.selector, { finder: mark.finder });
        } catch (err) {
          lastError = err;
          await sleep(300);
          continue;
        }
        if (!expect) return;
        const deadline = Date.now() + 4000;
        for (;;) {
          if (await client.evaluate(`(() => { try { return !!(${expect}); } catch (e) { return false; } })()`)) {
            return;
          }
          if (Date.now() > deadline) break;
          await sleep(200);
        }
        await sleep(200);
      }
      throw new Error(
        `点击「${text}」后条件未满足${lastError ? `（最后一次错误：${lastError.message}）` : ''}`,
      );
    },

    /** 点击并把焦点交给目标元素（输入框用） */
    async focus(selector, { index = 0, timeoutMs = 10000 } = {}) {
      await client.click(selector, { index, timeoutMs });
      await client.evaluate(
        `document.querySelectorAll(${JSON.stringify(selector)})[${index}].focus()`,
      );
      await sleep(100);
    },

    /** 用真实输入事件敲字（中文走 insertText，不依赖输入法） */
    async type(text) {
      await send('Input.insertText', { text });
      await sleep(100);
    },

    async pressKey(key) {
      const map = {
        Enter: { code: 'Enter', vk: 13, text: '\r' },
        Escape: { code: 'Escape', vk: 27 },
        Tab: { code: 'Tab', vk: 9 },
        Backspace: { code: 'Backspace', vk: 8 },
      };
      const info = map[key] ?? { code: key, vk: 0 };
      const base = {
        key,
        code: info.code,
        windowsVirtualKeyCode: info.vk,
        nativeVirtualKeyCode: info.vk,
      };
      await send('Input.dispatchKeyEvent', { ...base, type: info.text ? 'keyDown' : 'rawKeyDown', text: info.text });
      await send('Input.dispatchKeyEvent', { ...base, type: 'keyUp' });
      await sleep(100);
    },

    /** 覆盖式设置输入框的值，并派发 React 能感知的 input 事件 */
    async setInputValue(selector, value, { index = 0 } = {}) {
      await client.evaluate(`(() => {
        const el = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
        if (!el) throw new Error('找不到输入框');
        const proto = el instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, ${JSON.stringify(String(value))});
        el.dispatchEvent(new Event('input', { bubbles: true }));
        return el.value;
      })()`);
      await sleep(100);
    },

    async waitForUrlIncludes(fragment, timeoutMs = 15000) {
      const deadline = Date.now() + timeoutMs;
      let href = '';
      for (;;) {
        href = await client.evaluate('location.href');
        if (href.includes(fragment)) return href;
        if (Date.now() > deadline) throw new Error(`URL 未包含「${fragment}」，当前 ${href}`);
        await sleep(200);
      }
    },

    async screenshot(path, { fullPage = false } = {}) {
      const params = { format: 'png' };
      if (fullPage) {
        const metrics = await send('Page.getLayoutMetrics');
        const size = metrics.cssContentSize ?? metrics.contentSize;
        params.captureBeyondViewport = true;
        params.clip = { x: 0, y: 0, width: size.width, height: size.height, scale: 1 };
      }
      const { data } = await send('Page.captureScreenshot', params, 20000);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, Buffer.from(data, 'base64'));
      return path;
    },

    /** 收集控制台错误、页面异常与失败请求（验收要求：无 console 错误） */
    collectProblems() {
      const problems = { console: [], pageErrors: [], failedRequests: [] };
      on('Runtime.consoleAPICalled', (p) => {
        if (p.type === 'error' || p.type === 'warning' || p.type === 'assert') {
          problems.console.push(
            `${p.type}: ${p.args.map((a) => a.value ?? a.description ?? a.type).join(' ')}`,
          );
        }
      });
      on('Runtime.exceptionThrown', (p) => {
        problems.pageErrors.push(p.exceptionDetails?.exception?.description ?? '未知异常');
      });
      on('Network.loadingFailed', (p) => {
        if (p.canceled) return;
        problems.failedRequests.push(`${p.type} ${p.errorText}`);
      });
      return problems;
    },

    close() {
      try {
        ws.close();
      } catch {
        /* 忽略 */
      }
    },
  };

  return client;
}
