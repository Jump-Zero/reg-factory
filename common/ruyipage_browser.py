"""RuyiPage Firefox backend with the Playwright subset used by shared flows."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import urllib.parse
import uuid
from pathlib import Path

from common.bundled_browser import BundledBrowser


_HAS_TEXT_RE = re.compile(r':has-text\((["\'])(.*?)\1\)', re.IGNORECASE)


def _selector_parts(selector: str) -> tuple[str, list[str], bool]:
    selector = str(selector or "*").strip()
    texts = [match.group(2) for match in _HAS_TEXT_RE.finditer(selector)]
    selector = _HAS_TEXT_RE.sub("", selector)
    visible_only = ":visible" in selector
    selector = selector.replace(":visible", "") or "*"
    return selector, texts, visible_only


async def _element_text(element) -> str:
    values = []
    try:
        values.append(await element.get_text())
    except Exception:
        pass
    for name in ("aria-label", "title", "value", "alt"):
        try:
            values.append(await element.attr(name))
        except Exception:
            pass
    return " ".join(str(value or "") for value in values).strip()


class RuyiLocator:
    def __init__(
        self,
        page,
        selector,
        *,
        index=None,
        has_text=None,
        has_not_text=None,
        exact=False,
    ):
        self.page = page
        self.selector = str(selector or "*")
        self.index = index
        self.has_text = [] if has_text is None else [has_text]
        self.has_not_text = [] if has_not_text is None else [has_not_text]
        self.exact = exact

    def _copy(self, **updates):
        values = {
            "page": self.page,
            "selector": self.selector,
            "index": self.index,
            "exact": self.exact,
        }
        values.update(updates)
        item = type(self)(**values)
        item.has_text = list(updates.get("has_text", self.has_text))
        item.has_not_text = list(updates.get("has_not_text", self.has_not_text))
        return item

    @property
    def first(self):
        return self._copy(index=0)

    @property
    def last(self):
        return self._copy(index=-1)

    def nth(self, index):
        return self._copy(index=int(index))

    def filter(self, *, has_text=None, has_not_text=None):
        item = self._copy()
        if has_text is not None:
            item.has_text.append(has_text)
        if has_not_text is not None:
            item.has_not_text.append(has_not_text)
        return item

    def locator(self, selector, *, has_text=None, has_not_text=None):
        return RuyiChildLocator(
            self,
            selector,
            has_text=has_text,
            has_not_text=has_not_text,
        )

    @staticmethod
    def _matches(value, pattern, *, exact=False):
        if hasattr(pattern, "search"):
            return bool(pattern.search(value))
        expected = str(pattern or "")
        return value.strip() == expected if exact else expected.lower() in value.lower()

    async def _resolve(self, timeout=0.25):
        selector, selector_texts, visible_only = _selector_parts(self.selector)
        try:
            elements = await self.page._ruyi.eles(
                "css:" + selector, timeout=max(0.05, float(timeout))
            )
        except Exception:
            elements = []
        required = [*selector_texts, *self.has_text]
        excluded = list(self.has_not_text)
        if required or excluded or visible_only:
            filtered = []
            for element in elements:
                if visible_only:
                    try:
                        if not await element.get_is_displayed():
                            continue
                    except Exception:
                        continue
                text = await _element_text(element)
                if not all(
                    self._matches(text, value, exact=self.exact)
                    for value in required
                ):
                    continue
                if any(self._matches(text, value) for value in excluded):
                    continue
                filtered.append(element)
            elements = filtered
        if self.index is None:
            return elements
        try:
            return [elements[self.index]]
        except (IndexError, TypeError):
            return []

    async def _one(self, timeout=0.25):
        elements = await self._resolve(timeout=timeout)
        return elements[0] if elements else None

    async def count(self):
        return len(await self._resolve())

    async def click(self, timeout=30000, force=False):
        del force
        element = await self._one(timeout=max(0.1, timeout / 1000))
        if element is None:
            raise RuntimeError(f"RuyiPage element not found: {self.selector}")
        try:
            await element.click_self(timeout=max(0.1, min(timeout / 1000, 3)))
        except Exception:
            await element.click_self(by_js=True)
        await asyncio.sleep(0.15)
        await self.page._refresh_url()

    async def fill(self, value, timeout=30000):
        element = await self._one(timeout=max(0.1, timeout / 1000))
        if element is None:
            raise RuntimeError(f"RuyiPage element not found: {self.selector}")
        await element.input(str(value), clear=True, by_js=False)
        await asyncio.sleep(0.05)

    async def type(self, value, delay=0):
        element = await self._one()
        if element is None:
            raise RuntimeError(f"RuyiPage element not found: {self.selector}")
        await element.input(str(value), clear=False, by_js=False)
        if delay:
            await asyncio.sleep(len(str(value)) * float(delay) / 1000)

    async def press(self, key, timeout=30000):
        del timeout
        element = await self._one()
        if element is None:
            raise RuntimeError(f"RuyiPage element not found: {self.selector}")
        normalized = str(key).lower()
        if normalized in {"control+a", "ctrl+a"}:
            await element.focus()
            await element.run_js("function(){ return this.select && this.select(); }")
            return
        if normalized in {"delete", "backspace"}:
            await element.clear()
            return
        await element.focus()
        await self.page.keyboard.press(key)
        await asyncio.sleep(0.1)
        await self.page._refresh_url()

    async def input_value(self):
        element = await self._one()
        return str(await element.get_value() or "") if element else ""

    async def inner_text(self, timeout=30000):
        element = await self._one(timeout=max(0.1, timeout / 1000))
        return str(await element.get_text() or "") if element else ""

    async def text_content(self, timeout=30000):
        return await self.inner_text(timeout=timeout)

    async def all_inner_texts(self):
        return [str(await element.get_text() or "") for element in await self._resolve()]

    async def get_attribute(self, name):
        element = await self._one()
        return await element.attr(name) if element else None

    async def is_visible(self):
        element = await self._one()
        if element is None:
            return False
        try:
            return bool(await element.get_is_displayed())
        except Exception:
            return False

    async def is_checked(self):
        element = await self._one()
        return bool(await element.get_is_checked()) if element else False

    async def check(self, force=False, timeout=30000):
        del force
        if not await self.is_checked():
            await self.click(timeout=timeout)

    async def select_option(self, value=None, *, label=None, index=None):
        """Select a native option using the Playwright argument forms Outlook uses."""
        if isinstance(value, dict):
            label = value.get("label", label)
            index = value.get("index", index)
            value = value.get("value")
        element = await self._one()
        if element is None:
            raise RuntimeError(f"RuyiPage element not found: {self.selector}")
        selected = await element.run_js(
            """function(value, label, index) {
                const options = Array.from(this.options || []);
                let option = null;
                if (index !== null && index !== undefined) {
                    option = options[Number(index)] || null;
                } else if (label !== null && label !== undefined) {
                    option = options.find(item =>
                        String(item.label || item.textContent || '').trim() === String(label).trim()
                    ) || null;
                } else {
                    option = options.find(item => String(item.value) === String(value)) || null;
                }
                if (!option) return null;
                this.value = option.value;
                option.selected = true;
                this.dispatchEvent(new Event('input', {bubbles: true}));
                this.dispatchEvent(new Event('change', {bubbles: true}));
                return option.value;
            }""",
            value,
            label,
            index,
        )
        if selected is None:
            target = label if label is not None else index if index is not None else value
            raise RuntimeError(f"RuyiPage option not found: {target}")
        return [str(selected)]

    async def evaluate(self, script, arg=None, timeout=30000):
        element = await self._one(timeout=max(0.1, timeout / 1000))
        if element is None:
            raise RuntimeError(f"RuyiPage element not found: {self.selector}")
        wrapped = f"function(){{ return ({script})(this, ...arguments); }}"
        if arg is None:
            return await element.run_js(wrapped)
        return await element.run_js(wrapped, arg)

    async def bounding_box(self):
        element = await self._one()
        if element is None:
            return None
        location = await element.get_location()
        size = await element.get_size()
        offset = getattr(self.page, "_frame_offset", {}) or {}
        return {
            "x": float(location.get("x", 0)) + float(offset.get("x", 0)),
            "y": float(location.get("y", 0)) + float(offset.get("y", 0)),
            "width": float(size.get("width", 0)),
            "height": float(size.get("height", 0)),
        }

    async def wait_for(self, state="visible", timeout=30000):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            count = await self.count()
            visible = await self.is_visible() if count else False
            if state == "attached" and count:
                return
            if state == "visible" and visible:
                return
            if state == "hidden" and not visible:
                return
            if state == "detached" and not count:
                return
            await asyncio.sleep(0.15)
        raise RuntimeError(f"RuyiPage wait_for timeout: {self.selector} ({state})")


class RuyiChildLocator(RuyiLocator):
    def __init__(self, parent, selector, **kwargs):
        super().__init__(parent.page, selector, **kwargs)
        self.parent = parent

    def _copy(self, **updates):
        values = {
            "parent": self.parent,
            "selector": self.selector,
            "index": self.index,
            "exact": self.exact,
        }
        values.update(updates)
        item = type(self)(**values)
        item.has_text = list(updates.get("has_text", self.has_text))
        item.has_not_text = list(updates.get("has_not_text", self.has_not_text))
        return item

    async def _resolve(self, timeout=0.25):
        parents = await self.parent._resolve(timeout=timeout)
        if not parents:
            return []
        scope = RuyiLocatorScope(parents[0], self.page)
        locator = RuyiLocator(
            scope,
            self.selector,
            index=self.index,
            exact=self.exact,
        )
        locator.has_text = list(self.has_text)
        locator.has_not_text = list(self.has_not_text)
        return await locator._resolve(timeout=timeout)

    async def evaluate_all(self, script, arg=None):
        parent = await self.parent._one()
        if parent is None:
            return []
        wrapped = (
            "function(arg){ return (" + str(script) + ")"
            "(Array.from(this.querySelectorAll(" + json.dumps(self.selector) + ")), arg); }"
        )
        return await parent.run_js(wrapped, arg)


class RuyiKeyboard:
    def __init__(self, page):
        self.page = page

    @staticmethod
    def _key(key):
        from ruyipage import Keys

        return {
            "enter": Keys.ENTER,
            "delete": Keys.DELETE,
            "backspace": Keys.BACKSPACE,
            "tab": Keys.TAB,
            "escape": Keys.ESCAPE,
            "arrowdown": Keys.ARROW_DOWN,
            "arrowup": Keys.ARROW_UP,
            "home": Keys.HOME,
            "space": Keys.SPACE,
        }.get(str(key).lower(), str(key))

    async def type(self, text, delay=0):
        actions = await self.page._ruyi.actions.type(str(text), interval=int(delay or 0))
        await actions.perform()

    async def press(self, key):
        actions = await self.page._ruyi.actions.press(self._key(key))
        await actions.perform()
        await asyncio.sleep(0.1)
        await self.page._refresh_url()


class RuyiMouse:
    def __init__(self, page):
        self.page = page

    async def click(self, x, y):
        actions = await self.page._ruyi.actions.move_to((int(x), int(y)))
        actions = await actions.click()
        await actions.perform()

    async def move(self, x, y, steps=1):
        duration = max(8, min(250, int(steps or 1) * 12))
        actions = await self.page._ruyi.actions.move_to(
            (int(x), int(y)), duration=duration
        )
        await actions.perform()

    @staticmethod
    def _button(button):
        return {"left": 0, "middle": 1, "right": 2}.get(str(button).lower(), 0)

    async def down(self, button="left"):
        actions = await self.page._ruyi.actions.hold(button=self._button(button))
        await actions.perform()

    async def up(self, button="left"):
        actions = await self.page._ruyi.actions.release(button=self._button(button))
        await actions.perform()


class RuyiFrameAdapter:
    """Playwright-like frame view backed by a RuyiPage frame context."""

    def __init__(self, raw_frame, *, url="", offset=None):
        self._ruyi = raw_frame
        self._url = str(url or "about:blank")
        self._frame_offset = dict(offset or {"x": 0, "y": 0})

    @property
    def url(self):
        return self._url

    def locator(self, selector, *, has_text=None, has_not_text=None):
        return RuyiLocator(
            self, selector, has_text=has_text, has_not_text=has_not_text
        )

    async def evaluate(self, script, arg=None):
        stripped = str(script).lstrip()
        callable_script = stripped.startswith(("(", "function", "async"))
        if arg is None:
            return await self._ruyi.run_js(
                script, as_expr=False if callable_script else None
            )
        return await self._ruyi.run_js(script, arg, as_expr=False)

    async def _refresh_url(self):
        try:
            self._url = str(await self._ruyi.get_url() or self._url)
        except Exception:
            pass
        return self._url


class RuyiLocatorScope:
    """Keep scoped element/frame queries from swapping the live page backend."""

    def __init__(self, raw_scope, root_page, *, offset=None):
        self._ruyi = raw_scope
        self._root_page = root_page
        self._frame_offset = dict(offset or {})

    async def _refresh_url(self):
        return await self._root_page._refresh_url()


class RuyiFrameLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def locator(self, selector):
        return RuyiFrameElementLocator(self.page, self.selector, selector)


class RuyiFrameElementLocator(RuyiLocator):
    def __init__(self, page, frame_selector, selector, **kwargs):
        super().__init__(page, selector, **kwargs)
        self.frame_selector = frame_selector

    def _copy(self, **updates):
        values = {
            "page": self.page,
            "frame_selector": self.frame_selector,
            "selector": self.selector,
            "index": self.index,
            "exact": self.exact,
        }
        values.update(updates)
        item = type(self)(**values)
        item.has_text = list(updates.get("has_text", self.has_text))
        item.has_not_text = list(updates.get("has_not_text", self.has_not_text))
        return item

    async def _resolve(self, timeout=0.25):
        try:
            frame = await self.page._ruyi.get_frame(
                locator="css:" + self.frame_selector
            )
        except Exception:
            return []
        if not frame:
            return []
        offset = {}
        try:
            box = await self.page.locator(self.frame_selector).first.bounding_box()
            if box:
                offset = {"x": box["x"], "y": box["y"]}
        except Exception:
            pass
        scope = RuyiLocatorScope(frame, self.page, offset=offset)
        locator = RuyiLocator(
            scope,
            self.selector,
            index=self.index,
            exact=self.exact,
        )
        locator.has_text = list(self.has_text)
        locator.has_not_text = list(self.has_not_text)
        return await locator._resolve(timeout=timeout)


class RuyiContext:
    _reg_factory_route_support = False

    def __init__(self, root):
        self.root = root
        self.pages = []

    async def cookies(self):
        cookies = await self.root._ruyi.get_cookies(all_info=True)
        result = []
        for cookie in cookies:
            raw = dict(getattr(cookie, "raw", {}) or {})
            raw["name"] = getattr(cookie, "name", raw.get("name", ""))
            raw["value"] = getattr(cookie, "value", raw.get("value", ""))
            if "expiry" in raw and "expires" not in raw:
                raw["expires"] = raw.pop("expiry")
            result.append(raw)
        return result

    async def clear_cookies(self):
        await self.root._ruyi.delete_cookies()

    async def add_cookies(self, cookies):
        normalized = []
        for cookie in cookies:
            item = dict(cookie)
            if "expires" in item and "expiry" not in item:
                item["expiry"] = item.pop("expires")
            same_site = str(item.get("sameSite") or "").strip().lower()
            if same_site in {"strict", "lax", "none"}:
                item["sameSite"] = same_site
            else:
                item.pop("sameSite", None)
            if isinstance(item.get("expiry"), (int, float)):
                item["expiry"] = int(item["expiry"])
            normalized.append(item)
        await self.root._ruyi.set_cookies(normalized)

    async def set_extra_http_headers(self, headers):
        await self.root._ruyi.network.set_extra_headers(headers)

    async def add_init_script(self, script):
        await self.root._ruyi.add_preload_script(script)

    async def new_page(self):
        tab = await self.root._ruyi.new_tab()
        page = RuyiPageAdapter(tab, context=self, root=False)
        self.pages.append(page)
        return page

    async def route(self, pattern, handler):
        del pattern, handler

    async def unroute(self, pattern, handler):
        del pattern, handler

    async def shutdown(self):
        for page in list(self.pages):
            await page._stop_watcher()


class RuyiBrowserFacade:
    def __init__(self, context):
        self.contexts = [context]


class RuyiPageAdapter:
    def __init__(self, ruyi_page, context=None, root=True):
        self._ruyi = ruyi_page
        self._root = root
        self._closed = False
        self._navigating = False
        self._url = "about:blank"
        self._frames = []
        self._next_frame_refresh = 0.0
        self.context = context or RuyiContext(self)
        if self not in self.context.pages:
            self.context.pages.append(self)
        self.keyboard = RuyiKeyboard(self)
        self.mouse = RuyiMouse(self)
        self._watcher = asyncio.create_task(self._watch_url())

    @property
    def url(self):
        return self._url

    @property
    def main_frame(self):
        return self

    @property
    def frames(self):
        return [self, *self._frames]

    async def _refresh_url(self):
        try:
            self._url = str(await self._ruyi.get_url() or self._url)
        except Exception:
            pass
        return self._url

    async def _refresh_frames(self, *, force=False):
        now = time.monotonic()
        if not force and now < self._next_frame_refresh:
            return self._frames
        self._next_frame_refresh = now + 0.75
        try:
            raw_frames = list(await self._ruyi.get_frames() or [])
        except Exception:
            return self._frames

        viewport = {"width": 0.0, "height": 0.0}
        try:
            value = await self._ruyi.run_js(
                "() => ({width: window.innerWidth, height: window.innerHeight})",
                as_expr=False,
            )
            if isinstance(value, dict):
                viewport = {
                    "width": float(value.get("width") or 0),
                    "height": float(value.get("height") or 0),
                }
        except Exception:
            pass

        frame_elements = []
        try:
            elements = await self._ruyi.eles("css:iframe,frame", timeout=0.1)
            for element in elements:
                location = await element.get_location()
                size = await element.get_size()
                src = str(await element.attr("src") or "")
                x = float(location.get("x", 0))
                y = float(location.get("y", 0))
                width = float(size.get("width", 0))
                height = float(size.get("height", 0))
                visible = (
                    width > 0
                    and height > 0
                    and viewport["width"] > 0
                    and viewport["height"] > 0
                    and 0 <= x + width / 2 < viewport["width"]
                    and 0 <= y + height / 2 < viewport["height"]
                )
                frame_elements.append({
                    "src": src, "x": x, "y": y, "visible": visible,
                })
        except Exception:
            frame_elements = []

        frames = []
        used_elements = set()
        for raw_frame in raw_frames:
            frame = RuyiFrameAdapter(raw_frame)
            await frame._refresh_url()
            frame_host = urllib.parse.urlsplit(frame.url).hostname or ""
            candidates = []
            for element_index, element in enumerate(frame_elements):
                if element_index in used_elements:
                    continue
                element_host = urllib.parse.urlsplit(element["src"]).hostname or ""
                url_match = bool(
                    frame.url
                    and element["src"]
                    and (
                        frame.url == element["src"]
                        or frame.url.startswith(element["src"])
                        or element["src"].startswith(frame.url)
                    )
                )
                host_match = bool(
                    frame_host and element_host and frame_host == element_host
                )
                score = (20 if element["visible"] else 0)
                score += 200 if url_match else 100 if host_match else 0
                candidates.append((score, element_index, element))
            if candidates:
                score, element_index, element = max(
                    candidates, key=lambda item: item[0]
                )
                if score >= 100 or len(candidates) == 1:
                    frame._frame_offset = {
                        "x": element["x"], "y": element["y"],
                    }
                    used_elements.add(element_index)
            frames.append(frame)
        self._frames = frames
        return frames

    async def _watch_url(self):
        while not self._closed:
            if not self._navigating:
                await self._refresh_url()
                await self._refresh_frames()
            await asyncio.sleep(0.25)

    async def _stop_watcher(self):
        self._closed = True
        if self._watcher and self._watcher is not asyncio.current_task():
            self._watcher.cancel()
            try:
                await self._watcher
            except (asyncio.CancelledError, Exception):
                pass

    def locator(self, selector, *, has_text=None, has_not_text=None):
        return RuyiLocator(
            self, selector, has_text=has_text, has_not_text=has_not_text
        )

    def get_by_role(self, role, name=None, exact=False):
        selectors = {
            "button": 'button,[role="button"],input[type="button"],input[type="submit"]',
            "link": 'a,[role="link"]',
            "combobox": 'select,[role="combobox"],input[list]',
            "radio": 'input[type="radio"],[role="radio"]',
            "tab": '[role="tab"]',
        }
        return RuyiLocator(
            self,
            selectors.get(str(role).lower(), f'[role="{role}"]'),
            has_text=name,
            exact=exact,
        )

    def get_by_text(self, text, exact=False):
        return RuyiLocator(self, "*", has_text=text, exact=exact)

    def frame_locator(self, selector):
        return RuyiFrameLocator(self, selector)

    def on(self, event, callback):
        del event, callback

    async def goto(self, url, timeout=30000, wait_until=None):
        wait = "interactive" if wait_until == "domcontentloaded" else "complete"
        seconds = max(0.001, timeout / 1000)
        self._navigating = True
        try:
            await asyncio.wait_for(
                self._ruyi.get(url, wait=wait, timeout=seconds),
                timeout=seconds,
            )
        except asyncio.TimeoutError as error:
            stop_loading = getattr(self._ruyi, "stop_loading", None)
            if stop_loading:
                try:
                    await asyncio.wait_for(stop_loading(), timeout=3)
                except Exception:
                    pass
            raise TimeoutError(
                f"RuyiPage navigation timed out after {timeout} ms: {url}"
            ) from error
        finally:
            self._navigating = False
            await self._refresh_url()

    async def reload(self, timeout=30000, wait_until=None):
        wait = "interactive" if wait_until == "domcontentloaded" else "complete"
        seconds = max(0.001, timeout / 1000)
        self._navigating = True
        try:
            reload_page = getattr(getattr(self._ruyi, "contexts", None), "reload", None)
            if reload_page:
                await asyncio.wait_for(reload_page(wait=wait), timeout=seconds)
            else:
                await self.goto(self.url, timeout=timeout, wait_until=wait_until)
        finally:
            self._navigating = False
            await self._refresh_url()

    async def go_back(self, timeout=30000, wait_until=None):
        del wait_until
        seconds = max(0.001, timeout / 1000)
        self._navigating = True
        try:
            await asyncio.wait_for(self._ruyi.back(), timeout=seconds)
        finally:
            self._navigating = False
            await self._refresh_url()

    async def evaluate(self, script, arg=None):
        stripped = str(script).lstrip()
        callable_script = stripped.startswith(("(", "function", "async"))
        if arg is None:
            return await self._ruyi.run_js(
                script, as_expr=False if callable_script else None
            )
        return await self._ruyi.run_js(script, arg, as_expr=False)

    async def content(self):
        return str(await self._ruyi.get_html() or "")

    async def inner_text(self, selector):
        return await self.locator(selector).inner_text()

    async def screenshot(self, path, full_page=False, timeout=30000, **kwargs):
        del kwargs
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        capture = self._ruyi.screenshot(path=str(target), full_page=full_page)
        if timeout is None or timeout <= 0:
            return await capture
        return await asyncio.wait_for(capture, timeout=float(timeout) / 1000)

    async def bring_to_front(self):
        activate = getattr(self._ruyi, "activate", None)
        if activate:
            await activate()

    async def close(self):
        await self._stop_watcher()
        if not self._root:
            await self._ruyi.close()

    def is_closed(self):
        return self._closed


class RuyiPageBrowser:
    """Profile owner used by ``common.browser.open_and_connect``."""

    provider_name = "ruyipage"

    def __init__(self):
        data_root = Path(
            os.environ.get("REG_FACTORY_DATA_DIR") or Path.cwd() / ".reg-factory-data"
        ).resolve()
        self.profile_root = data_root / "ruyipage-profiles"
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self.profiles = {}
        self.sessions = {}

    def create_browser(self, name="reg_factory", **kwargs):
        profile_id = uuid.uuid4().hex
        proxy = BundledBrowser._proxy_from_fields(kwargs)
        explicit_noproxy = str(kwargs.get("proxyType") or "").lower() == "noproxy"
        if proxy is None and not explicit_noproxy:
            from common import proxy_switch

            effective = proxy_switch.effective_proxy_url()
            proxy = BundledBrowser._proxy_from_fields({"proxy": effective}) if effective else None
        self.profiles[profile_id] = {
            "name": name,
            "proxy": proxy.url if proxy else "",
        }
        return profile_id

    async def open_browser_async(self, profile_id):
        existing = self.sessions.get(str(profile_id))
        if existing:
            _raw_page, page = existing
            return RuyiBrowserFacade(page.context), page.context, page
        try:
            from ruyipage.aio import launch
        except ImportError as exc:
            raise RuntimeError(
                "RuyiPage is unavailable; run: pip install ruyiPage[async]"
            ) from exc
        from common.ruyipage_runtime import ensure_runtime, runtime_status

        configured_path = os.environ.get("RUYIPAGE_BROWSER_PATH", "").strip()
        status = runtime_status(configured_path)
        if status["state"] != "ready":
            print("[ruyipage] Firefox runtime missing; installing automatically...")
            status = await asyncio.to_thread(ensure_runtime, configured_path)
            print(f"[ruyipage] Firefox ready: {status.get('path', '')}")
        browser_path = status["path"]
        profile = self.profiles[str(profile_id)]
        raw_page = await launch(
            browser_path=browser_path,
            user_dir=str(self.profile_root / str(profile_id)),
            proxy=profile.get("proxy") or None,
            headless=False,
            close_on_exit=False,
            window_size=(1440, 900),
        )
        page = RuyiPageAdapter(raw_page)
        await page._refresh_url()
        context = page.context
        browser = RuyiBrowserFacade(context)
        self.sessions[str(profile_id)] = (raw_page, page)
        return browser, context, page

    async def close_browser_async(self, profile_id):
        session = self.sessions.pop(str(profile_id), None)
        if not session:
            return
        raw_page, page = session
        await page.context.shutdown()
        try:
            await raw_page.quit()
        except Exception:
            pass

    def delete_browser(self, profile_id):
        target = (self.profile_root / str(profile_id)).resolve()
        if target.is_relative_to(self.profile_root.resolve()):
            shutil.rmtree(target, ignore_errors=True)
        self.profiles.pop(str(profile_id), None)
