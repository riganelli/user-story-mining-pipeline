"""Helpers for resuming a phase after an interruption.

``load_resume`` rebuilds the set of already-processed issues from the
pair of result files (accepted / discarded) used by phases 3 and 4.
``load_progress`` loads the JSON file of cumulative counters that each
phase persists via ``utils.atomic_io.dump_json_atomic``.
"""
from __future__ import annotations

import json
import os


def load_resume(output_file: str, discarded_file: str,
                   output_label: str, discarded_label: str
                   ) -> tuple[list, list, set]:
    """Load the two checkpoint files and build the set of processed keys.

    Args:
        output_file: JSON file with the accepted issues.
        discarded_file: JSON file with the discarded issues.
        output_label: description used in the resume log line for the
            main file.
        discarded_label: description used in the resume log line for the
            discarded file.

    Returns:
        ``(output_list, discarded_list, processed_keys)``. The lists are
        empty if the files do not exist; the set contains
        ``"{repo}#{number}"`` keys derived from both lists.
    """
    if os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as f:
            output_list = json.load(f)
        print(f"Resume: {len(output_list)} {output_label}.")
    else:
        output_list = []

    if os.path.exists(discarded_file):
        with open(discarded_file, "r", encoding="utf-8") as f:
            discarded_list = json.load(f)
        print(f"Resume: {len(discarded_list)} {discarded_label}.")
    else:
        discarded_list = []

    processed_issues: set = set()
    for i in output_list:
        processed_issues.add(f"{i['repo']}#{i['number']}")
    for i in discarded_list:
        processed_issues.add(f"{i['repo']}#{i['number']}")

    print(f"Issues already processed: {len(processed_issues)}")

    return output_list, discarded_list, processed_issues


def load_progress(progress_file: str) -> dict:
    """Load a JSON progress file holding cumulative counters.

    Returns an empty dict if the file is missing or unreadable, so a
    corrupt checkpoint restarts the counters instead of crashing the
    phase.
    """
    if not os.path.exists(progress_file):
        return {}
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(f"⚠️  load_progress: {progress_file} is not a dict, ignoring.")
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️  load_progress: could not read {progress_file}: {e}. "
              "Starting counters from scratch.")
        return {}
