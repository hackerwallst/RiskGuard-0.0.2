import argparse
import importlib
import logging
import os
import sys
import traceback

os.environ.setdefault("MPLBACKEND", "Agg")


CRITICAL_IMPORTS = [
    "MetaTrader5",
    "win32api",
    "pywinauto",
    "requests",
    "pandas",
    "pytz",
    "numpy",
    "matplotlib",
    "img2pdf",
    "investpy",
]

PROJECT_IMPORTS = [
    "mt5_reader",
    "limits",
    "logger",
    "notify",
    "reports",
]


def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("health_check")
    logger.setLevel(logging.INFO)
    logger.handlers = []
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def check_imports(logger: logging.Logger, names, label):
    failures = []
    for name in names:
        try:
            importlib.import_module(name)
            logger.info("%s import ok: %s", label, name)
        except Exception as exc:  # noqa: BLE001 - explicit logging
            logger.error("%s import failed: %s - %s", label, name, exc)
            failures.append(name)
    return failures


def check_permissions(logger: logging.Logger, app_dir: str, logs_dir: str):
    failures = []
    main_path = os.path.join(app_dir, "main.py")
    try:
        with open(main_path, "r", encoding="utf-8") as handle:
            handle.read(16)
        logger.info("Read access ok: %s", main_path)
    except Exception as exc:  # noqa: BLE001 - explicit logging
        logger.error("Read access failed: %s - %s", main_path, exc)
        failures.append("read")

    try:
        os.makedirs(logs_dir, exist_ok=True)
        test_path = os.path.join(logs_dir, "health_check.tmp")
        with open(test_path, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(test_path)
        logger.info("Write access ok: %s", logs_dir)
    except Exception as exc:  # noqa: BLE001 - explicit logging
        logger.error("Write access failed: %s - %s", logs_dir, exc)
        failures.append("write")

    return failures


def bootstrap_riskguard(logger: logging.Logger, app_dir: str):
    failures = []
    try:
        if app_dir not in sys.path:
            sys.path.insert(0, app_dir)
        os.environ["RG_HEALTH_CHECK"] = "1"
        import main  # noqa: F401
        if hasattr(main, "_detect_terminal"):
            _ = main._detect_terminal()
        logger.info("Bootstrap ok: main imported.")
    except Exception:  # noqa: BLE001 - explicit logging
        logger.error("Bootstrap failed:\n%s", traceback.format_exc())
        failures.append("bootstrap")
    return failures


def parse_args():
    parser = argparse.ArgumentParser(description="RiskGuard install health check")
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--logs-dir", required=True)
    parser.add_argument("--log-file", required=False, default="")
    return parser.parse_args()


def main():
    args = parse_args()
    logger = setup_logging(args.log_file)

    logger.info("Health check started.")
    failures = []

    failures += check_permissions(logger, args.app_dir, args.logs_dir)
    failures += check_imports(logger, CRITICAL_IMPORTS, "critical")
    failures += check_imports(logger, PROJECT_IMPORTS, "project")
    failures += bootstrap_riskguard(logger, args.app_dir)

    if failures:
        logger.error("Health check failed: %s", ", ".join(sorted(set(failures))))
        return 1

    logger.info("Health check completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
