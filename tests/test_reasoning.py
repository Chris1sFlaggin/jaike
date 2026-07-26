"""The <think>...</think> filter: it must survive tags split across chunks."""

import unittest

from jake.gateway import _partial_tag, _strip_reasoning


def strip(chunks):
    return "".join(_strip_reasoning(chunks))


class StripReasoning(unittest.TestCase):
    def test_plain_text_untouched(self):
        self.assertEqual(strip(["hello ", "world"]), "hello world")

    def test_whole_block_removed(self):
        self.assertEqual(strip(["<think>secret</think>answer"]), "answer")

    def test_text_before_and_after(self):
        self.assertEqual(strip(["a<think>b</think>c"]), "ac")

    def test_open_tag_split_across_chunks(self):
        self.assertEqual(strip(["<th", "ink>x</think>hi"]), "hi")

    def test_close_tag_split_across_chunks(self):
        self.assertEqual(strip(["<think>x</th", "ink>hi"]), "hi")

    def test_unclosed_block_never_leaks(self):
        self.assertEqual(strip(["ok <think>still thinking"]), "ok ")

    def test_character_by_character(self):
        text = "hi <think>no</think> yo"
        self.assertEqual(strip(list(text)), "hi  yo")


class PartialTag(unittest.TestCase):
    def test_matching_suffix(self):
        self.assertEqual(_partial_tag("aa<thi", "<think>"), 4)

    def test_single_char(self):
        self.assertEqual(_partial_tag("<", "<think>"), 1)

    def test_no_match(self):
        self.assertEqual(_partial_tag("hello", "<think>"), 0)


if __name__ == "__main__":
    unittest.main()
