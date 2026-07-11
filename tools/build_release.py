"""Build the per-window dataset release archives.

Merges the two R1 runs (exp_w1_2023_2026 + exp_w1_plus_2025_2026) and
packages, for each temporal window, the final dataset (phase 5 output)
together with the validation output and the per-phase run statistics
needed to verify the numbers reported in the thesis.

Usage:
    python build_release.py --experiments-dir PATH --w1plus-dir PATH --out PATH

Where:
    --experiments-dir  folder containing exp_w1_2023_2026, exp_w2_2020_2022,
                       exp_w3_2017_2019 (the extracted ``experiments/`` folder)
    --w1plus-dir       the extracted ``exp_w1_plus_2025_2026`` folder
    --out              output folder for the release archives

Produces:
    R1_2023_2026.tar.gz, R2_2020_2022.tar.gz, R3_2017_2019.tar.gz
    MANIFEST.md with record counts and SHA-256 checksums.

The source folders are never modified.
"""

import argparse
import hashlib
import json
import sys
import tarfile
import tempfile
from pathlib import Path

# Expected final record counts (data.json files) per window, as reported
# in the thesis (Tabella 1.1, row "Record finali").
EXPECTED_RECORDS = {"R1": 11_610, "R2": 5_737, "R3": 3_271}
# Expected validated issue counts per window (Tabella 1.9, row "Validate").
EXPECTED_VALIDATED = {"R1": 11_956, "R2": 5_937, "R3": 3_314}

PHASES = (
    "1_discovery",
    "2_issue_discovery",
    "3_pr_linking",
    "4_ai_validation",
    "5_dataset",
)


def fail(msg: str) -> None:
    print(f"ERRORE: {msg}")
    sys.exit(1)


def dataset_records(exp_dir: Path) -> set[str]:
    """Relative paths of all data.json files under 5_dataset."""
    root = exp_dir / "5_dataset"
    if not root.is_dir():
        fail(f"cartella 5_dataset assente in {exp_dir}")
    return {
        str(p.relative_to(root)) for p in root.rglob("data.json") if p.is_file()
    }


def add_dataset(tar: tarfile.TarFile, exp_dir: Path, arc_root: str) -> int:
    """Add every file under 5_dataset to the archive; return record count."""
    root = exp_dir / "5_dataset"
    n = 0
    for p in sorted(root.rglob("*")):
        if p.is_file():
            tar.add(p, arcname=f"{arc_root}/5_dataset/{p.relative_to(root)}")
            if p.name == "data.json":
                n += 1
    return n


def add_meta(tar: tarfile.TarFile, exp_dir: Path, arc_root: str, run_label: str) -> None:
    """Add config, pipeline state and per-phase run statistics."""
    base = f"{arc_root}/_meta/{run_label}"
    for name in ("config.json", "pipeline_state.json"):
        src = exp_dir / name
        if src.is_file():
            tar.add(src, arcname=f"{base}/{name}")
    for phase in PHASES:
        stats = exp_dir / phase / "run_stats.json"
        if stats.is_file():
            tar.add(stats, arcname=f"{base}/{phase}_run_stats.json")


def load_json_list(path: Path) -> list:
    if not path.is_file():
        fail(f"file atteso non trovato: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        fail(f"{path} non contiene una lista JSON")
    return data


def add_validated(
    tar: tarfile.TarFile, arc_root: str, sources: list[Path], tmpdir: Path
) -> int:
    """Concatenate validated_issues.json from the given runs and add it."""
    merged: list = []
    for src in sources:
        merged.extend(load_json_list(src / "4_ai_validation" / "validated_issues.json"))
    out = tmpdir / f"{arc_root}_validated_issues.json"
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tar.add(out, arcname=f"{arc_root}/validated_issues.json")
    return len(merged)


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

    w1 = args.experiments_dir / "exp_w1_2023_2026"
    w2 = args.experiments_dir / "exp_w2_2020_2022"
    w3 = args.experiments_dir / "exp_w3_2017_2019"
    plus = args.w1plus_dir

    for d in (w1, w2, w3, plus):
        if not d.is_dir():
            fail(f"cartella non trovata: {d}")

    # Safety check: the two R1 runs must not share any record.
    overlap = dataset_records(w1) & dataset_records(plus)
    if overlap:
        fail(
            f"{len(overlap)} record presenti in entrambe le run R1 "
            f"(es. {sorted(overlap)[:3]}): fusione interrotta."
        )
    print("Controllo collisioni R1: OK (0 record in comune)")

    windows = {
        "R1": {"arc": "R1_2023_2026", "runs": [("w1", w1), ("w1_plus", plus)]},
        "R2": {"arc": "R2_2020_2022", "runs": [("w2", w2)]},
        "R3": {"arc": "R3_2017_2019", "runs": [("w3", w3)]},
    }

    args.out.mkdir(parents=True, exist_ok=True)
    manifest_rows = []

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for win, spec in windows.items():
            arc_root = spec["arc"]
            archive = args.out / f"{arc_root}.tar.gz"
            print(f"\n[{win}] costruzione di {archive.name} ...")
            n_records = 0
            with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
                for run_label, exp_dir in spec["runs"]:
                    n = add_dataset(tar, exp_dir, arc_root)
                    print(f"  {run_label}: {n} record aggiunti")
                    n_records += n
                    add_meta(tar, exp_dir, arc_root, run_label)
                n_validated = add_validated(
                    tar, arc_root, [d for _, d in spec["runs"]], tmpdir
                )

            if n_records != EXPECTED_RECORDS[win]:
                fail(
                    f"{win}: {n_records} record trovati, "
                    f"attesi {EXPECTED_RECORDS[win]}"
                )
            if n_validated != EXPECTED_VALIDATED[win]:
                fail(
                    f"{win}: {n_validated} validate trovate, "
                    f"attese {EXPECTED_VALIDATED[win]}"
                )
            print(
                f"  verifica: {n_records} record, {n_validated} validate "
                f"(attesi {EXPECTED_RECORDS[win]} / {EXPECTED_VALIDATED[win]}) OK"
            )
            size_mb = archive.stat().st_size / 1e6
            manifest_rows.append(
                (archive.name, n_records, n_validated, f"{size_mb:.0f} MB", sha256(archive))
            )

    total = sum(r[1] for r in manifest_rows)
    lines = [
        "# Dataset release manifest",
        "",
        "| Archivio | Record finali | Issue validate | Dimensione | SHA-256 |",
        "|---|---|---|---|---|",
    ]
    for name, rec, val, size, digest in manifest_rows:
        lines.append(f"| {name} | {rec:,} | {val:,} | {size} | `{digest}` |")
    lines += ["", f"Totale record: **{total:,}**", ""]
    manifest = args.out / "MANIFEST.md"
    manifest.write_text("\n".join(lines).replace(",", "."), encoding="utf-8")

    print(f"\nTotale record: {total} (atteso 20618)")
    if total != sum(EXPECTED_RECORDS.values()):
        fail("il totale non corrisponde al dataset della tesi")
    print(f"Manifest scritto in {manifest}")
    print("Fatto.")


if __name__ == "__main__":
    main()
