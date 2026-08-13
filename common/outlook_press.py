# -*- coding: utf-8 -*-
"""Shared Outlook PerimeterX press-and-hold behavior.

Registration and recovery must use this module together. Keeping target
selection and the physical press sequence in one place prevents the two flows
from slowly diverging as Microsoft's challenge markup changes.
"""

from __future__ import annotations

import asyncio
import random

from common import human_mouse


async def _viewport_size(page):
    try:
        value = await page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        if isinstance(value, dict):
            width = float(value.get("width") or 0)
            height = float(value.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
    except Exception:
        pass
    return None


def _box_in_viewport(box, viewport, *, min_width=0, min_height=0):
    if (
        not box
        or box.get("width", 0) <= min_width
        or box.get("height", 0) <= min_height
    ):
        return False
    if not viewport:
        return box.get("x", 0) >= 0 and box.get("y", 0) >= 0
    width, height = viewport
    center_x = float(box.get("x", 0)) + float(box.get("width", 0)) / 2
    center_y = float(box.get("y", 0)) + float(box.get("height", 0)) / 2
    return 0 <= center_x < width and 0 <= center_y < height


async def captcha_visible(page):
    """Return whether an interactive Outlook hold challenge is still visible."""
    viewport = await _viewport_size(page)
    try:
        for selector in (
            'button:has-text("Press and hold")',
            'button:has-text("Appuyer et maintenir")',
            'button:has-text("按住")',
            'button:has-text("长按")',
            'button:has-text("Halten")',
            '#px-captcha',
        ):
            element = page.locator(selector).first
            if await element.count() > 0:
                box = await element.bounding_box()
                if _box_in_viewport(box, viewport, min_width=30, min_height=8):
                    return True

        frames = page.locator(
            'iframe[src*="hsprotect.net"], '
            'iframe[src*="arkose"], '
            'iframe[src*="funcaptcha"]'
        )
        for index in range(await frames.count()):
            box = await frames.nth(index).bounding_box()
            if _box_in_viewport(box, viewport, min_width=50, min_height=30):
                return True
    except Exception:
        pass
    return False


async def find_hold_target(page):
    """Use the target lookup proven by the Outlook registration flow."""
    viewport = await _viewport_size(page)
    refresh_frames = getattr(page, "_refresh_frames", None)
    if refresh_frames:
        try:
            await refresh_frames(force=True)
        except Exception:
            pass
    for frame in page.frames:
        if frame == page.main_frame or "hsprotect.net" not in (frame.url or ""):
            continue
        try:
            button = frame.locator("#px-captcha").first
            if await button.count() > 0:
                box = await button.bounding_box()
                if _box_in_viewport(box, viewport, min_width=30, min_height=8):
                    return box, True
        except Exception:
            pass

    try:
        frames = page.locator('iframe[src*="hsprotect.net"]')
        for index in range(await frames.count()):
            box = await frames.nth(index).bounding_box()
            if _box_in_viewport(box, viewport, min_width=50, min_height=30):
                return box, False
    except Exception:
        pass
    return None, False


async def press_and_hold(page, *, label="", press_number=1):
    """Run one registration-style hold attempt, or return None without a target."""
    target_box, box_is_button = await find_hold_target(page)
    if not target_box:
        return None

    bx = target_box["x"]
    by = target_box["y"]
    bw = target_box["width"]
    bh = target_box["height"]
    if box_is_button:
        cx = bx + bw * random.uniform(0.40, 0.60)
        cy = by + bh * random.uniform(0.40, 0.60)
    else:
        cx = bx + bw * random.uniform(0.42, 0.58)
        cy = by + bh * random.uniform(0.48, 0.62)

    suffix = " [btn]" if box_is_button else " [box]"
    print(f"{label} press #{press_number}: ({cx:.0f},{cy:.0f}){suffix}")

    async def hold_done():
        return not await captcha_visible(page)

    try:
        held, passed = await human_mouse.human_press_and_hold(
            page,
            cx,
            cy,
            is_done=hold_done,
            max_hold=random.uniform(11.0, 15.0),
            min_hold=1.5,
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(f"{label} human_press_and_hold err: {message}")
        message_lower = message.lower()
        if "out of bounds" in message_lower or "outside viewport" in message_lower:
            print(f"{label} discarded off-screen captcha target; rescanning")
            return None
        if "closed" in message_lower or "targetclosed" in message_lower:
            print(f"{label} page/context 已关闭，跳过重按，交外层判定")
            held, passed = 0.0, False
        else:
            try:
                await page.mouse.down()
                await asyncio.sleep(random.uniform(11.0, 14.0))
                await page.mouse.up()
            except Exception:
                pass
            held, passed = 12.0, False

    print(f"{label} held {held:.1f}s{' (passed)' if passed else ''}")
    return {
        "held": held,
        "passed": passed,
        "box_is_button": box_is_button,
        "x": cx,
        "y": cy,
    }
