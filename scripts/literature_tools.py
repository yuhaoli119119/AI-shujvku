#!/usr/bin/env python3
"""Read-only local coordinator CLI; never dispatches or changes production data."""
import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.literature_tools import AdapterError, BrowserAdapter, WorkbenchAPI, write_project_output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["browser-status", "work-package"])
    parser.add_argument("--paper-id")
    parser.add_argument("--output", required=True, help="New file under this project's outputs directory")
    args = parser.parse_args()
    if args.command == "browser-status":
        result = BrowserAdapter.inspect()
    else:
        if not args.paper_id:
            parser.error("work-package requires --paper-id")
        adapter = WorkbenchAPI.from_local_credentials()
        try:
            result = adapter.work_package(args.paper_id)
        finally:
            adapter.close()
    write_project_output(args.output, result)
    print(json.dumps({"saved": str(Path(args.output).resolve())}))


if __name__ == "__main__":
    try:
        main()
    except AdapterError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
