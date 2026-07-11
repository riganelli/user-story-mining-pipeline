#!/usr/bin/env python3
"""
Extract the literal-match (verbatim) sub-dataset, with optional benefit.

INCLUSION CRITERION
A record enters the sub-dataset if:
  - role    is present and appears verbatim in the issue text;
  - feature is present and appears verbatim in the issue text;
  - benefit is absent OR, when present, appears verbatim in the text.
(role and feature mandatory; benefit optional but, when present, verbatim.)

"verbatim" = the normalized field string (from ai_validation) is a
substring of the normalized concatenation of issue_title + issue_body.

NORMALIZATION (two switches, at the top of the file)
  CASE_INSENSITIVE  True  -> comparison ignores letter case
                            (the published criterion)
                    False -> case-sensitive comparison
  STRIP_MARKDOWN    False -> no transformation beyond whitespace
                            (the published criterion)
                    True  -> strips Markdown formatting characters
                            (* _ ` ~) before comparing, so that records
                            whose only mismatch is markup are not excluded
  In all cases whitespace (\\r\\n, newlines, multiple spaces) -> single space.

PARTITION (to rebuild the dataset partition table under a given criterion)
  full                  role, feature and benefit all verbatim         -> INCLUDED
  partial               role and feature verbatim, benefit absent      -> INCLUDED
  benefit_reformulated  role and feature verbatim, benefit present but not verbatim -> excluded
  core_reformulated     role or feature present but not verbatim       -> excluded
  core_missing          role or feature absent                         -> excluded
  Sub-dataset = full + partial.

Usage:
    python3 extract_exact_match_benefit_opt.py <run_dir_1> [<run_dir_2> ...] -o <output_dir>

Output:
    <output_dir>/exact_match_dataset/<run>/<language>/<repo>/<issue>/data.json
    <output_dir>/exact_match_dataset.json   (included records, no diff, with _class)
    <output_dir>/exact_match_stats.json     (partition and counts)
"""

import argparse
import glob
import json
import os
import re
import shutil

# --- criterion switches ---
CASE_INSENSITIVE = True
STRIP_MARKDOWN = False

CLASSES = ["full", "partial", "benefit_reformulated",
           "core_reformulated", "core_missing"]
INCLUDED = {"full", "partial"}


def norm(s):
    """Normalize whitespace (+ optional markdown). Also handles list fields."""
    if isinstance(s, list):
        s = " ".join(str(x) for x in s)
    if s is None:
        return ""
    s = str(s)
    if STRIP_MARKDOWN:
        s = re.sub(r"[*_`~]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def field_status(value_raw, hay):
    """'missing' | 'exact' | 'reformulated'. hay is already normalized (and lowered if CI)."""
    value = norm(value_raw)
    if not value:
        return "missing"
    if CASE_INSENSITIVE:
        return "exact" if value.lower() in hay else "reformulated"
    return "exact" if value in hay else "reformulated"


def classify(rec):
    av = rec.get("ai_validation") or {}
    hay = norm((rec.get("issue_title") or "") + " \n " + (rec.get("issue_body") or ""))
    if CASE_INSENSITIVE:
        hay = hay.lower()

    r = field_status(av.get("role"), hay)
    f = field_status(av.get("feature"), hay)
    b = field_status(av.get("benefit"), hay)

    # role and feature are mandatory
    if r == "missing" or f == "missing":
        return "core_missing"
    if r == "reformulated" or f == "reformulated":
        return "core_reformulated"
    # role and feature verbatim: the benefit decides
    if b == "exact":
        return "full"
    if b == "missing":
        return "partial"
    return "benefit_reformulated"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="Run folders (containing 5_dataset/)")
    ap.add_argument("-o", "--output", default=".", help="Output folder")
    args = ap.parse_args()

    out_tree = os.path.join(args.output, "exact_match_dataset")
    if os.path.exists(out_tree):
        shutil.rmtree(out_tree)

    stats = {}
    light = []
    per_run = {}

    for run_dir in args.runs:
        run = os.path.basename(os.path.normpath(run_dir))
        stats[run] = {"total": 0, "incluso": 0, **{c: 0 for c in CLASSES}}
        pattern = os.path.join(run_dir, "5_dataset", "*", "*", "*", "data.json")

        for path in sorted(glob.glob(pattern)):
            with open(path) as fh:
                rec = json.load(fh)
            stats[run]["total"] += 1
            cls = classify(rec)
            stats[run][cls] += 1

            if cls not in INCLUDED:
                continue
            stats[run]["incluso"] += 1

            # path: .../5_dataset/<language>/<repo>/<issue>/data.json
            parts = path.split(os.sep)
            language, repo, issue = parts[-4], parts[-3], parts[-2]

            dst = os.path.join(out_tree, run, language, repo, issue)
            os.makedirs(dst, exist_ok=True)
            shutil.copy2(path, os.path.join(dst, "data.json"))

            light_rec = dict(rec)
            light_rec.pop("diff_between_base_and_last", None)
            light_rec["_run"] = run
            light_rec["_language"] = language
            light_rec["_class"] = cls
            light.append(light_rec)

            per_run.setdefault(run, {}).setdefault(language, 0)
            per_run[run][language] += 1

    os.makedirs(args.output, exist_ok=True)
    with open(os.path.join(args.output, "exact_match_dataset.json"), "w") as fh:
        json.dump(light, fh, indent=1)
    with open(os.path.join(args.output, "exact_match_stats.json"), "w") as fh:
        json.dump(
            {
                "criterio": (
                    "role e feature presenti e verbatim; benefit verbatim se presente, "
                    "altrimenti assente (facoltativo). Confronto %s, whitespace "
                    "normalizzato%s."
                    % ("case-insensitive" if CASE_INSENSITIVE else "case-sensitive",
                       ", markdown rimosso" if STRIP_MARKDOWN else "")
                ),
                "partizione_per_run": stats,
                "estratti_per_run_e_linguaggio": per_run,
                "totale_estratti": len(light),
            },
            fh,
            indent=2,
        )

    print(json.dumps(stats, indent=2))
    print("Estratti %d record in %s" % (len(light), out_tree))


if __name__ == "__main__":
    main()
