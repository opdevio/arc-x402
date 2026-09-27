import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import server


class SettlementReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.tmp.name, "settlements.jsonl")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request_paid(self, receipt_result):
        payload = {"payload": {"authorization": {"nonce": "test-nonce"}}}
        header = base64.b64encode(json.dumps(payload).encode()).decode()
        req = Request("http://127.0.0.1:%d%s" % (self.httpd.server_port, server.PAID),
                      headers={"PAYMENT-SIGNATURE": header})
        post_results = iter(({"isValid": True}, {"success": True, "transaction": "0xabc", "payer": "0x123"}))
        with patch.object(server, "SETTLEMENT_LOG", self.log_path), \
             patch.object(server, "_post", side_effect=lambda *args, **kwargs: next(post_results)), \
             patch.object(server, "snapshot", return_value={"paid_payload": "secret"}), \
             patch.object(server, "receipt_pays_us", return_value=receipt_result), \
             patch.object(server, "record", wraps=server.record) as record:
            try:
                response = urlopen(req)
            except HTTPError as exc:
                response = exc
            status = response.code
            body = response.read()
            if receipt_result is False:
                record.assert_not_called()
            else:
                record.assert_called_once()
        return status, json.loads(body)

    def test_mismatched_receipt_rejects_without_record_or_paid_body(self):
        status, body = self.request_paid(False)
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "settlement_receipt_mismatch")
        self.assertNotIn("paid_payload", body)
        self.assertFalse(os.path.exists(self.log_path))

    def test_inconclusive_receipt_allows_facilitator_success(self):
        status, body = self.request_paid(None)
        self.assertEqual(status, 200)
        self.assertEqual(body["paid_payload"], "secret")

    def test_matching_receipt_allows_paid_response(self):
        status, body = self.request_paid(True)
        self.assertEqual(status, 200)
        self.assertEqual(body["paid_payload"], "secret")


if __name__ == "__main__":
    unittest.main()
