import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from common import human_mouse, outlook_press


class OutlookPressTests(unittest.IsolatedAsyncioTestCase):
    def test_box_in_viewport_rejects_offscreen_hidden_frame(self):
        self.assertFalse(
            outlook_press._box_in_viewport(
                {"x": -9999, "y": 100, "width": 300, "height": 180},
                (1414, 792),
                min_width=30,
                min_height=8,
            )
        )
        self.assertTrue(
            outlook_press._box_in_viewport(
                {"x": 420, "y": 100, "width": 300, "height": 180},
                (1414, 792),
                min_width=30,
                min_height=8,
            )
        )

    async def test_find_hold_target_skips_offscreen_frame_button(self):
        hidden_button = MagicMock()
        hidden_button.count = AsyncMock(return_value=1)
        hidden_button.bounding_box = AsyncMock(
            return_value={"x": -9999, "y": 100, "width": 300, "height": 80}
        )
        visible_button = MagicMock()
        visible_button.count = AsyncMock(return_value=1)
        visible_button.bounding_box = AsyncMock(
            return_value={"x": 430, "y": 300, "width": 300, "height": 80}
        )
        hidden_frame = MagicMock(url="https://captcha.hsprotect.net/old")
        hidden_frame.locator.return_value.first = hidden_button
        visible_frame = MagicMock(url="https://captcha.hsprotect.net/current")
        visible_frame.locator.return_value.first = visible_button
        page = MagicMock()
        page.main_frame = page
        page.frames = [page, hidden_frame, visible_frame]
        page.evaluate = AsyncMock(return_value={"width": 1414, "height": 792})
        page.locator.return_value.count = AsyncMock(return_value=0)

        box, is_button = await outlook_press.find_hold_target(page)

        self.assertTrue(is_button)
        self.assertEqual(box["x"], 430)

    async def test_offscreen_mouse_target_is_not_counted_as_a_press(self):
        page = MagicMock()
        with (
            patch.object(
                outlook_press,
                "find_hold_target",
                AsyncMock(return_value=(
                    {"x": -9999, "y": 100, "width": 300, "height": 80},
                    True,
                )),
            ),
            patch.object(
                human_mouse,
                "human_press_and_hold",
                AsyncMock(side_effect=ValueError("mouse target is outside viewport")),
            ),
        ):
            result = await outlook_press.press_and_hold(page, press_number=2)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
