#!/usr/bin/env python3
"""Tests for the context-boundary tester (stdlib unittest)."""
import abc
import json
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import boundary_tester as bt  # noqa: E402


class TestCanariesAreSynthetic(unittest.TestCase):
    def test_canaries_clearly_marked_synthetic(self):
        self.assertIn("000", bt.CANARIES["CANARY_SSN"], "SSN must use un-issuable 000 area")
        self.assertIn("SYNTHETIC", bt.CANARIES["CANARY_API_KEY"])
        self.assertTrue(
            bt.CANARIES["CANARY_EMAIL"].endswith(".invalid"),
            "email must use the non-resolvable .invalid TLD",
        )
        for label, value in bt.CANARIES.items():
            markers = ("CANARY", "SYNTHETIC", "synth", ".invalid", "000")
            self.assertTrue(
                any(m in value for m in markers),
                f"{label} must carry a synthetic marker: {value!r}",
            )


class TestAdapterContract(unittest.TestCase):
    def test_contract_is_three_abstract_methods(self):
        abstract = bt.AgentAdapter.__abstractmethods__
        self.assertEqual(set(abstract), {"name", "describe", "invoke"})

    def test_mock_adapter_implements_contract(self):
        for mode in ("clean", "leaky"):
            a = bt.MockAdapter(mode=mode)
            self.assertIsInstance(a.name(), str)
            self.assertIsInstance(a.describe(), str)
            log = a.invoke("some context")
            self.assertIsInstance(log, bt.ToolCallLog)
            for call in log.calls:
                self.assertIsInstance(call.name, str)
                self.assertIsInstance(call.arguments, str)

    def test_mock_adapter_rejects_bad_mode(self):
        with self.assertRaises(ValueError):
            bt.MockAdapter(mode="evil")

    def test_http_adapter_implements_contract(self):
        a = bt.GenericHTTPAdapter("http://localhost:9/nope")
        self.assertIsInstance(a.name(), str)
        self.assertIn("localhost", a.describe())


class TestMockScenarios(unittest.TestCase):
    def setUp(self):
        self.scenarios = {s.name: s for s in bt.build_scenarios()}

    def test_leaky_mock_direct_leak_names_canary(self):
        r = bt.run_scenario(bt.MockAdapter("leaky"), self.scenarios["direct-leak"])
        self.assertEqual(r.verdict, "LEAK")
        self.assertIn("CANARY_SSN", r.leaked_canaries)

    def test_leaky_mock_indirect_leak(self):
        r = bt.run_scenario(bt.MockAdapter("leaky"), self.scenarios["indirect-leak"])
        self.assertEqual(r.verdict, "LEAK")
        self.assertIn("CANARY_EMAIL", r.leaked_canaries)

    def test_leaky_mock_clean_control_is_clean(self):
        r = bt.run_scenario(bt.MockAdapter("leaky"), self.scenarios["clean-control"])
        self.assertEqual(r.verdict, "CLEAN")
        self.assertEqual(r.leaked_canaries, [])

    def test_clean_mock_is_clean_everywhere(self):
        for s in bt.build_scenarios():
            r = bt.run_scenario(bt.MockAdapter("clean"), s)
            self.assertEqual(r.verdict, "CLEAN", f"clean mock leaked on {s.name}")

    def test_three_scenarios_shipped(self):
        self.assertEqual(
            sorted(self.scenarios), ["clean-control", "direct-leak", "indirect-leak"]
        )


class TestGenericHTTPAdapter(unittest.TestCase):
    def _serve(self, payload):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.server.received = self.rfile.read(length)
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_leak_detected_through_http(self):
        canary = bt.CANARIES["CANARY_API_KEY"]
        server = self._serve({"tool_calls": [{"name": "webhook", "arguments": f"key={canary}"}]})
        try:
            url = f"http://127.0.0.1:{server.server_port}/agent"
            adapter = bt.GenericHTTPAdapter(url)
            log = adapter.invoke("context with " + canary)
            self.assertEqual(len(log.calls), 1)
            self.assertIn(canary, log.calls[0].arguments)
            scenario = bt.Scenario("s", "ctx " + canary, "d")
            r = bt.run_scenario(adapter, scenario)
            self.assertEqual(r.verdict, "LEAK")
            self.assertIn("CANARY_API_KEY", r.leaked_canaries)
        finally:
            server.shutdown()

    def test_context_is_posted_as_json(self):
        server = self._serve({"tool_calls": []})
        try:
            url = f"http://127.0.0.1:{server.server_port}/agent"
            bt.GenericHTTPAdapter(url).invoke("hello context")
            posted = json.loads(server.received.decode())
            self.assertEqual(posted, {"context": "hello context"})
        finally:
            server.shutdown()

    def test_custom_tool_calls_path(self):
        server = self._serve({"result": {"actions": [{"name": "t", "arguments": "ok"}]}})
        try:
            url = f"http://127.0.0.1:{server.server_port}/agent"
            log = bt.GenericHTTPAdapter(url, tool_calls_path="result.actions").invoke("x")
            self.assertEqual(log.calls[0].name, "t")
        finally:
            server.shutdown()

    def test_non_string_arguments_coerced(self):
        server = self._serve({"tool_calls": [{"name": "t", "arguments": {"a": 1}}]})
        try:
            url = f"http://127.0.0.1:{server.server_port}/agent"
            log = bt.GenericHTTPAdapter(url).invoke("x")
            self.assertIsInstance(log.calls[0].arguments, str)
            self.assertIn('"a": 1', log.calls[0].arguments)
        finally:
            server.shutdown()

    def test_missing_path_raises(self):
        server = self._serve({"nope": []})
        try:
            url = f"http://127.0.0.1:{server.server_port}/agent"
            with self.assertRaises(ValueError):
                bt.GenericHTTPAdapter(url).invoke("x")
        finally:
            server.shutdown()


class TestReportRendering(unittest.TestCase):
    def test_table_shows_verdicts_and_summary(self):
        results = bt.run_suite(bt.MockAdapter("leaky"))
        table = bt.render_table(results)
        for name in ("direct-leak", "indirect-leak", "clean-control"):
            self.assertIn(name, table)
        self.assertIn("LEAK", table)
        self.assertIn("CLEAN", table)
        self.assertIn("2/3 scenarios leaked", table)
        self.assertIn("CANARY_SSN", table)


class TestCLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "boundary_tester.py"), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_demo_runs_both_mocks(self):
        r = self.run_cli("--demo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("mock(leaky)", r.stdout)
        self.assertIn("mock(clean)", r.stdout)
        self.assertIn("CONTEXT-BOUNDARY TEST RESULTS", r.stdout)

    def test_list_canaries(self):
        r = self.run_cli("--list-canaries")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CANARY_SSN", r.stdout)
        self.assertIn("SYNTHETIC", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
