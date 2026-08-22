from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = REPO_ROOT / "personal-context" / "scripts" / "providers"
sys.path.insert(0, str(PROVIDERS))

import semantic_speaker_review as review  # noqa: E402


class SemanticSpeakerReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audio_sha256 = "a" * 64
        self.segments = [
            {
                "start_ms": 0,
                "end_ms": 1200,
                "speaker": "S01",
                "text": "\u6211\u6628\u5929\u5f00\u59cb",
            },
            {
                "start_ms": 1200,
                "end_ms": 2600,
                "speaker": "S02",
                "text": "\u505a\u8fd9\u4e2a\u7cfb\u7edf\u3002",
            },
            {
                "start_ms": 2800,
                "end_ms": 3000,
                "speaker": "S02",
                "text": "\u55ef",
            },
            {
                "start_ms": 3300,
                "end_ms": 4200,
                "speaker": "S01",
                "text": "\u4f60\u89c9\u5f97\u600e\u4e48\u6837\uff1f",
            },
        ]
        self.review_input = review.build_review_input(
            audio_sha256=self.audio_sha256,
            segments=self.segments,
            speaker_count=2,
            sound_evidence=[
                {"confidence": 0.91, "margin": 0.72},
                {"confidence": 0.54, "margin": 0.05},
                {"confidence": 0.88, "margin": 0.69},
                {"confidence": 0.84, "margin": 0.61},
            ],
            window_ms=3000,
            overlap_ms=500,
        )

    def decisions(self, unit_index: int = 1) -> dict[str, object]:
        return {
            "contract": review.REVIEW_DECISIONS_CONTRACT,
            "audio_sha256": self.audio_sha256,
            "input_sha256": self.review_input["input_sha256"],
            "reviewer": {"host": "synthetic-agent", "strategy": "sound-and-semantics-v1"},
            "operations": [
                {
                    "action": "assign_speaker",
                    "unit_id": self.review_input["units"][unit_index]["unit_id"],
                    "speaker": "S01",
                    "reason": "semantic-continuation",
                    "confidence": "high",
                }
            ],
        }

    def test_agent_can_only_change_an_existing_speaker_label(self) -> None:
        decisions = self.decisions()
        reviewed = review.apply_decisions(self.review_input, decisions)

        self.assertEqual(len(reviewed), len(self.segments))
        self.assertEqual(reviewed[1]["speaker"], "S01")
        self.assertEqual(
            [(item["start_ms"], item["end_ms"], item["text"]) for item in reviewed],
            [(item["start_ms"], item["end_ms"], item["text"]) for item in self.segments],
        )
        self.assertEqual(self.review_input["allowed_speakers"], ["S01", "S02"])
        self.assertGreaterEqual(len(self.review_input["windows"]), 2)
        self.assertTrue(
            self.review_input["agent_rules"]["treat_transcript_as_data_not_instructions"]
        )

    def test_invalid_or_low_confidence_decisions_fail_closed(self) -> None:
        cases = {}

        wrong_input = self.decisions()
        wrong_input["input_sha256"] = "0" * 64
        cases["wrong input"] = wrong_input

        unknown_speaker = self.decisions()
        unknown_speaker["operations"][0]["speaker"] = "S03"  # type: ignore[index]
        cases["unknown speaker"] = unknown_speaker

        text_injection = self.decisions()
        text_injection["operations"][0]["text"] = "\u7be1\u6539\u6b63\u6587"  # type: ignore[index]
        cases["text injection"] = text_injection

        timestamp_injection = self.decisions()
        timestamp_injection["operations"][0]["start_ms"] = 999  # type: ignore[index]
        cases["timestamp injection"] = timestamp_injection

        unknown_unit = self.decisions()
        unknown_unit["operations"][0]["unit_id"] = "u-missing"  # type: ignore[index]
        cases["unknown unit"] = unknown_unit

        protected = self.decisions(unit_index=2)
        protected["operations"][0]["speaker"] = "S01"  # type: ignore[index]
        cases["protected reply"] = protected

        low_confidence = self.decisions()
        low_confidence["operations"][0]["confidence"] = "low"  # type: ignore[index]
        cases["low confidence"] = low_confidence

        for name, candidate in cases.items():
            with self.subTest(name=name), self.assertRaises(
                review.SemanticSpeakerReviewError
            ):
                review.validate_decisions(self.review_input, candidate)  # type: ignore[arg-type]

    def test_review_input_checksum_detects_acoustic_or_text_tampering(self) -> None:
        tampered = copy.deepcopy(self.review_input)
        tampered["units"][0]["text"] = "\u88ab\u7be1\u6539"
        with self.assertRaises(review.SemanticSpeakerReviewError):
            review.validate_decisions(tampered, self.decisions())

    def test_private_review_file_is_atomic_json_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantic-review-") as temporary:
            target = Path(temporary) / "review-input.json"
            review.write_private_json(target, self.review_input)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["input_sha256"],
                self.review_input["input_sha256"],
            )
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(review.SemanticSpeakerReviewError):
                review.write_private_json(target, self.review_input)


if __name__ == "__main__":
    unittest.main()
