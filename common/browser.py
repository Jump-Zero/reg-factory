# -*- coding: utf-8 -*-
"""
common/browser.py — BitBrowser 连接 + 反检测 stealth 注入（从 register.py 抽取，通用）

用法:
    from common.browser import open_and_connect, teardown, human_type
    bb, pid, browser, ctx, page = await open_and_connect(name="chatgpt_xxx")
    ...
    await teardown(bb, pid, delete=True)
"""

import asyncio
import random
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright

import os
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bitbrowser import BitBrowser
from common.fingerprint import browser_fingerprint
from common.playwright_runtime import install_shutdown_guard
from common.task_context import active_worker
from common.traffic_saver import install as install_traffic_saver

# 与 register.py 完全一致的反检测脚本
STEALTH_JS = r"""
    // 1. 隐藏 navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    try { delete navigator.__proto__.webdriver; } catch(e) {}

    // 2. 伪造 chrome 对象
    if (!window.chrome) {
        window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    }

    // 3. 伪造 Permissions API
    const origQuery = window.navigator.permissions?.query;
    if (origQuery) {
        window.navigator.permissions.query = (params) => (
            params.name === 'notifications' ?
                Promise.resolve({state: Notification.permission}) :
                origQuery(params)
        );
    }

    // 4. 伪造 plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1},
            {name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 1},
        ],
    });

    // 5. 伪造 languages
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

    // 6. 伪造 connection.rtt
    if (navigator.connection) {
        Object.defineProperty(navigator.connection, 'rtt', {get: () => 50});
    }

    // 7. 隐藏 Headless/Automation 相关
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

    // 8. 对抗 PerimeterX CDP 检测
    const cdcProps = Object.getOwnPropertyNames(window).filter(p =>
        p.match(/^cdc_|^__cdc|^_cdp|^__cdp|^chrome_devtools/i)
    );
    cdcProps.forEach(p => { try { delete window[p]; } catch(e) {} });

    // 9. 隐藏 Error.stack 中的 CDP 痕迹
    const origPrepare = Error.prepareStackTrace;
    Error.prepareStackTrace = function(err, stack) {
        const filtered = stack.filter(s => {
            const fn = s.getFunctionName() || '';
            const file = s.getFileName() || '';
            return !fn.includes('cdp') && !file.includes('pptr') &&
                   !file.includes('playwright') && !file.includes('puppeteer');
        });
        if (origPrepare) return origPrepare(err, filtered);
        return err + '\n' + filtered.map(s => '    at ' + s).join('\n');
    };

    // 10. 隐藏 Runtime.evaluate 注入的全局变量
    const origDefineProperty = Object.defineProperty;
    Object.defineProperty = function(obj, prop, desc) {
        if (obj === window && typeof prop === 'string' &&
            (prop.startsWith('cdc_') || prop.startsWith('__cdc'))) {
            return obj;
        }
        return origDefineProperty.call(this, obj, prop, desc);
    };

    // 11. 伪造 window.outerWidth/outerHeight
    if (window.outerWidth === 0) {
        Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth + 16});
    }
    if (window.outerHeight === 0) {
        Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight + 88});
    }

    // 12. 隐藏 Notification.permission 异常
    if (Notification.permission === 'denied') {
        Object.defineProperty(Notification, 'permission', {get: () => 'default'});
    }

    // 13. 对抗 iframe 检测
    const origGetter = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    if (origGetter) {
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() {
                const w = origGetter.get.call(this);
                if (w) {
                    try { Object.defineProperty(w.navigator, 'webdriver', {get: () => undefined}); } catch(e) {}
                }
                return w;
            }
        });
    }
"""

# Keep the page supplement narrow.  The profile provider owns plugins,
# language, hardware, canvas and WebGL; overriding those values in JavaScript
# made every concurrent worker look identical and could contradict its proxy.
NATIVE_STEALTH_JS = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  try { delete navigator.__proto__.webdriver; } catch (e) {}
  if (!window.chrome) {
    window.chrome = {runtime: {}, loadTimes() {}, csi() {}, app: {}};
  }
  for (const prop of Object.getOwnPropertyNames(window)) {
    if (/^cdc_|^__cdc|^_cdp|^__cdp|^chrome_devtools/i.test(prop)) {
      try { delete window[prop]; } catch (e) {}
    }
  }
  try {
    if (window.outerWidth === 0) {
      Object.defineProperty(window, 'outerWidth', {get: () => innerWidth + 16});
    }
    if (window.outerHeight === 0) {
      Object.defineProperty(window, 'outerHeight', {get: () => innerHeight + 88});
    }
  } catch (e) {}
})();
"""


async def inject_stealth(context, page):
    """Hide automation markers without replacing the native fingerprint."""
    install_shutdown_guard()
    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": NATIVE_STEALTH_JS})
    except Exception as e:
        print(f"  stealth CDP inject failed: {e}")
    try:
        await page.evaluate(NATIVE_STEALTH_JS)
    except Exception:
        pass
    try:
        await context.add_init_script(NATIVE_STEALTH_JS)
    except Exception:
        pass
    print("  native fingerprint preserved; automation markers hidden")


def _prepare_browser_options(browser_options=None):
    options = dict(browser_options or {})
    worker = active_worker()
    platform = worker.platform if worker else "browser"
    native = browser_fingerprint(platform)
    configured = dict(options.get("browserFingerPrint") or {})
    native.update(configured)
    options["browserFingerPrint"] = native
    if worker:
        options.setdefault(
            "remark",
            f"reg-factory {platform} worker={worker.index} slot={worker.slot}",
        )
    return options


def create_browser_with_retry(bb, name, retries=3, **browser_options):
    """创建 BitBrowser 窗口，带配额满自动清理 / 网络错误重试"""
    import time
    for attempt in range(retries):
        try:
            return bb.create_browser(name=name, **browser_options)
        except Exception as e:
            msg = str(e)
            if any(k in msg.lower() for k in ["最大创建窗口数", "超过", "quota", "limit", "maximum", "exceed"]):
                print("  窗口数量已满；为避免误删已有浏览器资料，已停止自动清理")
                return None
            if any(k in msg.lower() for k in ["tls", "socket", "econnreset", "network", "timeout"]):
                print(f"  create browser network error (retry {attempt+1}/{retries}): {msg[:80]}")
                time.sleep(5)
                continue
            raise
    return None


async def open_and_connect(name, p=None, browser_options=None):
    """创建并打开 BitBrowser 窗口，连接 Playwright 并注入 stealth。
    返回 (bb, profile_id, browser, context, page)。
    注意：调用方需自行管理 async_playwright 生命周期，或传入 p。"""
    browser_options = _prepare_browser_options(browser_options)
    bb = BitBrowser()
    pid = create_browser_with_retry(bb, name, **(browser_options or {}))
    if not pid:
        raise RuntimeError("create browser failed after retries")
    # open 也可能遇到 BitBrowser TLS 抖动，多重试几次（BitBrowser API 不稳）
    data = None
    max_open = 10
    for attempt in range(max_open):
        try:
            data = bb.open_browser(pid)
            break
        except Exception as e:
            msg = str(e)
            if any(k in msg.lower() for k in ["tls", "socket", "econnreset", "network", "timeout", "disconnected", "未知错误", "正在打开", "opening", "启动中", "starting", "busy"]):
                print(f"  open browser retry (retry {attempt+1}/{max_open}): {msg[:80]}")
                await asyncio.sleep(3)
                continue
            raise
    if not data:
        raise RuntimeError("open browser failed after retries")
    ws = data["ws"]
    print(f"  ws: {ws}")
    browser = await p.chromium.connect_over_cdp(ws)
    context = browser.contexts[0]
    # BitBrowser can expose a zero-sized startup/navigation tab while its
    # profile window is still settling. Install routing before creating a clean
    # registration tab and close only the stale startup pages.
    await install_traffic_saver(context)
    startup_pages = list(context.pages)
    page = await asyncio.wait_for(context.new_page(), timeout=20)
    for startup_page in startup_pages:
        try:
            await asyncio.wait_for(startup_page.close(), timeout=5)
        except Exception:
            pass
    # 强制英文界面：代理 IP 地区（如马来/法国）会让 OpenAI/x.ai 按 Accept-Language
    # 返回本地化 UI（马来语 'Teruskan'/'Selesaikan...'），导致按钮文本匹配失效。
    # stealth 只改 navigator.languages（页面内 JS），改不了 HTTP 请求头，故在此统一固定。
    try:
        await context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
    except Exception as e:
        print(f"  set Accept-Language failed: {e}")
    await inject_stealth(context, page)
    return bb, pid, browser, context, page


async def teardown(bb, profile_id, delete=True):
    """关闭并（可选）删除窗口"""
    if hasattr(bb, "close_browser_async"):
        try:
            await bb.close_browser_async(profile_id)
        except Exception:
            pass
        if delete:
            try:
                bb.delete_browser(profile_id)
            except Exception:
                pass
        return
    try:
        bb.close_browser(profile_id)
    except Exception:
        pass
    await asyncio.sleep(2)
    if delete:
        try:
            bb.delete_browser(profile_id)
        except Exception:
            pass


async def human_type(page, selector, text, delay_range=(0.05, 0.18)):
    """模拟人工逐字输入"""
    el = page.locator(selector).first
    await el.click()
    for ch in text:
        await el.type(ch, delay=random.uniform(*delay_range) * 1000)
    await asyncio.sleep(random.uniform(0.2, 0.5))


async def react_fill(page, selector, text, tries=3, delay=55, verbose=True, settle=0.6):
    """填 React 受控输入，确保框架 state 真正更新。

    坑：page.fill()/locator.fill() 只写 DOM 的 .value，不触发 React 的合成
    onChange，React 内部 state 仍为空 —— 回读 input_value() 却能读到（读的是
    DOM .value），看似成功，提交时 React 用空 state -> 例如 ChatGPT 出现 ?email= 空提交。

    解法：
      1) 键盘逐字输入(page.keyboard.type) —— 触发真实 keydown/input 事件，React 收得到；
      2) 仍不一致则 JS 原生 setter + 派发 input/change 事件兜底；
    每轮回读 input_value() 校验。返回是否成功写入。

    settle: 键入后等 React 同步再回读的秒数。邮箱等关键字段用默认 0.6 求稳；
            onboarding 本地字段可传小值（如 0.15）消除"输完名字停顿很久才输年龄"的卡顿。"""
    el = page.locator(selector).first
    try:
        if await el.count() == 0:
            return False
    except Exception:
        return False

    async def _readback():
        try:
            return (await el.input_value()).strip()
        except Exception:
            return ""

    for i in range(tries):
        # 1) 键盘逐字输入（清空后真实键入，触发 React onChange）
        # 给 click/press 设短 timeout：默认 actionability 超时是 30s，若字段被遮罩
        # （如 name 框的 autocomplete 浮层盖住 age 框）会干等 30s。超时就跳过键入走 JS setter 兜底。
        try:
            await el.click(timeout=4000)
            await el.press("Control+A", timeout=2000)
            await el.press("Delete", timeout=2000)
            await page.keyboard.type(text, delay=delay)
        except Exception as e:
            if verbose:
                print(f"  [react_fill] keyboard path skipped: {str(e)[:50]}")
        await asyncio.sleep(settle)
        if await _readback() == text:
            return True

        # 2) JS 原生 setter 兜底（绕过 React 重写的 value setter，再派发事件让 React 同步）
        try:
            await el.evaluate(
                """(node, v) => {
                    const proto = node.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(node, v);
                    node.dispatchEvent(new Event('input', {bubbles: true}));
                    node.dispatchEvent(new Event('change', {bubbles: true}));
                }""", text)
        except Exception:
            pass
        await asyncio.sleep(settle)
        if await _readback() == text:
            return True

        if verbose:
            print(f"  [react_fill] not committed (got '{await _readback()}'), retry {i+1}/{tries}")

    return False
