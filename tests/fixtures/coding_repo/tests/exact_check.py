import unittest
from pathlib import Path


class ExactFileTests(unittest.TestCase):
    def test_exact_binary_contract(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertEqual((repository / "exact.bin").read_bytes(), b"QWEN_CODE_OK")


if __name__ == "__main__":
    unittest.main()
