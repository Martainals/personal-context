from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = REPO_ROOT / "personal-context" / "scripts" / "providers"
sys.path.insert(0, str(PROVIDERS))

from transcript_assembly import (  # noqa: E402
    assemble_transcript_segments,
    restore_asr_punctuation,
)


SETTINGS = {
    "boundary_join_gap_seconds": 0.08,
    "boundary_max_shift_characters": 2,
    "boundary_max_shift_seconds": 1.0,
    "boundary_pause_seconds": 0.35,
    "max_fragment_characters": 2,
    "max_fragment_gap_seconds": 0.2,
    "max_fragment_margin": 0.2,
    "max_fragment_seconds": 0.8,
    "max_same_speaker_gap_seconds": 2.0,
    "max_segment_characters": 200,
    "sentence_pause_seconds": 0.8,
}
DIARIZATION = [
    {"start": 0.0, "end": 30.0, "speaker": 0, "confidence": 0.99, "margin": 0.9}
]
PUNCTUATION = set("，。！？；：、,.!?;:‘’“”\"'（）()《》【】[]—…")


def timed_words(value: str) -> list[dict[str, object]]:
    result = []
    clock = 0.0
    for character in value:
        if character.isspace() or character in PUNCTUATION:
            continue
        result.append({"start": clock, "end": clock + 0.18, "text": character})
        clock += 0.18
    return result


class PunctuationRestorationTests(unittest.TestCase):
    def test_restores_asr_punctuation_and_splits_sentences(self) -> None:
        asr = "童年塑造灵魂。我们想记录回忆。"
        restored, details = restore_asr_punctuation(asr, timed_words(asr))
        segments = assemble_transcript_segments(restored, [], DIARIZATION, SETTINGS)
        self.assertEqual("".join(item["text"] for item in segments), asr)
        self.assertEqual(
            [item["text"] for item in segments],
            ["童年塑造灵魂。", "我们想记录回忆。"],
        )
        self.assertEqual(details["restored"], 2)

    def test_closing_quote_still_ends_the_sentence(self) -> None:
        asr = "她问：“你记得吗？”我说，记得。"
        restored, _ = restore_asr_punctuation(asr, timed_words(asr))
        segments = assemble_transcript_segments(restored, [], DIARIZATION, SETTINGS)
        self.assertEqual(
            [item["text"] for item in segments],
            ["她问：“你记得吗？”", "我说，记得。"],
        )

    def test_long_pause_splits_a_sentence_without_punctuation(self) -> None:
        words = timed_words("前半句后半句")
        for item in words[3:]:
            item["start"] = float(item["start"]) + 0.8
            item["end"] = float(item["end"]) + 0.8

        segments = assemble_transcript_segments(words, [], DIARIZATION, SETTINGS)

        self.assertEqual([item["text"] for item in segments], ["前半句", "后半句"])

    def test_small_alignment_difference_never_invents_missing_words(self) -> None:
        asr = "我想把这些回忆，整理成一个网站。"
        restored, details = restore_asr_punctuation(
            asr, timed_words("我想把些回忆整理成一个网站")
        )
        text = "".join(item["text"] for item in restored)
        self.assertEqual(details["status"], "restored")
        self.assertNotIn("这", text)
        self.assertIn("，", text)
        self.assertTrue(text.endswith("。"))

    def test_unrelated_alignment_fails_closed(self) -> None:
        restored, details = restore_asr_punctuation("童年回忆。", timed_words("完全不同"))
        self.assertEqual(details["status"], "low_similarity")
        self.assertEqual("".join(item["text"] for item in restored), "完全不同")


if __name__ == "__main__":
    unittest.main()
