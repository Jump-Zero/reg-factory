import unittest
from unittest.mock import MagicMock

from common.playwright_runtime import install_shutdown_guard


class PlaywrightRuntimeTests(unittest.TestCase):
    def test_target_closed_future_is_consumed(self):
        target_closed_error = type("TargetClosedError", (Exception,), {})
        loop = MagicMock()
        loop.get_exception_handler.return_value = None
        install_shutdown_guard(loop)
        handler = loop.set_exception_handler.call_args.args[0]

        handler(loop, {"exception": target_closed_error("browser has closed")})

        loop.default_exception_handler.assert_not_called()

    def test_unrelated_future_uses_previous_handler(self):
        previous = MagicMock()
        loop = MagicMock()
        loop.get_exception_handler.return_value = previous
        install_shutdown_guard(loop)
        handler = loop.set_exception_handler.call_args.args[0]
        context = {"exception": RuntimeError("unexpected")}

        handler(loop, context)

        previous.assert_called_once_with(loop, context)


if __name__ == "__main__":
    unittest.main()
