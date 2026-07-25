import unittest
from unittest.mock import patch

from src.security_runner import render_report


class SecurityRunnerTests(unittest.TestCase):
    @patch("src.security_runner.subprocess.run")
    def test_report_name_is_a_literal_argument(self, mocked_run) -> None:
        mocked_run.return_value.stdout = "ok"

        self.assertEqual(render_report("quarterly; whoami"), "ok")

        command = mocked_run.call_args.args[0]
        keyword_arguments = mocked_run.call_args.kwargs
        self.assertEqual(command, ["fixture-report", "--name", "quarterly; whoami"])
        self.assertFalse(keyword_arguments.get("shell", False))


if __name__ == "__main__":
    unittest.main()
