"""CLI to create a new experiment with a well-formed ``config.json``.

Creates the ``experiments/{id}/`` folder, writes the configuration and
optionally marks the experiment as current. The build and validation
logic lives in ``config_builder``, shared with the Streamlit UI; this
module is a thin command-line wrapper.

Examples:

# 500-repo run without the TAG strategy, excluding repos already used
python new_experiment.py exp_002_no_tag_500 \
    --description "500 repos, TAG ablation, excludes exp_001" \
    --max-repo 500 --max-per-strategy 100 \
    --strategies LABEL TEMPLATE MENTION CONNEXTRA \
    --exclude-from exp_001_baseline_100 \
    --exclude-rejected-from exp_001_baseline_100 \
    --set-current

# CONNEXTRA-only run of 200 repos
python new_experiment.py exp_003_only_connextra_200 \
    --description "CONNEXTRA strategy only" \
    --max-repo 200 \
    --strategies CONNEXTRA

# Threshold-based language filter: accept polyglot repos where Java or
# Python account for at least 20% of the bytes
python new_experiment.py exp_005_lang_threshold_20 \
    --description "any_above_threshold language filter @ 20%" \
    --max-repo 100 \
    --language-mode any_above_threshold \
    --min-lang-pct 20

# Dry run
python new_experiment.py exp_004_test --dry-run --strategies LABEL CONNEXTRA
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the pipeline package importable when this script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.config_builder import build_config, write_config, VALID_STRATEGIES, VALID_RUN_MODES


def _int_or_none(value: str) -> "int | None":
    """argparse type: 'none'/'null'/'' → None (no cap), otherwise an int."""
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("none", "null", ""):
        return None
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer or 'none', got '{value}'")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a new experiment + config.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("experiment_id", help="experiment ID (folder name)")
    parser.add_argument("--description", default="", help="free-text description")

    parser.add_argument("--max-repo", type=_int_or_none, default=100,
                        help="total repo cap (default: 100). Use 'none' for no cap.")
    parser.add_argument("--max-per-strategy", type=_int_or_none, default=20,
                        help="per-strategy repo cap (default: 20). Use 'none' for no cap.")
    parser.add_argument("--min-stars", type=int, default=2,
                        help="minimum stars (default: 2)")
    parser.add_argument("--languages", nargs="+", default=["Java", "Python"],
                        help="allowed languages (default: Java Python)")
    parser.add_argument("--language-mode", choices=["primary_only", "any_above_threshold"],
                        default="primary_only",
                        help="language filter mode: 'primary_only' (legacy: only if "
                             "primaryLanguage is in --languages) or 'any_above_threshold' "
                             "(accepts if at least one of --languages is present with "
                             "pct >= --min-lang-pct). Default: primary_only.")
    parser.add_argument("--min-lang-pct", type=float, default=15.0,
                        help="%% bytes threshold for 'any_above_threshold' (default: 15.0). "
                             "Ignored if --language-mode=primary_only.")
    parser.add_argument("--strategies", nargs="+", default=list(VALID_STRATEGIES),
                        help="strategies to activate. Valid: " + " ".join(sorted(VALID_STRATEGIES)))
    parser.add_argument("--time-windows-granularity",
                        choices=["yearly", "semester", "quarter", "monthly", "custom"],
                        default="yearly",
                        help="phase-1 time-window granularity (default: yearly)")
    parser.add_argument("--time-windows-custom-days", type=int, default=30,
                        help="days per window when --time-windows-granularity=custom (default: 30)")
    parser.add_argument("--max-pages-override", type=_int_or_none, default=None,
                        help="override max_pages for ALL phase-2 strategies (int >= 1), "
                             "or 'none'/omit to keep the per-strategy defaults")

    parser.add_argument("--cutoff-date-closed", default=None,
                        help="keep only issues closed on/after this date (YYYY-MM-DD)")
    parser.add_argument("--cutoff-date-closed-end", default=None,
                        help="keep only issues closed on/before this date (YYYY-MM-DD). "
                             "With --cutoff-date-closed it defines a closed interval.")
    parser.add_argument("--max-issue-per-repo", type=_int_or_none, default=15,
                        help="issue cap per repo (default: 15). Use 'none' for no cap.")
    parser.add_argument("--min-language-files", type=int, default=0,
                        help="min language files in the linked PR (step 5 filter; 0 = off)")
    parser.add_argument("--min-language-fraction", type=float, default=0.0,
                        help="min fraction of language files in the linked PR (step 5; 0.0 = off)")

    parser.add_argument("--run-mode", choices=sorted(VALID_RUN_MODES), default="testing",
                        help="'testing' (you set the caps) or 'full_coverage' (forces no caps, "
                             "quarterly windows, max_pages=10). Default: testing.")

    parser.add_argument("--exclude-from", nargs="*", default=[],
                        help="experiment IDs to exclude repos from (step 1)")
    parser.add_argument("--exclude-issues-from", nargs="*", default=[],
                        help="experiment IDs to exclude extracted issues from (step 2)")
    parser.add_argument("--exclude-rejected-from", nargs="*", default=[],
                        help="experiment IDs to exclude discarded issues from (step 4)")
    parser.add_argument("--exclude-validated-from", nargs="*", default=[],
                        help="experiment IDs to exclude validated issues from (step 4)")

    parser.add_argument("--root", default=None,
                        help="experiments root folder (default: the same one the "
                             "pipeline uses, see config/experiment_paths.py — i.e. EXPERIMENTS_ROOT "
                             "if set, or <repo_root>/experiments/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what it would do without writing anything")
    parser.add_argument("--set-current", action="store_true",
                        help="write the experiment_id into current_experiment.txt")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing folder (WARNING)")

    args = parser.parse_args()

    # Warn about extreme thresholds without rejecting them.
    if args.language_mode == "any_above_threshold":
        if 0.0 < args.min_lang_pct < 5.0:
            print(f"⚠️  --min-lang-pct={args.min_lang_pct}% very low: risk of false positives.")
        elif args.min_lang_pct > 50.0:
            print(f"⚠️  --min-lang-pct={args.min_lang_pct}% very high: almost equivalent to primary_only.")

    # full_coverage forces no caps, quarterly windows and max_pages=10
    # (mirroring the Streamlit UI); testing uses the flags as given.
    if args.run_mode == "full_coverage":
        eff_max_repo = None
        eff_max_per_strategy = None
        eff_max_issue_per_repo = None
        eff_time_windows_granularity = "quarter"
        eff_max_pages_override: "int | None" = 10
    else:
        eff_max_repo = args.max_repo
        eff_max_per_strategy = args.max_per_strategy
        eff_max_issue_per_repo = args.max_issue_per_repo
        eff_time_windows_granularity = args.time_windows_granularity
        eff_max_pages_override = args.max_pages_override

    try:
        config = build_config(
            experiment_id=args.experiment_id,
            description=args.description,
            max_repo=eff_max_repo,
            max_per_strategy=eff_max_per_strategy,
            min_stars=args.min_stars,
            languages=args.languages,
            language_mode=args.language_mode,
            min_lang_pct=args.min_lang_pct,
            strategies=args.strategies,
            time_windows_granularity=eff_time_windows_granularity,
            time_windows_custom_days=args.time_windows_custom_days,
            max_pages_override=eff_max_pages_override,
            min_language_files=args.min_language_files,
            min_language_fraction=args.min_language_fraction,
            cutoff_date_closed=args.cutoff_date_closed,
            cutoff_date_closed_end=args.cutoff_date_closed_end,
            max_issue_per_repo=eff_max_issue_per_repo,
            exclude_from=args.exclude_from,
            exclude_issues_from=args.exclude_issues_from,
            exclude_rejected_from=args.exclude_rejected_from,
            exclude_validated_from=args.exclude_validated_from,
            run_mode=args.run_mode,
        )
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    print("=" * 70)
    print(f"NEW EXPERIMENT: {args.experiment_id}")
    print("=" * 70)
    print(json.dumps(config, indent=2, ensure_ascii=False))
    print()

    if args.root is None:
        # Use the same root resolution as the pipeline runtime.
        from config.experiment_paths import _resolve_experiments_root
        root = _resolve_experiments_root()
    else:
        root = Path(args.root)
    target = root / args.experiment_id
    current_file = Path(__file__).resolve().parent.parent / "config" / "current_experiment.txt"

    if target.exists() and not args.force:
        print(f"❌ The folder '{target}' already exists — use --force to overwrite.")
        return 1

    if args.dry_run:
        print("Dry run: no changes to the filesystem.")
        return 0

    try:
        target = write_config(config, root, force=args.force)
    except FileExistsError as e:
        print(f"❌ {e}")
        return 1

    print(f"✅ Wrote {target / 'config.json'}")

    if args.set_current:
        current_file.write_text(args.experiment_id, encoding="utf-8")
        print(f"✅ Current experiment set to '{args.experiment_id}' (current_experiment.txt)")

    print()
    print("Next steps:")
    if not args.set_current:
        print(f"  1. Set the experiment as current:")
        print(f"     PowerShell: $env:EXPERIMENT_ID = '{args.experiment_id}'")
        print(f"     or re-run with --set-current")
    print(f"  2. Launch the pipeline: python orchestration/run_pipeline.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())