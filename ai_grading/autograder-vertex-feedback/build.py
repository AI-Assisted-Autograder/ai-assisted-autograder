"""Package the original Java grader and the assignment-token Vertex client."""

import argparse
import json
from pathlib import Path
import sys
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parents[1]
sys.path.insert(0, str(ROOT))
from local_client import ClientError, read_assignment_token, validate_backend_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--token-file", type=Path,
                        default=ROOT / ".local-vertex/gradescope/client/token")
    parser.add_argument("--output", type=Path, default=SOURCE / "vertex autograder.zip")
    parser.add_argument("--functional-source", type=Path,
                        default=ROOT / ".local-vertex/gradescope/functional-source")
    args = parser.parse_args()
    try:
        backend_url = validate_backend_url(args.backend_url)
        token = read_assignment_token(args.token_file)
    except ClientError as error:
        parser.error(str(error))
    if not backend_url.startswith("https://"):
        parser.error("The Gradescope backend must use HTTPS.")

    files = {
        "setup.sh": ((SOURCE / "setup.sh").read_bytes(), 0o755),
        "run_autograder": ((SOURCE / "run_autograder").read_bytes(), 0o755),
        "vertex_feedback.py": ((SOURCE / "vertex_feedback.py").read_bytes(), 0o644),
        "ai_feedback.json": ((SOURCE / "ai_feedback.json").read_bytes(), 0o644),
        "local_client.py": ((ROOT / "local_client.py").read_bytes(), 0o644),
        "config.json": ((json.dumps({"backend_url": backend_url}) + "\n").encode(), 0o644),
        "assignment.token": ((token + "\n").encode("ascii"), 0o600),
    }
    for name in ("compile_error.prefix", "compile_error.postfix"):
        files[name] = ((args.functional_source / "autograder" / name).read_bytes(), 0o644)
    # Only the grader runtime is included, never .git, autograder/deploy_key,
    # SSH config, or the repository's example student implementation.
    for name in (
        "compile.sh", "run.sh", "LICENSE",
        "tests/MainGrader.java", "tests/SortedCollectionGrading.java",
        "lib/jgrade-1.1-all.jar", "lib/json-20180813.jar",
        "lib/mockito-core-1.10.19.jar", "lib/objenesis-2.4.jar",
    ):
        files["github/" + name] = ((args.functional_source / name).read_bytes(), 0o644)
    for name, (data, mode) in files.items():
        path = SOURCE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as destination:
            path.chmod(mode)
            destination.write(data)

    with args.output.open("wb") as output:
        args.output.chmod(0o600)
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for name, (_, mode) in files.items():
                member = ZipInfo(name)
                member.create_system = 3
                member.external_attr = (0o100000 | mode) << 16
                archive.writestr(member, (SOURCE / name).read_bytes(), compress_type=ZIP_DEFLATED)
    print(f"Created {args.output}")
    print(f"Packaged {len(files)} files; matching loose files are in {SOURCE}")
    print("Contains an assignment token; keep this archive out of Git.")


if __name__ == "__main__":
    main()
