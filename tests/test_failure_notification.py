from __future__ import annotations

import io
import os
import unittest
from unittest.mock import patch

from wallet_rebalancer.failure_notification import (
    TRACKING_FAILURE_MESSAGE,
    main,
)


class TrackingFailureNotificationTests(unittest.TestCase):
    def test_sends_generic_tracking_failure_alert(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token:secret",
            "TELEGRAM_CHAT_ID": "123456",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "wallet_rebalancer.failure_notification.load_dotenv"
            ),
            patch(
                "wallet_rebalancer.failure_notification.TelegramClient"
            ) as client_class,
        ):
            self.assertEqual(main(), 0)

        client_class.assert_called_once_with("test-token:secret")
        client_class.return_value.send_message.assert_called_once_with(
            "123456",
            TRACKING_FAILURE_MESSAGE,
        )
        self.assertIn("investigate the logs", TRACKING_FAILURE_MESSAGE)

    def test_missing_telegram_configuration_fails_safely(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "wallet_rebalancer.failure_notification.load_dotenv"
            ),
            patch(
                "wallet_rebalancer.failure_notification.TelegramClient"
            ) as client_class,
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(main(), 2)

        client_class.assert_not_called()
        self.assertNotIn("secret", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
