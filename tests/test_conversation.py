"""The (question, answer) pairing behind the memory thread."""

import unittest

from jake.conversation import exchanges


def U(text):
    return {"role": "user", "content": text}


def A(text):
    return {"role": "assistant", "content": text}


class Exchanges(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(exchanges([]), [])

    def test_single_pair(self):
        self.assertEqual(exchanges([U("hi"), A("yo")]), [("hi", "yo")])

    def test_pending_question_pairs_with_blank(self):
        self.assertEqual(exchanges([U("hi")]), [("hi", "")])

    def test_two_questions_in_a_row(self):
        # an interrupted question still shows up, paired with an empty answer
        got = exchanges([U("a"), U("b"), A("c")])
        self.assertEqual(got, [("a", ""), ("b", "c")])

    def test_multiple_turns_ordered_oldest_first(self):
        got = exchanges([U("q1"), A("a1"), U("q2"), A("a2")])
        self.assertEqual(got, [("q1", "a1"), ("q2", "a2")])


if __name__ == "__main__":
    unittest.main()
