import argparse
import hashlib
import json
from pathlib import Path


def sha256_of_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate update.json for launcher updates")
    parser.add_argument("--exe", required=True, help="Path to release exe")
    parser.add_argument("--version", required=True, help="Version string, e.g. 5.0.1")
    parser.add_argument("--download-url", required=True, help="Direct download URL for exe on GitHub")
    parser.add_argument("--out", default="update.json", help="Output update.json path")
    args = parser.parse_args()

    exe_path = Path(args.exe).resolve()
    if not exe_path.is_file():
        raise SystemExit(f"EXE not found: {exe_path}")

    payload = {
        "version": str(args.version).strip(),
        "download_url": str(args.download_url).strip(),
        "sha256": sha256_of_file(exe_path),
    }

    out_path = Path(args.out).resolve()
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Generated {out_path}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
