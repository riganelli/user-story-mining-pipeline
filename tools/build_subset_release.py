"""Build the literal-match (verbatim) sub-dataset release artifacts.

Applies the inclusion criterion of thesis section 5.4.6 to the final
dataset (phase 5 output) of the three temporal windows and packages the
result for distribution. The criterion is the one implemented in
``extract_exact_match_benefit_opt.py``:

  - role and feature must be present and appear verbatim (whitespace
    normalized, case-insensitive) in the concatenation of issue title
    and body;
  - benefit is optional; when present it must satisfy the same
    constraint.

Runs are mapped to windows: R1 = exp_w1_2023_2026 + exp_w1_plus_2025_2026,
R2 = exp_w2_2020_2022, R3 = exp_w3_2017_2019.

Usage:
    python build_subset_release.py --experiments-dir PATH --w1plus-dir PATH --out PATH

Produces in --out:
    verbatim_subset.tar.gz        full data.json tree per window + index + stats
    verbatim_subset_index.json    window/language/repo/issue/class of every
                                   included record (lightweight)
    verbatim_subset_light.json    included records without the diff field
    verbatim_subset_stats.json    partition counts per window
    (index and stats are also included inside the archive)

Counts are verified against the thesis tables (5.18 and 5.19); any
mismatch aborts the build. Source folders are never modified.
"""

import argparse
import hashlib
import json
import re
import sys
import tarfile
import tempfile
from pathlib import Path

# --- criterion switches (must mirror extract_exact_match_benefit_opt.py) ---
CASE_INSENSITIVE = True
STRIP_MARKDOWN = False

CLASSES = ("full", "partial", "benefit_reformulated",
           "core_reformulated", "core_missing")
INCLUDED = {"full", "partial"}

# Expected partition per window (Tabelle 5.18/5.19 and per-run stats).
EXPECTED = {
    "R1": {"total": 11_610, "incluso": 8_963, "full": 7_161, "partial": 1_802,
           "benefit_reformulated": 630, "core_reformulated": 1_489,
           "core_missing": 528, "java": 3_162, "python": 5_801},
    "R2": {"total": 5_737, "incluso": 4_323, "full": 3_011, "partial": 1_312,
           "benefit_reformulated": 324, "core_reformulated": 733,
           "core_missing": 357, "java": 1_953, "python": 2_370},
    "R3": {"total": 3_271, "incluso": 2_448, "full": 1_552, "partial": 896,
           "benefit_reformulated": 256, "core_reformulated": 382,
           "core_missing": 185, "java": 1_158, "python": 1_290},
}
EXPECTED_TOTAL = 15_734


def fail(msg: str) -> None:
    print(f"ERRORE: {msg}")
    sys.exit(1)


# --- criterion (verbatim from extract_exact_match_benefit_opt.py) ---

def norm(s):
    """Normalizza whitespace (+ eventuale markdown). Gestisce anche campi lista."""
    if isinstance(s, list):
        s = " ".join(str(x) for x in s)
    if s is None:
        return ""
    s = str(s)
    if STRIP_MARKDOWN:
        s = re.sub(r"[*_`~]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def field_status(value_raw, hay):
    """'missing' | 'exact' | 'reformulated'. hay e' gia' normalizzato (e lower se CI)."""
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

    if r == "missing" or f == "missing":
        return "core_missing"
    if r == "reformulated" or f == "reformulated":
        return "core_reformulated"
    if b == "exact":
        return "full"
    if b == "missing":
        return "partial"
    return "benefit_reformulated"


# --- packaging ---

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--experiments-dir", required=True, type=Path)
    ap.add_argument("--w1plus-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    windows = {
        "R1": {"arc": "R1_2023_2026",
               "runs": [args.experiments_dir / "exp_w1_2023_2026", args.w1plus_dir]},
        "R2": {"arc": "R2_2020_2022",
               "runs": [args.experiments_dir / "exp_w2_2020_2022"]},
        "R3": {"arc": "R3_2017_2019",
               "runs": [args.experiments_dir / "exp_w3_2017_2019"]},
    }
    for spec in windows.values():
        for d in spec["runs"]:
            if not (d / "5_dataset").is_dir():
                fail(f"cartella 5_dataset non trovata in {d}")

    args.out.mkdir(parents=True, exist_ok=True)
    archive = args.out / "verbatim_subset.tar.gz"

    index = []
    light = []
    stats = {}

    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
        for win, spec in windows.items():
            counts = {"total": 0, "incluso": 0, **{c: 0 for c in CLASSES},
                      "java": 0, "python": 0}
            print(f"[{win}] classificazione ...")
            for run_dir in spec["runs"]:
                for path in sorted((run_dir / "5_dataset").glob("*/*/*/data.json")):
                    rec = json.loads(path.read_text(encoding="utf-8"))
                    counts["total"] += 1
                    cls = classify(rec)
                    counts[cls] += 1
                    if cls not in INCLUDED:
                        continue
                    counts["incluso"] += 1

                    language = path.parts[-4]
                    repo = path.parts[-3]
                    issue = path.parts[-2]
                    counts[language] = counts.get(language, 0) + 1

                    arcname = (f"{spec['arc']}/5_dataset/"
                               f"{language}/{repo}/{issue}/data.json")
                    tar.add(path, arcname=arcname)

                    index.append({"window": win, "language": language,
                                  "repo": repo, "issue": issue, "class": cls})

                    light_rec = dict(rec)
                    light_rec.pop("diff_between_base_and_last", None)
                    light_rec["_window"] = win
                    light_rec["_language"] = language
                    light_rec["_class"] = cls
                    light.append(light_rec)

            exp = EXPECTED[win]
            for key, want in exp.items():
                got = counts.get(key, 0)
                if got != want:
                    fail(f"{win}: {key} = {got}, atteso {want}")
            print(f"  {counts['incluso']} inclusi su {counts['total']} "
                  f"(full {counts['full']}, parziali {counts['partial']}) OK")
            stats[win] = counts

        if len(index) != EXPECTED_TOTAL:
            fail(f"totale inclusi = {len(index)}, atteso {EXPECTED_TOTAL}")

        stats_obj = {
            "criterio": ("role e feature presenti e verbatim; benefit verbatim "
                         "se presente, altrimenti assente (facoltativo). "
                         "Confronto case-insensitive, whitespace normalizzato."),
            "finestre": {"R1": "exp_w1_2023_2026 + exp_w1_plus_2025_2026",
                         "R2": "exp_w2_2020_2022", "R3": "exp_w3_2017_2019"},
            "partizione_per_finestra": stats,
            "totale_inclusi": len(index),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            files = {
                "verbatim_subset_index.json": index,
                "verbatim_subset_stats.json": stats_obj,
            }
            for name, obj in files.items():
                p = tmpdir / name
                p.write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                             encoding="utf-8")
                tar.add(p, arcname=name)
                (args.out / name).write_text(p.read_text(encoding="utf-8"),
                                             encoding="utf-8")

    light_path = args.out / "verbatim_subset_light.json"
    light_path.write_text(json.dumps(light, ensure_ascii=False, indent=1),
                          encoding="utf-8")

    print(f"\nTotale inclusi: {len(index)} (atteso {EXPECTED_TOTAL}) OK")
    for p in (archive, light_path):
        size = p.stat().st_size / 1e6
        print(f"  {p.name}: {size:.0f} MB  sha256={sha256(p)}")
    print("Fatto.")


if __name__ == "__main__":
    main()
