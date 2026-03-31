#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
executor.py — Sandboxed code execution with functional hash

The core idea: instead of hashing the *source code* (which varies by hardware),
we hash the *execution output* (which is universal if the code is correct).

    SHA256(source)  → fragile, hardware-dependent
    SHA256(output)  → robust, behaviour-dependent

Safety measures:
  - Timeout:    process killed after N seconds (catches infinite loops)
  - Sandbox:    dangerous builtins patched out before exec (no file writes,
                no network, no os.system)
  - Stderr:     captured and included in result
  - Non-determinism detection: run twice, compare outputs
"""

import subprocess
import sys
import hashlib
import tempfile
import os
import textwrap
from dataclasses import dataclass
from typing import Optional


# ── Sandbox preamble injected at the top of every executed script ─────────────
# Patches out the most dangerous builtins without breaking normal code.
# Not a true security sandbox — just a safety net for accidental side effects.

SANDBOX_PREAMBLE = textwrap.dedent("""
import sys as _sys
import builtins as _builtins

# Block network
import unittest.mock as _mock
_sys.modules['requests']  = _mock.MagicMock()
_sys.modules['urllib']    = _mock.MagicMock()
_sys.modules['urllib.request'] = _mock.MagicMock()
_sys.modules['socket']    = _mock.MagicMock()
_sys.modules['http']      = _mock.MagicMock()
_sys.modules['http.client'] = _mock.MagicMock()

# Block file writes (reads are fine — e.g. reading local data)
_real_open = open
def _safe_open(file, mode='r', *args, **kwargs):
    if isinstance(mode, str) and any(c in mode for c in ('w', 'a', 'x')):
        raise PermissionError(f"[sandbox] file write blocked: {file!r}")
    return _real_open(file, mode, *args, **kwargs)
_builtins.open = _safe_open

# Block os.system / subprocess
import os as _os
_os.system  = lambda *a, **k: (_ for _ in ()).throw(PermissionError("[sandbox] os.system blocked"))
_os.popen   = lambda *a, **k: (_ for _ in ()).throw(PermissionError("[sandbox] os.popen blocked"))

# Seed random for determinism
import random as _random
_random.seed(42)
try:
    import numpy as _np
    _np.random.seed(42)
except ImportError:
    pass

""").lstrip()


@dataclass
class ExecutionResult:
    code: str
    stdout: str
    stderr: str
    exit_code: int          # 0=ok, 1=error, 124=timeout, 125=sandbox block
    timed_out: bool
    sandboxed: bool         # True if a sandbox block was triggered
    sha256_output: Optional[str]   # SHA of stdout if exit_code == 0
    stable: Optional[bool]  # True if two runs produced identical stdout
    run2_stdout: Optional[str] = None  # second run output (for diff inspection)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def display(self):
        print(f"\n{'─'*60}")
        status = "✅ OK" if self.ok else ("⏱ TIMEOUT" if self.timed_out else "❌ ERROR")
        print(f"Execution : {status}  (exit {self.exit_code})")
        if self.stdout:
            preview = self.stdout[:400]
            if len(self.stdout) > 400:
                preview += f"\n... ({len(self.stdout)} chars total)"
            print(f"Output    :\n{preview}")
        if self.stderr:
            print(f"Stderr    : {self.stderr[:300]}")
        if self.sha256_output:
            print(f"SHA output: {self.sha256_output}")
        if self.stable is not None:
            print(f"Stable    : {'✅ YES (2 identical runs)' if self.stable else '❌ NO (non-deterministic output)'}")
        print('─'*60)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Executor:
    def __init__(self, timeout: int = 10, check_stability: bool = True):
        """
        timeout:          seconds before killing the process
        check_stability:  if True, run the code twice and flag non-deterministic output
        """
        self.timeout = timeout
        self.check_stability = check_stability

    def run(self, code: str, label: str = "") -> ExecutionResult:
        """
        Execute code in a subprocess with sandbox preamble and timeout.
        Optionally runs twice to detect non-deterministic output.
        """
        stdout1, stderr1, exit1, timedout1 = self._run_once(code)
        sandboxed = "PermissionError: [sandbox]" in stderr1

        sha = sha256(stdout1) if exit1 == 0 and not timedout1 else None

        stable = None
        stdout2 = None
        if self.check_stability and exit1 == 0 and not timedout1:
            stdout2, _, exit2, _ = self._run_once(code)
            if exit2 == 0:
                stable = (stdout1 == stdout2)
                # If non-deterministic, invalidate the hash
                if not stable:
                    sha = None

        if sandboxed:
            exit1 = 125

        return ExecutionResult(
            code=code,
            stdout=stdout1,
            stderr=stderr1,
            exit_code=exit1,
            timed_out=timedout1,
            sandboxed=sandboxed,
            sha256_output=sha,
            stable=stable,
            run2_stdout=stdout2,
        )

    def _run_once(self, code: str) -> tuple:
        """Run code once, return (stdout, stderr, exit_code, timed_out)."""
        full_code = SANDBOX_PREAMBLE + code

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(full_code)
            tmp_path = tmp.name

        try:
            proc = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            return proc.stdout, proc.stderr, proc.returncode, False

        except subprocess.TimeoutExpired:
            return "", "Process killed after timeout", 124, True

        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


class FunctionalHasher:
    """
    High-level interface for Mnemo's functional hashing strategy.

    Given a prompt → generates code → executes → returns functional SHA256.
    This SHA256 is based on the *output* of the code, not its source.
    It is therefore hardware-independent (as long as the code is correct).
    """

    def __init__(self, executor: Executor = None):
        self.executor = executor or Executor()

    def hash_prompt_result(self, code: str,
                           verbose: bool = True) -> dict:
        """
        Execute code and return a full report including the functional hash.

        Returns a dict suitable for storing in DNS prompt metadata:
        {
            "sha256_source":    <hash of source code>,
            "sha256_output":    <hash of stdout — None if execution failed>,
            "stable":           <True/False/None>,
            "ok":               <bool>,
            "stdout_preview":   <first 200 chars>,
            "error":            <stderr if any>,
        }
        """
        result = self.executor.run(code)

        if verbose:
            result.display()

        report = {
            "sha256_source":  sha256(code),
            "sha256_output":  result.sha256_output,
            "stable":         result.stable,
            "ok":             result.ok,
            "timed_out":      result.timed_out,
            "sandboxed":      result.sandboxed,
            "stdout_preview": result.stdout[:200] if result.stdout else "",
            "error":          result.stderr[:300] if result.stderr else "",
        }

        if verbose:
            if result.sha256_output:
                print(f"\n✅ Functional hash ready for DNS storage:")
                print(f"   {result.sha256_output}")
            elif result.timed_out:
                print(f"\n⏱  Timed out — prompt not suitable for functional hashing")
            elif result.sandboxed:
                print(f"\n🔒 Sandbox triggered — prompt not suitable (side effects)")
            elif not result.ok:
                print(f"\n❌ Execution failed — check stderr above")
            elif result.stable is False:
                print(f"\n⚠️  Non-deterministic output — hash invalidated")
                print(f"   Run 1: {sha256(result.stdout)[:32]}…")
                print(f"   Run 2: {sha256(result.run2_stdout)[:32]}…")

        return report

    def test_suite(self, code_samples: list,
                   verbose: bool = True) -> list:
        """
        Run functional hashing on a list of (name, code) tuples.
        Returns a summary report.
        """
        reports = []
        print(f"\n🧪 Functional hash suite — {len(code_samples)} samples\n{'─'*60}")

        for name, code in code_samples:
            print(f"\n[{name}]")
            report = self.hash_prompt_result(code, verbose=verbose)
            report["name"] = name
            reports.append(report)

        # Summary
        print(f"\n{'─'*60}")
        print(f"{'Name':<30} {'OK':<6} {'Stable':<8} {'SHA output':<36}")
        print('─'*60)
        for r in reports:
            ok     = "✅" if r["ok"] else ("⏱" if r["timed_out"] else "❌")
            stable = ("✅" if r["stable"] else ("❌" if r["stable"] is False else "N/A"))
            sha    = r["sha256_output"][:32] + "…" if r["sha256_output"] else "—"
            print(f"{r['name']:<30} {ok:<6} {stable:<8} {sha:<36}")

        return reports
