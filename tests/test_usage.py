"""Offline checks for muse_cli.usage. Run: python -m unittest discover -s tests"""
import json
import os
import tempfile
import unittest

from muse_cli import usage
from muse_cli.gateway import AuthError

A = "4" + "a" * 41
B = "4" + "b" * 41


def ref(action_id, name):
    return (f'(0,d.createServerReference)("{action_id}",d.callServer,void 0,'
            f'd.findSourceMapURL,"{name}")')


class FindActionId(unittest.TestCase):
    def test_adjacent_calls_pick_the_named_one(self):
        js = f"let y={ref(A, 'otherAction')},f={ref(B, usage.ACTION_NAME)};"
        self.assertEqual(usage.find_action_id(js), B)

    def test_name_before_id_of_next_call_is_not_matched(self):
        js = f"let y={ref(A, usage.ACTION_NAME)},f={ref(B, 'otherAction')};"
        self.assertEqual(usage.find_action_id(js), A)

    def test_missing_name(self):
        self.assertIsNone(usage.find_action_id(f"let y={ref(A, 'otherAction')};"))


class ParseSubscription(unittest.TestCase):
    def reply(self, payload):
        return '0:{"a":"$@1"}\n1:' + json.dumps(payload) + "\n"

    def test_service_failure_is_not_an_auth_error(self):
        with self.assertRaises(RuntimeError) as cm:
            usage.parse_subscription(self.reply({"success": False, "error": "Billing down"}))
        self.assertNotIsInstance(cm.exception, AuthError)

    def test_missing_usage_is_rejected(self):
        with self.assertRaises(RuntimeError):
            usage.parse_subscription(self.reply({"success": True, "subscription": {"tier": {}}}))

    def test_unexpected_format(self):
        with self.assertRaises(RuntimeError):
            usage.parse_subscription("not an rsc reply")

    def test_valid(self):
        sub = usage.parse_subscription(self.reply(
            {"success": True, "subscription": {"usage": {"percentUsed": 3}}}))
        self.assertEqual(sub["usage"]["percentUsed"], 3)


class Cache(unittest.TestCase):
    def test_bad_cache_contents_fall_back(self):
        for content in ("null", "[]", '{"action_id": null}', '{"action_id": "nothex"}', "{"):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "c.json")
                with open(p, "w") as fh:
                    fh.write(content)
                self.assertEqual(usage._load_cached_id(p), usage.DEFAULT_ACTION_ID, content)

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "c.json")
            usage._save_cached_id(p, B)
            self.assertEqual(usage._load_cached_id(p), B)


if __name__ == "__main__":
    unittest.main()
