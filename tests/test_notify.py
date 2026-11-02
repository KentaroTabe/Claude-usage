import unittest

from claude_usage.notify import applescript_string


class AppleScriptStringTest(unittest.TestCase):
    def test_quotes_and_newlines(self):
        self.assertEqual(applescript_string('週次上限 "78%"\n最多 a\\b'), '"週次上限 \\"78%\\" 最多 a\\\\b"')


if __name__ == "__main__":
    unittest.main()
