"""Tests for the GitHub Action entrypoint (translate_docs.py).

Deterministic tests run against a local stub API, so they assert *our* behaviour (cost
guard, glossary gate, fail-closed on upstream errors) without depending on a model's
mood. The real-API smoke test lives behind `--real` and is documented in README.md.

Run: python3 -m pytest tests -q     (or: python3 tests/test_action.py --real)
"""

from __future__ import annotations

import http.server
import json
import pathlib
import subprocess
import sys
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "translate_docs.py"


class StubHandler(http.server.BaseHTTPRequestHandler):
    """Quote 100 chars / 2 blocks; translate either cleanly or with one violation."""

    violate = False
    boom = False

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/v1/quote"):
            body = {"characters": 100, "translatable_blocks": 2, "estimated_usd": 0.003}
        elif self.boom:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"detail":"upstream exploded"}')
            return
        else:
            body = {
                "markdown": payload["markdown"],
                "translatable_blocks": 2,
                "translated_blocks": 2,
                "violations": ([{"index": 1, "source": "grinder", "target": "磨核"}]
                               if self.violate else []),
                "upstream_model": "stub",
            }
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr logging."""


class ActionTests(unittest.TestCase):
    server: http.server.ThreadingHTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def run_action(self, tmp: pathlib.Path, *extra: str) -> subprocess.CompletedProcess[str]:
        (tmp / "docs").mkdir(parents=True, exist_ok=True)
        (tmp / "docs" / "guide.md").write_text("# Title\n\nBody text here.\n")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--api-url", self.base, "--api-key", "k",
             "--target-lang", "into Chinese", "--lang-slug", "zh-CN",
             "--paths", str(tmp / "docs" / "*.md"), *extra],
            capture_output=True, text=True, cwd=tmp,
        )

    def test_translates_and_reports_cost(self) -> None:
        import tempfile

        StubHandler.violate = StubHandler.boom = False
        with tempfile.TemporaryDirectory() as raw:
            tmp = pathlib.Path(raw)
            result = self.run_action(tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("characters=100", result.stdout)
            out = tmp / "docs" / "guide.zh-CN.md"
            self.assertTrue(out.exists(), "output file must be written next to the source")
            self.assertIn("# Title", out.read_text())

    def test_violation_fails_the_build(self) -> None:
        import tempfile

        StubHandler.violate, StubHandler.boom = True, False
        try:
            with tempfile.TemporaryDirectory() as raw:
                result = self.run_action(pathlib.Path(raw), "--fail-on-violation")
                self.assertEqual(result.returncode, 1)
                self.assertIn("::error file=", result.stdout)
        finally:
            StubHandler.violate = False

    def test_upstream_error_is_fail_closed(self) -> None:
        import tempfile

        StubHandler.boom = True
        try:
            with tempfile.TemporaryDirectory() as raw:
                result = self.run_action(pathlib.Path(raw))
                self.assertEqual(result.returncode, 2)
                self.assertIn("HTTP 500", result.stdout + result.stderr)
        finally:
            StubHandler.boom = False

    def test_max_chars_guard_refuses_to_spend(self) -> None:
        import tempfile

        StubHandler.violate = StubHandler.boom = False
        with tempfile.TemporaryDirectory() as raw:
            result = self.run_action(pathlib.Path(raw), "--max-chars", "50")
            self.assertEqual(result.returncode, 2)
            self.assertIn("refusing to spend", result.stdout + result.stderr)

    def test_missing_api_key_is_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = pathlib.Path(raw)
            (tmp / "docs").mkdir()
            (tmp / "docs" / "a.md").write_text("hi\n")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--api-url", self.base,
                 "--target-lang", "into Chinese", "--lang-slug", "zh",
                 "--paths", str(tmp / "docs" / "*.md")],
                capture_output=True, text=True, cwd=tmp, env={"PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("no API key", result.stdout + result.stderr)


def real_api_smoke() -> int:
    """Translate a real document through the deployed API (no stub)."""
    import tempfile

    api_url = "https://jj-macbook-air.bigeye-caiman.ts.net"
    key = (HERE.parent.parent / "md-translate-api" / "data" / "client_key.txt").read_text().strip()
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        (tmp / "docs").mkdir()
        (tmp / "docs" / "sample.md").write_text(
            "# Prompt Guide\n\nThe **pinning** grinder recovers the key.\n\n"
            "```python\nx = 1  # untouched\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        )
        (tmp / "glossary.json").write_text(
            json.dumps([{"source": "grinder", "target": "磨核"}, {"source": "prompt", "target": "提示词"}])
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--api-url", api_url, "--api-key", key,
             "--target-lang", "into Simplified Chinese", "--lang-slug", "zh-CN",
             "--paths", str(tmp / "docs" / "*.md"),
             "--glossary", str(tmp / "glossary.json"), "--fail-on-violation"],
            capture_output=True, text=True, cwd=tmp,
        )
        print(result.stdout + result.stderr)
        out = tmp / "docs" / "sample.zh-CN.md"
        if out.exists():
            print("--- 产出 ---")
            print(out.read_text())
        return result.returncode


if __name__ == "__main__":
    if "--real" in sys.argv:
        raise SystemExit(real_api_smoke())
    unittest.main(verbosity=2)