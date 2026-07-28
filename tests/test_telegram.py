from __future__ import annotations

import unittest
from unittest.mock import Mock

from wallet_rebalancer.telegram import TelegramClient


class TelegramClientTests(unittest.TestCase):
    def test_photo_report_is_uploaded_without_plain_text_caption(self) -> None:
        session = Mock()
        session.post.return_value.json.return_value = {"ok": True}
        client = TelegramClient("test-token:secret", session=session)

        client.send_photo("123456", b"png-image")

        session.post.assert_called_once()
        call = session.post.call_args
        self.assertTrue(call.args[0].endswith("/sendPhoto"))
        self.assertEqual(call.kwargs["data"], {"chat_id": "123456"})
        self.assertEqual(
            call.kwargs["files"]["photo"],
            ("rebalance-actions.png", b"png-image", "image/png"),
        )
        self.assertNotIn("caption", call.kwargs["data"])


if __name__ == "__main__":
    unittest.main()
