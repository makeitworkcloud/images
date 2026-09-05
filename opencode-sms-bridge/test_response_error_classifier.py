import unittest

from server import BRIDGE_ERROR_CODES, classify_response_error, opencode_response_error_code


class ResponseErrorClassifierTests(unittest.TestCase):
    def test_malformed_error_names_fall_back_without_raising(self):
        for name in ([], {}, 1, None):
            with self.subTest(name_type=type(name).__name__):
                error = {"name": name, "data": {"message": "private detail"}}
                self.assertEqual(classify_response_error(error), "unknown")
                self.assertEqual(opencode_response_error_code(error), "opencode-response-error:unknown")

    def test_api_status_and_retryability_are_bounded(self):
        cases = (
            ({"statusCode": 100, "isRetryable": True}, "opencode-response-error:api:100:retryable"),
            ({"statusCode": 599, "isRetryable": False}, "opencode-response-error:api:599:nonretryable"),
            ({"statusCode": 99, "isRetryable": False}, "opencode-response-error:api:no-status:nonretryable"),
            ({"statusCode": 600, "isRetryable": "false"}, "opencode-response-error:api:no-status:unknown"),
        )
        for data, expected in cases:
            with self.subTest(data=data):
                error = {"name": "APIError", "data": {"message": "private detail", **data}}
                code = opencode_response_error_code(error)
                self.assertEqual(code, expected)
                self.assertIn(code, BRIDGE_ERROR_CODES)
                self.assertNotIn("private detail", code)


if __name__ == "__main__":
    unittest.main()
