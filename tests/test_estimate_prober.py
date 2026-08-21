#!/usr/bin/env python3
"""
Unit tests for estimate_prober's response parsers and outcome classifier.

The classifier is the part of the tool that has to be right: everything else is
plumbing, but a mislabelled outcome either wakes someone at 3am over a sample
that legitimately matched nobody, or -- worse -- stays quiet while estimates
never start. Each case is driven by a recorded response fixture in
tests/fixtures/, including the NEVER_STARTED signature we have actually
observed in production: state PROCESSING, profilesReadSoFar 0, empty error
object, forever.

Standard library only (no pytest needed):
    python -m unittest discover -s tests -v
    python tests/test_estimate_prober.py
"""

from __future__ import annotations

import csv
import json
import tempfile
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import estimate_prober as ep  # noqa: E402  (path set above)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a recorded API response body by fixture name."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def poll_from(name: str, *, http_status: int = 200,
              elapsed: float = 12.0, parse_error: str = "") -> ep.Poll:
    """Parse a recorded estimate response into a Poll."""
    return ep.parse_estimate(load_fixture(name), http_status, elapsed, parse_error)


# ----------------------------------------------------------------------------
# The classifier
# ----------------------------------------------------------------------------
class TestOutcomeClassifier(unittest.TestCase):
    """One outcome per probe, and the right one."""

    def test_result_ready_with_estimate(self) -> None:
        outcome, note = ep.classify_outcome(poll_from("estimate_result_ready"),
                                            timed_out=False)
        self.assertEqual(ep.OUTCOME_WITH_RESULT, outcome)
        self.assertIn("4,275", note)

    def test_result_ready_matching_nobody_is_not_a_fault(self) -> None:
        """Rows read, none matched: sample starvation, and a zero exit."""
        outcome, note = ep.classify_outcome(
            poll_from("estimate_result_ready_empty"), timed_out=False)
        self.assertEqual(ep.OUTCOME_EMPTY, outcome)
        self.assertIn("41,003", note)
        self.assertEqual(0, ep.OUTCOME_EXIT[outcome])

    def test_result_ready_having_read_nothing(self) -> None:
        """RESULT_READY with 0 rows read still completed -- but say the sample
        looks empty, because that is the interesting half of the finding."""
        outcome, note = ep.classify_outcome(
            poll_from("estimate_result_ready_zero_read"), timed_out=False)
        self.assertEqual(ep.OUTCOME_EMPTY, outcome)
        self.assertIn("sample", note.lower())

    def test_never_started_signature(self) -> None:
        """THE signature: timed out at PROCESSING, 0 profiles read, no error."""
        poll = poll_from("estimate_never_started", elapsed=180.0)
        self.assertEqual("PROCESSING", poll.state)
        self.assertEqual(0, poll.profiles_read)
        self.assertFalse(poll.has_error)

        outcome, note = ep.classify_outcome(poll, timed_out=True)
        self.assertEqual(ep.OUTCOME_NEVER_STARTED, outcome)
        self.assertIn("never started", note)
        self.assertNotEqual(0, ep.OUTCOME_EXIT[outcome])

    def test_never_started_needs_the_timeout(self) -> None:
        """The same body mid-run is not yet a fault -- only a timeout makes it
        one, so a fast poll loop cannot cry wolf."""
        outcome, _ = ep.classify_outcome(poll_from("estimate_never_started"),
                                         timed_out=False)
        self.assertNotEqual(ep.OUTCOME_NEVER_STARTED, outcome)

    def test_stalled_when_rows_were_read(self) -> None:
        outcome, note = ep.classify_outcome(
            poll_from("estimate_stalled", elapsed=180.0), timed_out=True)
        self.assertEqual(ep.OUTCOME_STALLED, outcome)
        self.assertIn("12,500", note)
        self.assertNotEqual(0, ep.OUTCOME_EXIT[outcome])

    def test_partial_progress_timing_out_is_stalled_not_never_started(self) -> None:
        outcome, _ = ep.classify_outcome(
            poll_from("estimate_running_partial", elapsed=180.0), timed_out=True)
        self.assertEqual(ep.OUTCOME_STALLED, outcome)

    def test_populated_error_object_wins_over_result_ready(self) -> None:
        """The errored fixture also says RESULT_READY; the error still decides."""
        poll = poll_from("estimate_errored")
        self.assertTrue(poll.is_ready)
        outcome, note = ep.classify_outcome(poll, timed_out=False)
        self.assertEqual(ep.OUTCOME_ERRORED, outcome)
        self.assertIn("Failed to evaluate", note)

    def test_failed_state(self) -> None:
        outcome, note = ep.classify_outcome(poll_from("estimate_failed_state"),
                                            timed_out=False)
        self.assertEqual(ep.OUTCOME_ERRORED, outcome)
        self.assertIn("FAILED", note)

    def test_empty_error_object_is_not_an_error(self) -> None:
        """A healthy response carries error {description: "", traceback: ""}."""
        self.assertFalse(poll_from("estimate_result_ready").has_error)

    def test_http_error_status(self) -> None:
        outcome, note = ep.classify_outcome(
            ep.parse_estimate(None, 404, 1.0), timed_out=False)
        self.assertEqual(ep.OUTCOME_ERRORED, outcome)
        self.assertIn("404", note)

    def test_unparseable_body_is_a_finding(self) -> None:
        outcome, note = ep.classify_outcome(
            ep.parse_estimate(None, 200, 1.0, "unparseable response body"),
            timed_out=False)
        self.assertEqual(ep.OUTCOME_ERRORED, outcome)
        self.assertIn("could not be read", note)

    def test_no_response_at_all(self) -> None:
        outcome, _ = ep.classify_outcome(None, timed_out=True)
        self.assertEqual(ep.OUTCOME_ERRORED, outcome)

    def test_estimated_size_absent_falls_back_to_matched(self) -> None:
        body = {"state": "RESULT_READY", "profilesReadSoFar": 100,
                "profilesMatchedSoFar": 7}
        outcome, _ = ep.classify_outcome(ep.parse_estimate(body), timed_out=False)
        self.assertEqual(ep.OUTCOME_WITH_RESULT, outcome)

    def test_every_outcome_has_an_exit_code(self) -> None:
        outcomes = {ep.OUTCOME_WITH_RESULT, ep.OUTCOME_EMPTY,
                    ep.OUTCOME_NEVER_STARTED, ep.OUTCOME_STALLED,
                    ep.OUTCOME_ERRORED}
        self.assertEqual(outcomes, set(ep.OUTCOME_EXIT))
        self.assertEqual([0, 0], [ep.OUTCOME_EXIT[ep.OUTCOME_WITH_RESULT],
                                  ep.OUTCOME_EXIT[ep.OUTCOME_EMPTY]])
        for bad in (ep.OUTCOME_NEVER_STARTED, ep.OUTCOME_STALLED,
                    ep.OUTCOME_ERRORED):
            self.assertNotEqual(0, ep.OUTCOME_EXIT[bad], bad)

    def test_classification_is_stable_across_the_fixture_set(self) -> None:
        """Every fixture lands on exactly one known outcome."""
        cases = [
            ("estimate_result_ready", False, ep.OUTCOME_WITH_RESULT),
            ("estimate_result_ready_empty", False, ep.OUTCOME_EMPTY),
            ("estimate_result_ready_zero_read", False, ep.OUTCOME_EMPTY),
            ("estimate_never_started", True, ep.OUTCOME_NEVER_STARTED),
            ("estimate_stalled", True, ep.OUTCOME_STALLED),
            ("estimate_errored", False, ep.OUTCOME_ERRORED),
            ("estimate_failed_state", False, ep.OUTCOME_ERRORED),
        ]
        for name, timed_out, expected in cases:
            with self.subTest(fixture=name):
                outcome, _ = ep.classify_outcome(poll_from(name, elapsed=180.0),
                                                 timed_out=timed_out)
                self.assertEqual(expected, outcome)
                self.assertIn(outcome, ep.OUTCOME_EXIT)


# ----------------------------------------------------------------------------
# Estimate response parsing
# ----------------------------------------------------------------------------
class TestEstimateParsing(unittest.TestCase):
    """Read the documented fields, and survive undocumented shapes."""

    def test_documented_fields(self) -> None:
        poll = poll_from("estimate_result_ready", elapsed=11.5)
        self.assertEqual("RESULT_READY", poll.state)
        self.assertEqual(4275, poll.profiles_read)
        self.assertEqual(4275, poll.profiles_matched)
        self.assertEqual(4275, poll.num_rows_to_read)
        self.assertEqual(4275, poll.total_rows)
        self.assertEqual(4275, poll.estimated_size)
        self.assertEqual(0.0, poll.standard_error)
        self.assertEqual("95%", poll.confidence_interval)
        self.assertEqual(11.5, poll.elapsed)

    def test_missing_body_and_keys_do_not_raise(self) -> None:
        for body in (None, {}, {"state": "NEW"}):
            with self.subTest(body=body):
                poll = ep.parse_estimate(body)
                self.assertIsNone(poll.profiles_read)
                self.assertFalse(poll.has_error)

    def test_error_as_bare_string(self) -> None:
        poll = ep.parse_estimate({"state": "FAILED", "error": "boom"})
        self.assertTrue(poll.has_error)
        self.assertEqual("boom", poll.error_description)

    def test_error_null_is_not_an_error(self) -> None:
        self.assertFalse(ep.parse_estimate({"state": "RUNNING",
                                            "error": None}).has_error)

    def test_numeric_strings_are_accepted(self) -> None:
        poll = ep.parse_estimate({"state": "RESULT_READY",
                                  "profilesReadSoFar": "4275",
                                  "estimatedSize": "4275"})
        self.assertEqual(4275, poll.profiles_read)
        self.assertEqual(4275, poll.estimated_size)

    def test_lowercase_state_still_reads_as_ready(self) -> None:
        self.assertTrue(ep.parse_estimate({"state": "result_ready"}).is_ready)

    def test_long_traceback_is_clipped(self) -> None:
        poll = ep.parse_estimate({"state": "FAILED",
                                  "error": {"description": "x" * 5000,
                                            "traceback": ""}})
        self.assertLessEqual(len(poll.error_description), ep.MAX_ERROR_CHARS)


# ----------------------------------------------------------------------------
# Sample status parsing + freshness
# ----------------------------------------------------------------------------
class TestSampleStatus(unittest.TestCase):
    """The endpoint quotes its numbers and spells absence as the string null."""

    SAMPLED_AT = datetime(2020, 8, 1, 17, 57, 57, tzinfo=timezone.utc)

    def test_documented_fixture(self) -> None:
        status = ep.parse_sample_status(load_fixture("sample_status"),
                                        now=self.SAMPLED_AT + timedelta(hours=24))
        self.assertEqual("TASK_FINISHED", status.status)
        self.assertEqual(41003, status.sample_size)      # numRowsToRead
        self.assertEqual(41003, status.total_profiles)   # totalRows
        self.assertEqual(300803, status.doc_count)       # was '"300803"'
        self.assertEqual(47429, status.total_fragment_count)
        self.assertEqual(1.0, status.sampling_ratio)
        self.assertEqual("timestampOrdered_auto", status.merge_strategy)
        self.assertTrue(status.job_running)
        self.assertEqual(self.SAMPLED_AT, status.last_sampled_utc)
        self.assertIsNone(status.last_successful_batch_utc)   # was '"null"'
        self.assertEqual(24.0, status.age_hours)

    def test_fresh_within_threshold(self) -> None:
        status = ep.parse_sample_status(load_fixture("sample_status"),
                                        now=self.SAMPLED_AT + timedelta(hours=24))
        outcome, note = ep.classify_sample(status, max_age_hours=96.0)
        self.assertEqual(ep.SAMPLE_FRESH, outcome)
        self.assertIn("24.0h", note)

    def test_stale_beyond_threshold(self) -> None:
        status = ep.parse_sample_status(load_fixture("sample_status"),
                                        now=self.SAMPLED_AT + timedelta(hours=200))
        outcome, note = ep.classify_sample(status, max_age_hours=96.0)
        self.assertEqual(ep.SAMPLE_STALE, outcome)
        self.assertIn("96h threshold", note)

    def test_threshold_boundary_is_inclusive(self) -> None:
        status = ep.parse_sample_status(load_fixture("sample_status"),
                                        now=self.SAMPLED_AT + timedelta(hours=96))
        self.assertEqual(ep.SAMPLE_FRESH,
                         ep.classify_sample(status, max_age_hours=96.0)[0])

    def test_unknown_age_fails_safe(self) -> None:
        """No lastSampledTimestamp: we cannot assert freshness, so it is stale."""
        status = ep.parse_sample_status(
            load_fixture("sample_status_never_sampled"), now=self.SAMPLED_AT)
        self.assertIsNone(status.last_sampled_utc)
        self.assertIsNone(status.age_hours)
        self.assertFalse(status.job_running)
        outcome, note = ep.classify_sample(status, max_age_hours=96.0)
        self.assertEqual(ep.SAMPLE_STALE, outcome)
        self.assertIn("failing safe", note)

    def test_empty_body(self) -> None:
        status = ep.parse_sample_status(None)
        self.assertIsNone(status.sample_size)
        self.assertEqual(ep.SAMPLE_STALE,
                         ep.classify_sample(status, max_age_hours=96.0)[0])

    def test_boolean_sample_job_running(self) -> None:
        """Documented as a boolean, observed as an object -- accept both."""
        status = ep.parse_sample_status({"sampleJobRunning": True})
        self.assertTrue(status.job_running)

    def test_real_prod_response_shape(self) -> None:
        """Prod deviates from the documented example in two ways, both real:
        the document count comes back as cosmosDocCount (docCount is absent
        entirely), and an idle sampleJobRunning carries only {status}, with no
        submissionTimestamp. Counts here are synthetic."""
        status = ep.parse_sample_status(
            load_fixture("sample_status_prod_shape"),
            now=datetime(2020, 8, 1, 12, 30, 26, tzinfo=timezone.utc))
        self.assertEqual(1_000_000_000, status.doc_count)   # via cosmosDocCount
        self.assertEqual(100_000, status.sample_size)
        self.assertEqual(10_000_000, status.total_profiles)
        self.assertFalse(status.job_running)
        self.assertIsNone(status.job_submitted_utc)
        self.assertEqual(0.0, status.age_hours)

    def test_documented_doc_count_still_wins(self) -> None:
        status = ep.parse_sample_status({"docCount": "5", "cosmosDocCount": "9"})
        self.assertEqual(5, status.doc_count)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
class TestHelpers(unittest.TestCase):

    def test_unquote_value(self) -> None:
        self.assertEqual("300803", ep.unquote_value('"300803"'))
        self.assertEqual("", ep.unquote_value('"null"'))
        self.assertEqual("", ep.unquote_value(None))
        self.assertEqual("TASK_FINISHED", ep.unquote_value("TASK_FINISHED"))

    def test_parse_adobe_timestamp(self) -> None:
        expected = datetime(2020, 8, 1, 17, 57, 57, tzinfo=timezone.utc)
        self.assertEqual(expected, ep.parse_adobe_timestamp("2020-08-01 17:57:57.0"))
        self.assertEqual(expected, ep.parse_adobe_timestamp("2020-08-01T17:57:57Z"))
        self.assertEqual(expected,
                         ep.parse_adobe_timestamp(str(int(expected.timestamp() * 1000))))
        self.assertIsNone(ep.parse_adobe_timestamp("not a date"))
        self.assertIsNone(ep.parse_adobe_timestamp('"null"'))

    def test_mask_id_keeps_only_a_head(self) -> None:
        preview_id = "MDphcHAtMzJiZTAzMjgtM2YzMS00YjY0LThkODQtYWNkMGM0ZmJkYWQz"
        masked = ep.mask_id(preview_id)
        self.assertNotIn(preview_id, masked)
        self.assertTrue(masked.startswith(preview_id[:12]))

    def test_iso_utc(self) -> None:
        self.assertEqual("2020-08-01T17:57:57Z", ep.iso_utc(
            datetime(2020, 8, 1, 17, 57, 57, tzinfo=timezone.utc)))
        self.assertEqual("", ep.iso_utc(None))


# ----------------------------------------------------------------------------
# History row
# ----------------------------------------------------------------------------
class TestHistoryRow(unittest.TestCase):
    """The history file must carry the findings and nothing identifying."""

    def sample_record(self) -> dict:
        return {
            "timestamp_utc": "2026-08-20T09:00:00Z",
            "sandbox": "prod",
            "command": "probe",
            "outcome": ep.OUTCOME_NEVER_STARTED,
            "exit_code": 2,
            "note": "timed out",
            "elapsed_seconds": 180.0,
            "polls": 36,
            "final_state": "PROCESSING",
            "profiles_read": 0,
            "profiles_matched": None,
            "timed_out": True,
            # things that must never reach the file
            "pql": "xEvent.timestamp occurs <= 7 days before now",
            "preview_id": "MDphcHAtMzJiZTAzMjgtM2YzMS00YjY0",
            "org_id": "0123456789ABCDEF@AdobeOrg",
            "poll_records": [{"state": "PROCESSING"}],
        }

    def test_only_declared_columns_are_written(self) -> None:
        row = ep.history_row(self.sample_record())
        self.assertEqual(set(ep.HISTORY_FIELDS), set(row))

    def test_identifiers_do_not_leak(self) -> None:
        blob = " ".join(ep.history_row(self.sample_record()).values())
        for secret in ("AdobeOrg", "MDphcHAt", "xEvent.timestamp"):
            self.assertNotIn(secret, blob, f"{secret} leaked into the history row")

    def test_none_and_bool_rendering(self) -> None:
        row = ep.history_row(self.sample_record())
        self.assertEqual("", row["profiles_matched"])
        self.assertEqual("0", row["profiles_read"])
        self.assertEqual(ep.OUTCOME_NEVER_STARTED, row["outcome"])


class TestFailureStreak(unittest.TestCase):
    """'How long has it been stuck?' is read back out of the history file."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.csv"
        self.addCleanup(self.tmp.cleanup)

    def write(self, rows: list[tuple[str, str, str, str]]) -> None:
        """Write (timestamp, sandbox, command, exit_code) rows."""
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=ep.HISTORY_FIELDS,
                                    restval="", extrasaction="ignore")
            writer.writeheader()
            for stamp, sandbox, command, code in rows:
                writer.writerow({"timestamp_utc": stamp, "sandbox": sandbox,
                                 "command": command, "exit_code": code})

    def test_missing_file(self) -> None:
        self.assertEqual((0, ""), ep.previous_failure_streak(
            self.path, "prod", "probe"))

    def test_all_healthy(self) -> None:
        self.write([("2026-08-21T06:00:00Z", "prod", "probe", "0"),
                    ("2026-08-21T07:00:00Z", "prod", "probe", "0")])
        self.assertEqual((0, ""), ep.previous_failure_streak(
            self.path, "prod", "probe"))

    def test_trailing_failures_are_counted_from_the_first_one(self) -> None:
        self.write([("2026-08-21T05:00:00Z", "prod", "probe", "0"),
                    ("2026-08-21T06:00:00Z", "prod", "probe", "2"),
                    ("2026-08-21T07:00:00Z", "prod", "probe", "2"),
                    ("2026-08-21T08:00:00Z", "prod", "probe", "3")])
        count, since = ep.previous_failure_streak(self.path, "prod", "probe")
        self.assertEqual(3, count)
        self.assertEqual("2026-08-21T06:00:00Z", since)

    def test_an_earlier_recovery_breaks_the_streak(self) -> None:
        """Only the unbroken run at the end counts -- an old outage is over."""
        self.write([("2026-08-20T01:00:00Z", "prod", "probe", "2"),
                    ("2026-08-20T02:00:00Z", "prod", "probe", "2"),
                    ("2026-08-21T06:00:00Z", "prod", "probe", "0"),
                    ("2026-08-21T07:00:00Z", "prod", "probe", "2")])
        count, since = ep.previous_failure_streak(self.path, "prod", "probe")
        self.assertEqual(1, count)
        self.assertEqual("2026-08-21T07:00:00Z", since)

    def test_other_sandboxes_and_subcommands_are_ignored(self) -> None:
        self.write([("2026-08-21T06:00:00Z", "prod", "probe", "2"),
                    ("2026-08-21T06:30:00Z", "dev", "probe", "2"),
                    ("2026-08-21T06:45:00Z", "prod", "sample-status", "2")])
        self.assertEqual(1, ep.previous_failure_streak(
            self.path, "prod", "probe")[0])
        self.assertEqual(1, ep.previous_failure_streak(
            self.path, "prod", "sample-status")[0])

    def test_unparseable_exit_code_breaks_rather_than_invents(self) -> None:
        self.write([("2026-08-21T06:00:00Z", "prod", "probe", "2"),
                    ("2026-08-21T07:00:00Z", "prod", "probe", ""),
                    ("2026-08-21T08:00:00Z", "prod", "probe", "2")])
        self.assertEqual(1, ep.previous_failure_streak(
            self.path, "prod", "probe")[0])

    def test_report_gives_hours_since_the_first_failure(self) -> None:
        self.write([("2026-08-21T06:00:00Z", "prod", "probe", "2"),
                    ("2026-08-21T07:00:00Z", "prod", "probe", "2")])
        record = {"timestamp_utc": "2026-08-21T08:00:00Z", "exit_code": 2}
        fields = ep.report_degradation(
            self.path, record, "prod", "probe",
            now=datetime(2026, 8, 21, 12, 12, tzinfo=timezone.utc))
        self.assertEqual(3, fields["consecutive_failures"])   # 2 prior + this
        self.assertEqual("2026-08-21T06:00:00Z", fields["degraded_since_utc"])
        self.assertEqual(6.2, fields["degraded_hours"])

    def test_first_failure_dates_from_this_run(self) -> None:
        record = {"timestamp_utc": "2026-08-21T08:00:00Z", "exit_code": 2}
        fields = ep.report_degradation(
            self.path, record, "prod", "probe",
            now=datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(1, fields["consecutive_failures"])
        self.assertEqual("2026-08-21T08:00:00Z", fields["degraded_since_utc"])
        self.assertEqual(0.0, fields["degraded_hours"])

    def test_healthy_run_reports_nothing(self) -> None:
        self.write([("2026-08-21T06:00:00Z", "prod", "probe", "2")])
        record = {"timestamp_utc": "2026-08-21T08:00:00Z", "exit_code": 0}
        self.assertEqual({}, ep.report_degradation(self.path, record, "prod",
                                                   "probe"))

    def test_streak_columns_are_in_the_history_schema(self) -> None:
        for column in ("consecutive_failures", "degraded_since_utc",
                       "degraded_hours"):
            self.assertIn(column, ep.HISTORY_FIELDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
