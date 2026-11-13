import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import progress  # noqa: E402


class ProgressTest(unittest.TestCase):
    def test_halfway_line_names_the_remaining_time(self) -> None:
        line = progress.format_line(5, 10, 50.0, label="rf", detail="118 KiB/fr")
        self.assertIn("5/10", line)
        self.assertIn("eta 50s", line)
        self.assertIn("118 KiB/fr", line)
        self.assertEqual(line.count("#"), 10)


if __name__ == "__main__":
    unittest.main()
