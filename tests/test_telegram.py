from __future__ import annotations

import unittest
from unittest.mock import Mock

from wallet_rebalancer.telegram import TelegramClient


class TelegramClientTests(unittest.TestCase):
    def test_send_message_passes_html_parse_mode_to_telegram(self) -> None:
        session = Mock()
        response = session.post.return_value
        response.json.return_value = {"ok": True, "result": {}}
        client = TelegramClient("test-token:secret", session=session)

        client.send_message(
            "123456",
            "<pre>planned orders</pre>",
            parse_mode="HTML",
        )

        session.post.assert_called_once()
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["text"], "<pre>planned orders</pre>")


if __name__ == "__main__":
    unittest.main()
