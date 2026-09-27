import hashlib
import http.client
import importlib
import io
import json
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock


URL = "https://objects.example.test/deliveries/deck.pptx?signature=fixture"
BODY = b"deck-data"


def transfer_profile(operations=None):
    return {
        "operations": ["upload", "download", "verify"] if operations is None else operations,
        "allowedHosts": ["objects.example.test"],
        "pathPrefixes": ["/deliveries/"],
        "requiredQueryParameters": ["signature"],
        "maxBytes": 16,
    }


class RetainedTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        config = Path(temporary.name) / "profiles.json"
        config.write_text(json.dumps({"profiles": {"storage": transfer_profile()}}))
        environment = mock.patch.dict(os.environ, {
            "ARTIFACT_ROOT": temporary.name,
            "PROFILE_CONFIG_PATH": str(config),
            "MCP_ALLOWED_HOSTS": "agent-pipe.example.test",
        })
        environment.start()
        cls.addClassCleanup(environment.stop)
        network = mock.patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        network.start()
        cls.addClassCleanup(network.stop)
        cls.server = importlib.import_module("server")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = {
            "storage": transfer_profile(),
            "vendor": transfer_profile(["download", "verify"]),
        }
        for attribute, value in (("ARTIFACT_ROOT", self.root), ("PROFILES", self.profiles), ("CHUNK_SIZE", 4)):
            patcher = mock.patch.object(self.server, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.server, "connection", side_effect=AssertionError("unexpected connection"))
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, body=BODY, headers=None, status=200):
        response = mock.Mock(spec=http.client.HTTPResponse)
        response.status = status
        response.getheaders.return_value = (
            [("Content-Length", str(len(body)))] if headers is None else headers
        )
        response.read.side_effect = io.BytesIO(body).read
        client = mock.Mock()
        client.getresponse.return_value = response
        self.connect.side_effect = None
        self.connect.return_value = client
        return client, response

    def transfer(self, operation, url=URL):
        if operation == "verify":
            return self.server.verify_download("storage", url)
        return self.server.download_artifact("storage", url, "deck.pptx")

    def test_decorated_tools_are_directly_callable(self):
        for name in ("inspect_artifact", "upload_artifact", "verify_download", "download_artifact", "remove_artifact"):
            self.assertTrue(callable(getattr(self.server, name)))

    def test_operations_fail_closed(self):
        for operations in (None, [], "download", {}, ["*"], ["remove"], ["download", "invalid"], [None], [[]], ["download", "download"]):
            for operation in ("upload", "download", "verify"):
                with self.subTest(operations=operations, operation=operation):
                    self.profiles["storage"]["operations"] = operations
                    with self.assertRaises(self.server.TransferError):
                        self.server.signed_target("storage", URL, operation)
        del self.profiles["storage"]["operations"]
        with self.assertRaises(self.server.TransferError):
            self.server.signed_target("storage", URL, "download")
        self.connect.assert_not_called()

    def test_each_transfer_uses_its_distinct_operation(self):
        for allowed in ("upload", "download", "verify"):
            self.profiles["storage"]["operations"] = [allowed]
            self.server.signed_target("storage", URL, allowed)
            for operation in {"upload", "download", "verify"} - {allowed}:
                with self.subTest(allowed=allowed, denied=operation):
                    with self.assertRaises(self.server.TransferError):
                        if operation == "upload":
                            self.server.upload_artifact("storage", "missing", URL)
                        else:
                            self.transfer(operation)
        with self.assertRaises(self.server.TransferError):
            self.server.signed_target("storage", URL, "remove")
        self.connect.assert_not_called()

    def test_vendor_upload_denied_before_network_or_file_access(self):
        with mock.patch.object(self.server, "artifact_parent", side_effect=AssertionError("file access forbidden")):
            with self.assertRaises(self.server.TransferError):
                self.server.upload_artifact("vendor", "missing", URL)
        self.connect.assert_not_called()

    def test_unknown_or_invalid_profile_fails_closed(self):
        for selected in (None, [], "invalid"):
            self.profiles["invalid"] = selected
            with self.assertRaises(self.server.TransferError):
                self.server.signed_target("invalid", URL, "download")
        with self.assertRaises(self.server.TransferError):
            self.server.signed_target("missing", URL, "download")
        for key, value in (("maxBytes", True), ("maxBytes", -1), ("maxBytes", "16"),
                           ("allowedHosts", "objects.example.test"), ("pathPrefixes", []),
                           ("requiredQueryParameters", [])):
            self.profiles["storage"] = transfer_profile()
            self.profiles["storage"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(self.server.TransferError):
                self.server.signed_target("storage", URL, "download")
        self.connect.assert_not_called()

    def test_signed_url_exact_host_and_original_request_target(self):
        url = "https://OBJECTS.EXAMPLE.TEST:443/deliveries/deck%20one.pptx?signature=a%2Bb"
        selected, host, target = self.server.signed_target("vendor", url, "download")
        self.assertEqual(selected["operations"], ["download", "verify"])
        self.assertEqual(host, "objects.example.test")
        self.assertEqual(target, "/deliveries/deck%20one.pptx?signature=a%2Bb")

    def test_rejects_unsafe_urls_and_authorization_parameters(self):
        urls = [
            URL.replace("https:", "http:"), URL.replace(".test/", ".test:444/"),
            URL.replace(".test/", ".test:bad/"), URL.replace("https://", "https://user:password@"),
            URL + "#", URL + "#fragment", URL.replace(".test/", ".test.evil/"),
            URL.replace("/deliveries/", "/private/"),
            URL.replace("/deliveries/", "/deliveries/../"),
            URL.replace("/deliveries/", "/deliveries/%2e%2e/"),
            URL.replace("/deliveries/", "/deliveries/%252e%252e/"),
            URL.replace("/deliveries/", "/deliveries%2f"),
            URL.replace("/deliveries/", "/deliveries//"),
            URL.replace("/deliveries/", "/deliveries/%5c"),
            URL.replace("/deliveries/", "/deliveries/%00"),
            URL.replace("/deliveries/", "/deliveries/%ZZ"),
            URL.replace("signature=fixture", "signature="),
            URL.replace("signature=fixture", "signature=%20"),
            URL.replace("signature=fixture", "signature"),
            URL.replace("signature=fixture", "other=fixture"),
            URL + "&signature=second", URL + "&%73ignature=second", "\n" + URL,
            "https://[invalid/deliveries/a?signature=fixture",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(self.server.TransferError):
                self.server.signed_target("storage", url, "download")
        self.connect.assert_not_called()

    def test_complete_verify_and_download_match_inspection_digest(self):
        expected = hashlib.sha256(BODY).hexdigest()
        client, response = self.response()
        verified = self.server.verify_download("vendor", URL)
        self.assertEqual(verified, {"status": "available", "bytes": len(BODY), "sha256": expected})
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertGreater(response.read.call_count, 1)
        self.assertTrue(all(0 < call.args[0] <= 4 for call in response.read.call_args_list))
        client.close.assert_called_once()
        response.close.assert_called_once()
        self.response()
        downloaded = self.server.download_artifact("vendor", URL, "retained/deck.pptx")
        inspected = self.server.inspect_artifact("retained/deck.pptx")
        self.assertEqual(downloaded, {"status": "downloaded", **inspected})
        self.assertEqual(inspected["sha256"], expected)
        self.assertEqual((self.root / "retained/deck.pptx").read_bytes(), BODY)
        self.assertEqual([path.name for path in (self.root / "retained").iterdir()], ["deck.pptx"])

    def test_zero_length_and_exact_limit(self):
        for body in (b"", b"x" * 16):
            for operation in ("verify", "download"):
                with self.subTest(length=len(body), operation=operation):
                    self.response(body)
                    result = self.transfer(operation)
                    self.assertEqual(result["bytes"], len(body))
                    self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
                    if operation == "download":
                        (self.root / "deck.pptx").unlink()

    def test_rejects_invalid_or_unsupported_framing_before_reading(self):
        cases = [[], [("Content-Length", "-1")], [("Content-Length", "+1")],
                 [("Content-Length", "")], [("Content-Length", "1.0")],
                 [("Content-Length", "1, 1")], [("Content-Length", "1\n")],
                 [("Content-Length", "9" * 21)], [("Content-Length", "17")],
                 [("Content-Length", "1"), ("content-length", "1")],
                 [("Content-Length", "1"), ("Transfer-Encoding", "chunked")],
                 [("Content-Length", "1"), ("Transfer-Encoding", "identity")],
                 [("Transfer-Encoding", "chunked")],
                 [("Content-Length", "1"), ("Content-Encoding", "gzip")]]
        for headers in cases:
            for operation in ("verify", "download"):
                with self.subTest(headers=headers, operation=operation):
                    client, response = self.response(b"x", headers=headers)
                    with self.assertRaises(self.server.TransferError):
                        self.transfer(operation)
                    response.read.assert_not_called()
                    client.close.assert_called_once()
                    self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_short_and_overlong_bodies(self):
        for length in (2, 5):
            for operation in ("verify", "download"):
                with self.subTest(length=length, operation=operation):
                    self.response(b"abc", [("Content-Length", str(length))])
                    with self.assertRaises(self.server.TransferError):
                        self.transfer(operation)
                    self.assertEqual(list(self.root.iterdir()), [])

    def test_short_individual_reads_can_complete(self):
        for operation in ("verify", "download"):
            self.response(b"abc")
            self.connect.return_value.getresponse.return_value.read.side_effect = [b"a", b"b", b"c", b""]
            self.assertEqual(self.transfer(operation)["sha256"], hashlib.sha256(b"abc").hexdigest())

    def test_real_http_response_rejects_truncation_and_duplicate_headers(self):
        wire_responses = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nabc",
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 3\r\n\r\nabc",
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n",
        ]
        for wire in wire_responses:
            socket = mock.Mock()
            socket.makefile.return_value = io.BytesIO(wire)
            response = http.client.HTTPResponse(socket)
            response.begin()
            self.response()
            self.connect.return_value.getresponse.return_value = response
            with self.subTest(wire=wire), self.assertRaises(self.server.TransferError):
                self.server.verify_download("storage", URL)
            self.assertTrue(response.isclosed())

    def test_redirects_partial_content_and_errors_are_not_followed(self):
        for status in (204, 206, 301, 302, 307, 308, 403, 500):
            for operation in ("verify", "download"):
                self.connect.reset_mock()
                client, response = self.response(status=status)
                with self.subTest(status=status, operation=operation), self.assertRaises(self.server.TransferError):
                    self.transfer(operation)
                self.connect.assert_called_once()
                client.request.assert_called_once()
                response.read.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_connection_header_and_body_errors_are_sanitized_and_cleaned(self):
        marker = "private-response-and-signed-query"
        for operation in ("verify", "download"):
            for stage in ("connect", "request", "headers", "read"):
                for error_type in (OSError, http.client.HTTPException):
                    client, response = self.response()
                    error = error_type(marker)
                    if stage == "connect":
                        self.connect.side_effect = error
                    elif stage == "request":
                        client.request.side_effect = error
                    elif stage == "headers":
                        client.getresponse.side_effect = error
                    else:
                        response.read.side_effect = [b"deck", error]
                    with self.subTest(operation=operation, stage=stage, error_type=error_type):
                        try:
                            self.transfer(operation, URL.replace("fixture", marker))
                        except self.server.TransferError:
                            self.assertNotIn(marker, traceback.format_exc())
                        else:
                            self.fail("expected sanitized transfer error")
                        if stage != "connect":
                            client.close.assert_called_once()
                        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_destination_is_not_contacted_or_overwritten(self):
        destination = self.root / "deck.pptx"
        destination.write_bytes(b"original")
        with self.assertRaises(self.server.TransferError):
            self.transfer("download")
        self.connect.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"original")

    def test_concurrent_destination_creation_never_clobbers(self):
        _, response = self.response()
        read = response.read.side_effect
        destination = self.root / "deck.pptx"

        def race(size):
            if not destination.exists():
                destination.write_bytes(b"competitor")
            return read(size)

        response.read.side_effect = race
        with self.assertRaises(self.server.TransferError):
            self.transfer("download")
        self.assertEqual(destination.read_bytes(), b"competitor")
        self.assertEqual([path.name for path in self.root.iterdir()], ["deck.pptx"])

    def test_link_and_disk_write_failures_remove_temporary_files(self):
        self.response()
        with mock.patch.object(self.server.os, "link", side_effect=OSError("private-path")):
            with self.assertRaises(self.server.TransferError):
                self.transfer("download")
        self.assertEqual(list(self.root.iterdir()), [])
        self.response()
        fdopen = os.fdopen

        def broken_output(descriptor, mode):
            output = mock.MagicMock(wraps=fdopen(descriptor, mode))
            output.__enter__.return_value = output
            output.__exit__.side_effect = lambda *args: output.close()
            output.write.side_effect = OSError("private-disk-path")
            return output

        with mock.patch.object(self.server.os, "fdopen", side_effect=broken_output):
            with self.assertRaises(self.server.TransferError):
                self.transfer("download")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_relative_paths_root_directories_and_symlinks_are_refused(self):
        (self.root / "real").mkdir()
        (self.root / "real/file").write_bytes(BODY)
        (self.root / "parent-link").symlink_to(self.root / "real", target_is_directory=True)
        (self.root / "file-link").symlink_to(self.root / "real/file")
        (self.root / "broken-link").symlink_to(self.root / "missing")
        os.mkfifo(self.root / "fifo")
        digest = hashlib.sha256(BODY).hexdigest()
        paths = ["", ".", "..", "/", str(self.root / "real/file"), "../outside", "real/../real/file",
                 "real/./file", "real//file", "real/", "real\\file", "real/\x00file",
                 "real", "parent-link/file", "file-link", "broken-link", "fifo"]
        for path in paths:
            for operation in ("inspect", "remove", "upload", "download"):
                with self.subTest(path=path, operation=operation), self.assertRaises(self.server.TransferError):
                    if operation == "inspect":
                        self.server.inspect_artifact(path)
                    elif operation == "remove":
                        self.server.remove_artifact(path, digest)
                    elif operation == "upload":
                        self.server.upload_artifact("storage", path, URL)
                    else:
                        self.server.download_artifact("storage", URL, path)
        self.connect.assert_not_called()
        self.assertEqual((self.root / "real/file").read_bytes(), BODY)

    def test_upload_is_bounded_and_does_not_read_response_body(self):
        (self.root / "deck.pptx").write_bytes(BODY)
        client, response = self.response(status=201)
        self.assertEqual(self.server.upload_artifact("storage", "deck.pptx", URL),
                         {"status": "uploaded", "bytes": len(BODY)})
        self.assertEqual(b"".join(call.args[0] for call in client.send.call_args_list), BODY)
        response.read.assert_not_called()
        response.close.assert_called_once()
        client.close.assert_called_once()
        self.connect.reset_mock()
        (self.root / "deck.pptx").write_bytes(b"x" * 17)
        with self.assertRaises(self.server.TransferError):
            self.server.upload_artifact("storage", "deck.pptx", URL)
        self.connect.assert_not_called()

    def test_upload_errors_are_sanitized_and_redirects_rejected(self):
        (self.root / "deck.pptx").write_bytes(BODY)
        for status in (302, 403, 500):
            client, response = self.response(status=status)
            with self.assertRaises(self.server.TransferError):
                self.server.upload_artifact("storage", "deck.pptx", URL)
            client.close.assert_called_once()
            response.read.assert_not_called()
        for error in (OSError("private-query"), http.client.HTTPException("private-query")):
            client, _ = self.response()
            client.send.side_effect = error
            try:
                self.server.upload_artifact("storage", "deck.pptx", URL)
            except self.server.TransferError:
                self.assertNotIn("private-query", traceback.format_exc())
            else:
                self.fail("expected sanitized upload error")
            client.close.assert_called_once()

    def test_remove_requires_matching_digest_and_no_profile(self):
        destination = self.root / "deck.pptx"
        destination.write_bytes(BODY)
        for digest in ("", "invalid", "0" * 63, "g" * 64, "0" * 64):
            with self.subTest(digest=digest), self.assertRaises(self.server.TransferError):
                self.server.remove_artifact("deck.pptx", digest)
            self.assertEqual(destination.read_bytes(), BODY)
        expected = self.server.inspect_artifact("deck.pptx")
        self.profiles.clear()
        result = self.server.remove_artifact("deck.pptx", expected["sha256"].upper())
        self.assertEqual(result, {"status": "removed", **expected})
        self.assertFalse(destination.exists())
        with self.assertRaises(self.server.TransferError):
            self.server.remove_artifact("deck.pptx", expected["sha256"])
        self.connect.assert_not_called()

    def test_remove_refuses_replaced_name_after_hashing(self):
        destination = self.root / "deck.pptx"
        destination.write_bytes(BODY)
        read_metadata = self.server.read_metadata

        def replace_after_hash(handle):
            metadata = read_metadata(handle)
            destination.unlink()
            destination.write_bytes(b"replacement")
            return metadata

        with mock.patch.object(self.server, "read_metadata", side_effect=replace_after_hash):
            with self.assertRaises(self.server.TransferError):
                self.server.remove_artifact("deck.pptx", hashlib.sha256(BODY).hexdigest())
        self.assertEqual(destination.read_bytes(), b"replacement")


if __name__ == "__main__":
    unittest.main()
