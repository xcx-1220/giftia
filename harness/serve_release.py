"""Run the Harness web origin behind an HTTPS proxy."""

import argparse
import os
from pathlib import Path

from continuity import Continuity
from giftia.config import load_env_file
from release_config import WebRelease
from stage1 import GATEWAY_PORT, MODEL_PORT, Lab
from web_gateway import serve_web


def deepseek_upstream(model):
    key = os.environ.get(model.api_key_env, "").strip()
    if not key:
        raise RuntimeError("The configured DeepSeek credential is missing")
    return f"{model.base_url.rstrip('/')}/anthropic", key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Candidate state and prepared Harness profile")
    parser.add_argument("--database", required=True, type=Path, help="Existing Giftia database")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--env-file", type=Path, help="Server-side Giftia credentials")
    parser.add_argument("--legacy-data", type=Path, help="The Giftia data directory for maintenance and push")
    parser.add_argument("--gateway-port", type=int, default=GATEWAY_PORT)
    parser.add_argument("--model-port", type=int, default=MODEL_PORT)
    parser.add_argument("--rehearse", action="store_true", help="Require synthetic data and use the mock model")
    args = parser.parse_args()
    os.umask(0o077)
    if args.env_file and not args.rehearse:
        load_env_file(args.env_file)
    release = WebRelease.load(args.manifest)
    settings = release.settings(rehearsal=args.rehearse)
    upstream = None if args.rehearse else deepseek_upstream(settings.model)
    lab = Lab(args.root, args.gateway_port, args.model_port, upstream=upstream)
    if not (lab.profile / "cordis.patch.yml").is_file():
        raise RuntimeError("Prepare the candidate Harness profile before starting the release origin")
    lab.database = args.database.resolve(strict=True)
    data = args.legacy_data.resolve(strict=True) if args.legacy_data else None
    if data is not None and data != lab.database.parent:
        parser.error('--legacy-data must be the directory containing the Giftia database')
    continuity = Continuity(data) if data else None
    serve_web(lab, False, release=release, continuity=continuity,
              role_options={"settings": settings, "owner": release.owner, "rehearsal": args.rehearse,
                            "giftia_root": data.parent if data else None})


if __name__ == "__main__":
    main()
