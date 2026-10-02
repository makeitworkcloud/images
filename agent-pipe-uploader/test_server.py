import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary_directory.name)
        cls.artifacts = cls.root / "artifacts"
        cls.artifacts.mkdir()
        cls.profiles = cls.root / "profiles.json"
        cls.profiles.write_text(
            json.dumps(
                {
                    "profiles": {
                        "agent-pipe": {
                            "allowedHosts": ["agent-pipe.s3.us-west-2.amazonaws.com"],
                            "pathPrefixes": ["/deliveries/"],
                            "requiredQueryParameters": ["X-Amz-Algorithm", "X-Amz-Signature"],
                            "maxBytes": 1024,
                        }
                    }
                }
            )
        )
        os.environ["ARTIFACT_ROOT"] = str(cls.artifacts)
        os.environ["PROFILE_CONFIG_PATH"] = str(cls.profiles)
        os.environ["MCP_ALLOWED_HOSTS"] = "agent-pipe-uploader.opencode.svc"
        cls.server = importlib.import_module("server")

    @classmethod
    def tearDownClass(cls):
        cls.temporary_directory.cleanup()

    def test_resolves_existing_artifact(self):
        artifact = self.artifacts / "sample.txt"
        artifact.write_text("test")
        self.assertEqual(self.server.artifact_path("sample.txt", must_exist=True), artifact)

    def test_rejects_artifact_path_escape(self):
        with self.assertRaises(self.server.TransferError):
            self.server.artifact_path("../outside", must_exist=True)

    def test_validates_profile_signed_url(self):
        profile, host, target = self.server.signed_target(
            "agent-pipe",
            "https://agent-pipe.s3.us-west-2.amazonaws.com/deliveries/test.txt?"
            "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=example",
        )
        self.assertEqual(profile["maxBytes"], 1024)
        self.assertEqual(host, "agent-pipe.s3.us-west-2.amazonaws.com")
        self.assertTrue(target.startswith("/deliveries/test.txt?"))

    def test_rejects_unapproved_signed_url(self):
        with self.assertRaises(self.server.TransferError):
            self.server.signed_target(
                "agent-pipe",
                "https://example.com/deliveries/test.txt?"
                "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=example",
            )

    def test_rejects_disallowed_object_prefix(self):
        with self.assertRaises(self.server.TransferError):
            self.server.signed_target(
                "agent-pipe",
                "https://agent-pipe.s3.us-west-2.amazonaws.com/private/test.txt?"
                "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=example",
            )


if __name__ == "__main__":
    unittest.main()
