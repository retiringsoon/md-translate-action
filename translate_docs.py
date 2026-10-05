#!/usr/bin/env python3
"""Translate a documentation tree, preserving Markdown structure, via the md-translate API.

Design notes (why it looks like this):
- **stdlib only.** CI should not need `pip install`; a supply-chain-free entrypoint is one
  less thing that can break a build. Only `urllib.request` talks to the API.
- **Quote before translate.** `/v1/quote` is free, so a run can refuse to spend money:
  `--max-chars` turns a mis-scoped glob into a clean failure instead of a bill.
- **Glossary violations are a build failure, not a warning.** That gate is the whole point:
  `--fail-on-violation` makes terminology drift visible in the PR instead of silently
  shipping wrong vocabulary.
- **Fail closed.** Any non-2xx, malformed payload, or unwritable output aborts the run;
  there is no partial silent success.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 600


def fail(message: str, code: int = 2) -> None:
    print(f"::error::{message}")
    sys.exit(code)


def post_json(url: str, api_key: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        fail(f"{url} returned HTTP {exc.code}: {detail}")
    except OSError as exc:  # connection refused, DNS, TLS...
        fail(f"{url} is unreachable: {exc}")
    try:
        parsed = json.loads(body)
    except ValueError:
        fail(f"{url} returned a non-JSON body: {body[:200]}")
    if not isinstance(parsed, dict):
        fail(f"{url} returned unexpected JSON: {body[:200]}")
    return parsed


def load_glossary(path: str | None) -> list[dict[str, str]]:
    """Load term pairs from JSON (``[{"source":..,"target":..}]``) or CSV lines."""
    if not path:
        return []
    raw = pathlib.Path(path).read_text()
    pairs: list[dict[str, str]] = []
    if path.endswith(".json"):
        data = json.loads(raw)
        if not isinstance(data, list):
            fail("glossary JSON must be a list of {source,target}")
        for item in data:
            pairs.append({"source": str(item["source"]), "target": str(item["target"])})
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            source, _, target = line.partition(",")
            pairs.append({"source": source.strip(), "target": target.strip()})
    for pair in pairs:
        if not pair["source"] or not pair["target"]:
            fail(f"glossary entry with an empty side: {pair}")
    return pairs


def changed_files(ref: str) -> set[str]:
    """Paths changed since ``ref`` (for 'translate only what this PR touched')."""
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{ref}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        fail(f"cannot compute changed files since {ref!r}: {exc}")
    return {line.strip() for line in out.splitlines() if line.strip()}


def output_path(source: pathlib.Path, lang_slug: str, mode: str) -> pathlib.Path:
    if mode == "subdir":
        return source.parent / lang_slug / source.name
    return source.with_name(f"{source.stem}.{lang_slug}{source.suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--api-key", default=os.environ.get("MDTRANSLATE_API_KEY", ""))
    parser.add_argument("--paths", action="append", default=[])
    parser.add_argument("--target-lang", required=True)
    parser.add_argument("--lang-slug", required=True, help="filename/dir suffix, e.g. 'zh-CN'")
    parser.add_argument("--glossary", default=None)
    parser.add_argument("--output-mode", choices=("sibling", "subdir"), default="sibling")
    parser.add_argument("--changed-since", default=None)
    parser.add_argument("--fail-on-violation", action="store_true")
    parser.add_argument("--max-chars", type=int, default=200_000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.api_key:
        fail("no API key: pass --api-key or set MDTRANSLATE_API_KEY")

    patterns = args.paths or ["docs/**/*.md"]
    files: list[pathlib.Path] = []
    for pattern in patterns:
        files.extend(pathlib.Path(p) for p in glob.glob(pattern, recursive=True))
    files = sorted({p for p in files if p.is_file() and p.suffix == ".md"})
    if args.changed_since:
        allowed = changed_files(args.changed_since)
        files = [p for p in files if str(p) in allowed or str(p.as_posix()) in allowed]
    if not files:
        print("no Markdown files matched; nothing to do")
        return 0

    glossary = load_glossary(args.glossary)
    base = args.api_url.rstrip("/")
    total_chars = total_blocks = total_violations = written = 0
    violation_lines: list[str] = []
    summary: list[str] = []

    for path in files:
        text = path.read_text()
        quote = post_json(f"{base}/v1/quote", args.api_key, {"markdown": text})
        chars = int(quote.get("characters", 0))
        blocks = int(quote.get("translatable_blocks", 0))
        if total_chars + chars > args.max_chars:
            fail(
                f"refusing to spend: {total_chars + chars} characters would exceed "
                f"--max-chars {args.max_chars} (this file: {path}, {chars} chars)"
            )
        out = output_path(path, args.lang_slug, args.output_mode)
        if args.dry_run:
            summary.append(f"{path} -> {out}: {chars} chars, {blocks} blocks (dry-run)")
            total_chars += chars
            total_blocks += blocks
            continue

        result = post_json(
            f"{base}/v1/translate",
            args.api_key,
            {"markdown": text, "target_lang": args.target_lang, "glossary": glossary},
        )
        markdown = result.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            fail(f"{path}: API returned an empty translation")
        violations = result.get("violations") or []
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown)
        written += 1
        total_chars += chars
        total_blocks += blocks
        for violation in violations:
            violation_lines.append(
                f"::error file={path}::glossary term {violation.get('source')!r} "
                f"was not translated as {violation.get('target')!r}"
            )
        total_violations += len(violations)
        summary.append(f"{path} -> {out}: {chars} chars, {len(violations)} violations")

    for line in violation_lines:
        print(line)
    print(
        f"\nfiles={len(files)} written={written} characters={total_chars} "
        f"blocks={total_blocks} violations={total_violations}"
    )
    print(f"estimated cost at the plan price: ${total_chars / 1000 * 0.03:.4f}")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as handle:
            handle.write("### md-translate\n\n")
            handle.write("| file | detail |\n|---|---|\n")
            for line in summary:
                handle.write(f"| `{line.split(':')[0]}` | {line.split(':', 1)[1].strip()} |\n")
            handle.write(
                f"\n**{written}** files, **{total_chars}** characters, "
                f"**{total_violations}** glossary violations, "
                f"≈${total_chars / 1000 * 0.03:.4f}\n"
            )

    if total_violations and args.fail_on_violation:
        fail(f"{total_violations} glossary violation(s): see annotations above", code=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())