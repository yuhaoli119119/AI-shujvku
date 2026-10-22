#!/usr/bin/env python3
"""Offline, explicit-scope rebuild dataset command. Does not load app settings."""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from app.services.rebuild_ml_dataset import build_dataset, encoded, read_snapshot, write_dataset
from app.services.rebuild_ml_shards import build_sharded_dataset, load_plan, write_sharded_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('snapshot', help='Capture a read-only DB graph for independent review')
    capture.add_argument('--paper-id', action='append', required=True)
    capture.add_argument('--output', required=True)
    capture.add_argument('--database-url-env', default='LITAI_ML_READONLY_DATABASE_URL')
    build = commands.add_parser('build', help='Build from reviewed immutable snapshot and explicit dispositions')
    build.add_argument('--snapshot', required=True)
    build.add_argument('--acceptance', required=True)
    build.add_argument('--output', required=True)
    build.add_argument('--seed', type=int, default=0)
    build.add_argument('--code-revision', required=True)
    shards = commands.add_parser('build-shards', help='Build isolated reviewed shards and exact union index')
    shards.add_argument('--snapshot', required=True)
    shards.add_argument('--plan', required=True)
    shards.add_argument('--output', required=True)
    shards.add_argument('--seed', type=int, default=0)
    shards.add_argument('--code-revision', required=True)
    args = parser.parse_args()
    if args.command == 'snapshot':
        url = os.environ[args.database_url_env]
        engine = create_engine(url, future=True)
        try:
            with engine.connect() as connection: snapshot = read_snapshot(connection, args.paper_id)
            with Path(args.output).open('xb') as handle: handle.write(encoded(snapshot))
        finally: engine.dispose()
    elif args.command == 'build-shards':
        snapshot = json.loads(Path(args.snapshot).read_text())
        write_sharded_dataset(build_sharded_dataset(snapshot, load_plan(args.plan),
            seed=args.seed, code_revision=args.code_revision), args.output)
    else:
        snapshot = json.loads(Path(args.snapshot).read_text())
        manifest = json.loads(Path(args.acceptance).read_text())
        write_dataset(build_dataset(snapshot, manifest, seed=args.seed, code_revision=args.code_revision), args.output)


if __name__ == '__main__': main()
