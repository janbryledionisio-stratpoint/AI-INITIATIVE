import argparse
import logging
import sys

from dotenv import load_dotenv

from testrail import fetch_test_cases, generate_endpoint_file
from pipeline import save_all_cases, fetch_passed_cases
from validation import validate_cases, PROVIDER_DEFAULT_MODEL

logger = logging.getLogger(__name__)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _add_llm_args(parser: argparse.ArgumentParser) -> None:
    _providers = list(PROVIDER_DEFAULT_MODEL)
    _model_defaults = ", ".join(
        f"{p}: {m}" for p, m in PROVIDER_DEFAULT_MODEL.items()
    )
    parser.add_argument(
        "--provider",
        default="anthropic",
        choices=_providers,
        help=f"LLM provider to use (default: anthropic; choices: {', '.join(_providers)})",
    )
    parser.add_argument(
        "--model",
        default="",
        help=f"Model override — uses provider default if omitted ({_model_defaults})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0,
        help="Seconds to wait between API calls — use 4+ for Google free tier (default: 0)",
    )


def run_fetch(args):
    endpoint_file = generate_endpoint_file(args.input_csv)
    results = fetch_test_cases(endpoint_file)
    output_file = save_all_cases(results, output_dir=args.output_dir)
    logger.info("Fetch complete. Output: %s", output_file)
    return output_file


def run_validate(args):
    cases = fetch_passed_cases(output_dir=args.output_dir)
    if not cases:
        logger.warning("No passed test cases found. Nothing to validate.")
        return None
    output_file = validate_cases(
        cases,
        output_dir=args.output_dir,
        provider=args.provider,
        model=args.model or None,
        delay=args.delay,
    )
    logger.info("Validation complete. Output: %s", output_file)
    return output_file


def main():
    load_dotenv()
    setup_logging()

    parser = argparse.ArgumentParser(
        description="AI Initiative — TestRail fetch and LLM validation pipeline"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch test cases from TestRail and apply general standards filter",
    )
    fetch_parser.add_argument(
        "--input-csv",
        default="data/GRCX-web.csv",
        help="Path to the source CSV (default: data/GRCX-web.csv)",
    )
    fetch_parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory where output CSVs are saved (default: output/)",
    )

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate passed test cases against requirements.md using an LLM",
    )
    validate_parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory containing output CSVs and where validation results are saved (default: output/)",
    )
    _add_llm_args(validate_parser)

    all_parser = subparsers.add_parser(
        "run-all",
        help="Run fetch then validate end-to-end",
    )
    all_parser.add_argument(
        "--input-csv",
        default="data/GRCX-web.csv",
        help="Path to the source CSV (default: data/GRCX-web.csv)",
    )
    all_parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory where output CSVs are saved (default: output/)",
    )
    _add_llm_args(all_parser)

    args = parser.parse_args()

    if args.command == "fetch":
        run_fetch(args)
    elif args.command == "validate":
        run_validate(args)
    elif args.command == "run-all":
        run_fetch(args)
        run_validate(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
