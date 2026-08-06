from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
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

    def test_send_photo_uses_multipart_upload(self) -> None:
        session = Mock()
        response = session.post.return_value
        response.json.return_value = {"ok": True, "result": {}}
        client = TelegramClient("test-token:secret", session=session)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chart.png"
            path.write_bytes(b"png-data")

            client.send_photo("123456", path, caption="Portfolio allocation")

        call = session.post.call_args
        self.assertTrue(call.args[0].endswith("/sendPhoto"))
        self.assertEqual(call.kwargs["data"]["chat_id"], "123456")
        self.assertEqual(call.kwargs["data"]["caption"], "Portfolio allocation")
        self.assertEqual(call.kwargs["files"]["photo"][0], "chart.png")


if __name__ == "__main__":
    unittest.main()
