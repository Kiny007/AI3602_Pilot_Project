from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.error

from .common import ROOT, PilotError, write_json
from .config import load_config
from .evaluation import compare_runs, evaluate, make_audit, split_prepared, verify_result
from .pipeline import run
from .source import download_webis, prepare


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Reddit 隐私披露挖掘：来源、证据、复现与评估")
    commands = p.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("download", help="Download the official ~3.1 GB Webis archive with checksum verification")
    fetch.add_argument("--output", type=Path, default=Path("data/raw/corpus-webis-tldr-17.zip"))
    fetch.add_argument("--restart", action="store_true", help="Back up any .part file and restart instead of resuming")
    prep = commands.add_parser("prepare", help="Freeze a deterministic raw-input subset; no model calls")
    prep.add_argument("--input", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--config", type=Path, default=ROOT / "config/default.toml")
    split = commands.add_parser("split", help="Disjoint dev/test/collection sets BEFORE rules or model calls")
    split.add_argument("--prepared", type=Path, required=True)
    split.add_argument("--output", type=Path, required=True)
    split.add_argument("--dev", type=int, default=40)
    split.add_argument("--test", type=int, default=100)
    split.add_argument("--seed", type=int, default=3602)
    pipeline = commands.add_parser("run", help="Execute extraction, validation, review and export")
    pipeline.add_argument("--prepared", type=Path, required=True)
    pipeline.add_argument("--output", type=Path, required=True)
    pipeline.add_argument("--config", type=Path, default=ROOT / "config/default.toml")
    pipeline.add_argument("--cache", type=Path, default=Path("artifacts/cache"))
    pipeline.add_argument("--mode", choices=("resume", "replay", "fresh"), default="resume")
    pipeline.add_argument("--prompts", type=Path, default=ROOT / "prompts")
    audit = commands.add_parser("audit", help="Export a blind annotation template (gold labels initially null)")
    audit.add_argument("--input", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--count", type=int, default=50)
    audit.add_argument("--seed", type=int, default=3602)
    audit.add_argument("--scope", choices=("final", "holdout", "rule_negative"), required=True)
    score = commands.add_parser("evaluate", help="Compute metrics from completed HUMAN annotations")
    score.add_argument("--predictions", type=Path, required=True)
    score.add_argument("--gold", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="Verify counts, exact evidence and optionally original source files")
    verify.add_argument("--run", type=Path, required=True)
    verify.add_argument("--raw-root", type=Path)
    verify.add_argument("--output", type=Path)
    verify.add_argument("--require-minimum", action="store_true")
    compare = commands.add_parser("compare", help="Compare two runs, including evidence and byte equality")
    compare.add_argument("first", type=Path)
    compare.add_argument("second", type=Path)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    exit_code = 0
    try:
        if args.command == "download":
            result = download_webis(args.output, restart=args.restart,
                                    progress=lambda s: print(s, file=sys.stderr, flush=True))
        elif args.command == "prepare":
            result = prepare(args.input, args.output, load_config(args.config))
        elif args.command == "split":
            result = split_prepared(args.prepared, args.output, args.dev, args.test, args.seed)
        elif args.command == "run":
            result = run(args.prepared, args.output, load_config(args.config), args.cache, args.mode,
                         prompts_dir=args.prompts, progress=lambda s: print(s, file=sys.stderr, flush=True))
            if result["counts"].get("error", 0) or any(k.startswith("unprocessed_") and k != "unprocessed_target"
                                                     for k in result["counts"]):
                exit_code = 2
        elif args.command == "audit":
            result = make_audit(args.input, args.output, args.count, args.seed, args.scope)
        elif args.command == "evaluate":
            result = evaluate(args.predictions, args.gold)
            write_json(args.output, result)
        elif args.command == "verify":
            result = verify_result(args.run, args.raw_root)
            if args.output:
                write_json(args.output, result)
            if not result["structurally_valid"] or (args.raw_root and not result["source_verification"]["ok"]) or \
               (args.require_minimum and not result["count_thresholds_met"]):
                exit_code = 2
        elif args.command == "compare":
            result = compare_runs(args.first, args.second)
            if not result["identical_bytes"]:
                exit_code = 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code
    except (PilotError, OSError, ValueError, KeyError, urllib.error.URLError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Completed response caches remain reusable; use a NEW output directory to resume.", file=sys.stderr)
        return 130
