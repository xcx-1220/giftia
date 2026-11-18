"""Build the test candidate archive from explicit source directories and pinned pypdf."""

import argparse
from hashlib import sha256
from importlib.metadata import distribution
from pathlib import Path
import tarfile

SOURCE = Path(__file__).resolve().parent
CONFIG = ("config/settings.toml", "config/persona.toml")
PWA = ("web/.well-known/assetlinks.json", "web/manifest.json", "web/icons/icon-192.png", "web/icons/icon-512.png")
HARNESS = ("package.json", "package-lock.json", ".npmrc", "profile.patch.yml", "document.patch.yml",
           "development.patch.yml", "stage5_cloud.sh", "stage5_live_cloud.sh", "r2_cloud.sh", "r2_cutover.sh",
           "verify_stage4_browser.js", "native_ui_deploy.sh")


def file_mode(info):
    if not info.isfile():
        raise ValueError(f"Expected a regular archive file: {info.name}")
    info.mode = 0o644
    return info


def pack(fixture, output):
    root = SOURCE.parent
    paths = [*(root / "giftia").glob("*.py"), *SOURCE.glob("*.py")]
    paths.extend(root / path for path in (*CONFIG, *PWA, "deploy/giftia-harness.service"))
    paths.extend(SOURCE / path for path in HARNESS)
    for directory in ("isla-bundle", "sample-tool-bundle", "chat-tool-bundle", "native-todo-bundle", "web"):
        paths.extend(path for path in (SOURCE / directory).iterdir() if path.suffix in {".js", ".yml", ".json", ".html", ".css"})
    pdf = distribution("pypdf")
    requirement = (SOURCE / "web-requirements.txt").read_text(encoding="utf-8").strip()
    if requirement != f"pypdf=={pdf.version}":
        raise ValueError("Install the pinned pypdf version first")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "x:gz") as archive:
        for path in sorted(paths):
            archive.add(path, arcname=f"source/{path.relative_to(root).as_posix()}", filter=file_mode)
        for entry in pdf.files:
            if (len(entry.parts) > 1 and entry.parts[0] in {"pypdf", f"pypdf-{pdf.version}.dist-info"}
                    and "__pycache__" not in entry.parts):
                archive.add(pdf.locate_file(entry), arcname=f"python/{entry.as_posix()}", filter=file_mode)
        archive.add(fixture, arcname="synthetic-reference.pdf", filter=file_mode)
    return sha256(output.read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(pack(args.fixture, args.output))
