#!/usr/bin/env python3
"""
Safety gate for an uploaded zip, run BEFORE any extraction happens.

Reads the zip's central directory only (zipfile.infolist()) -- this never extracts
a single byte, so it's safe to run against an untrusted upload. Three checks, any
failure means the file gets quarantined instead of processed:

  1. Zip-bomb guard: total uncompressed size across all entries must stay under a cap.
  2. Zip-slip guard: no entry path may escape the extraction directory (absolute paths,
     "..", or a leading "/" are all rejected).
  3. Structure guard: must actually look like a LinkedIn "Request my data" export --
     requires Profile.csv plus at least 2 of the other core CSVs, not just any zip
     that happens to unzip cleanly.

Exit code 0 = safe to extract and process. Exit code 1 = reject; caller quarantines
the object in R2 and records the reason instead of running the ingest pipeline on it.
"""
import sys
import zipfile

MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500MB -- a real export is a few MB
CORE_LINKEDIN_FILES = {
    "Profile.csv",
    "Positions.csv",
    "Skills.csv",
    "Education.csv",
    "Certifications.csv",
}
MIN_CORE_FILES_PRESENT = 3  # Profile.csv + at least 2 others


def entry_basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def is_unsafe_path(path: str) -> bool:
    if path.startswith("/") or path.startswith("\\"):
        return True
    parts = path.replace("\\", "/").split("/")
    return ".." in parts


def validate(zip_path: str) -> str | None:
    """Returns None if safe, or a human-readable rejection reason string."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return "Not a valid zip file."

    infos = zf.infolist()
    if not infos:
        return "Zip file is empty."

    total_uncompressed = sum(i.file_size for i in infos)
    if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
        mb = total_uncompressed / (1024 * 1024)
        return f"Uncompressed size ({mb:.0f}MB) exceeds the {MAX_UNCOMPRESSED_BYTES // (1024*1024)}MB limit -- possible zip bomb."

    unsafe = [i.filename for i in infos if is_unsafe_path(i.filename)]
    if unsafe:
        return f"Zip contains unsafe path(s) that escape the extraction directory: {unsafe[:3]}"

    basenames = {entry_basename(i.filename) for i in infos if not i.is_dir()}
    found_core = CORE_LINKEDIN_FILES & basenames
    if "Profile.csv" not in basenames or len(found_core) < MIN_CORE_FILES_PRESENT:
        return (
            f"Doesn't look like a LinkedIn export -- found {sorted(found_core) or 'none'} "
            f"of the expected core files {sorted(CORE_LINKEDIN_FILES)}."
        )

    return None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: validate_linkedin_export.py <path-to-zip>", file=sys.stderr)
        sys.exit(2)

    reason = validate(sys.argv[1])
    if reason:
        print(reason)
        sys.exit(1)

    print("OK: looks like a real LinkedIn export, safe to extract.")
    sys.exit(0)
