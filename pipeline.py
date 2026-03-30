#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py — Multi-stage generative pipeline over DNS
Orchestrates prompt chains: fetch stages from DNS, generate, verify hashes.
"""

import json
import hashlib
import subprocess
import sys
import os
import tempfile
from pathlib import Path
from typing import Optional

from llm_layer import LLMRunner, GenerationResult, extract_code, sha256
from dns_layer import DNSStorage


class Pipeline:
    def __init__(self, dns: DNSStorage, llm: LLMRunner, verbose: bool = True):
        self.dns = dns
        self.llm = llm
        self.verbose = verbose

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    # ─── Single prompt ────────────────────────────────────────────────────────

    def run_prompt_dict(self, prompt_data: dict,
                        execute: bool = False, save_to: str = None) -> GenerationResult:
        """Run a single prompt definition dict."""
        result = self.llm.generate(
            prompt=prompt_data["prompt"],
            model=prompt_data.get("model", self.llm.model),
        )
        result.display()

        expected = prompt_data.get("expected_sha256")
        if expected:
            actual = result.sha256_code or result.sha256_raw
            if actual == expected:
                print("✅ Hash match")
            else:
                print(f"❌ Hash mismatch\n  Expected: {expected}\n  Got:      {actual}")

        output = result.extracted_code or result.raw_output

        if save_to:
            Path(save_to).write_text(output, encoding="utf-8")
            self._log(f"💾 Saved to {save_to}")

        if execute:
            self._execute_python(output, prompt_data.get("name", "generated"))

        return result

    def run_prompt_file(self, path: str, execute: bool = False) -> GenerationResult:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return self.run_prompt_dict(data, execute=execute)

    def run_prompt_from_dns(self, filename: str, execute: bool = False) -> GenerationResult:
        raw = self.dns.download_file(filename, verbose=self.verbose)
        data = json.loads(raw.decode("utf-8"))
        return self.run_prompt_dict(data, execute=execute)

    # ─── Multi-stage pipeline ─────────────────────────────────────────────────

    def run_pipeline(self, pipeline_def: dict, execute_final: bool = False) -> dict:
        """
        Execute a multi-stage pipeline.

        pipeline_def format:
        {
            "name": "my_pipeline",
            "stages": [
                {
                    "name": "stage_name",
                    "prompt": "...",
                    "model": "mistral:7b",          # optional, inherits default
                    "expected_sha": "...",           # optional, for verification
                    "inject_previous": true,         # inject previous output into prompt
                    "output_type": "code|text",      # how to extract/use output
                    "execute": false                 # run this stage's output
                },
                ...
            ]
        }
        """
        stages = pipeline_def["stages"]
        name = pipeline_def.get("name", "pipeline")

        self._log(f"\n🚀 Pipeline '{name}' — {len(stages)} stages\n{'─'*60}")

        outputs = {}        # name → GenerationResult
        previous_text = None
        all_ok = True

        for i, stage in enumerate(stages):
            stage_name = stage["name"]
            self._log(f"\n[Stage {i+1}/{len(stages)}] '{stage_name}'")

            # Inject previous output into prompt if requested
            prompt = stage["prompt"]
            inject_from = stage.get("input_from")
            if inject_from and inject_from in outputs:
                prev = outputs[inject_from]
                prev_text = prev.extracted_code or prev.raw_output
                prompt = f"{prompt}\n\n# Input from stage '{inject_from}':\n{prev_text}"
            elif previous_text and stage.get("inject_previous", False):
                prompt = f"{prompt}\n\n# Previous output:\n{previous_text}"

            result = self.llm.generate(
                prompt=prompt,
                model=stage.get("model", self.llm.model),
            )

            # Hash verification
            expected = stage.get("expected_sha")
            if expected:
                actual = result.sha256_code or result.sha256_raw
                ok = actual == expected
                all_ok = all_ok and ok
                status = "✅" if ok else "❌"
                self._log(f"  {status} Hash: {actual[:32]}…")
            else:
                output_text = result.extracted_code or result.raw_output
                self._log(f"  SHA_raw={result.sha256_raw[:32]}…")
                if result.sha256_code:
                    self._log(f"  SHA_code={result.sha256_code[:32]}…")

            outputs[stage_name] = result
            previous_text = result.extracted_code or result.raw_output

            # Optional per-stage execution
            if stage.get("execute"):
                self._log(f"  ▶ Executing stage '{stage_name}'…")
                self._execute_python(previous_text, stage_name)

        self._log(f"\n{'─'*60}")
        self._log(f"Pipeline complete — {'✅ all hashes OK' if all_ok else '⚠️  some hashes mismatched'}")

        # Execute final stage output if requested
        if execute_final and previous_text:
            self._execute_python(previous_text, "final")

        return outputs

    def run_pipeline_file(self, path: str, execute_final: bool = False) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return self.run_pipeline(data, execute_final=execute_final)

    def run_pipeline_from_dns(self, filename: str, execute_final: bool = False) -> dict:
        raw = self.dns.download_file(filename, verbose=self.verbose)
        data = json.loads(raw.decode("utf-8"))
        return self.run_pipeline(data, execute_final=execute_final)

    # ─── Determinism test suite ───────────────────────────────────────────────

    def run_determinism_suite(self, prompts: list, runs: int = 5,
                               models: list = None, save_report: str = None) -> list:
        """
        Run a battery of determinism tests across prompts and models.
        Returns list of report dicts.
        """
        models = models or [self.llm.model]
        reports = []

        self._log(f"\n🧪 Determinism suite — {len(prompts)} prompts × {len(models)} models × {runs} runs\n")

        for model in models:
            for prompt_def in prompts:
                prompt = prompt_def if isinstance(prompt_def, str) else prompt_def["prompt"]
                label = prompt_def.get("name", prompt[:40]) if isinstance(prompt_def, dict) else prompt[:40]

                report = self.llm.test_determinism(prompt, runs=runs, model=model)
                report["label"] = label
                reports.append(report)

        # Summary table
        self._log(f"\n{'─'*70}")
        self._log(f"{'Label':<35} {'Model':<15} {'Raw OK':<10} {'Code OK':<10}")
        self._log('─'*70)
        for r in reports:
            raw_ok = "✅" if r["raw_deterministic"] else f"❌ ({r['unique_raw_hashes']} variants)"
            code_ok = ("✅" if r["code_deterministic"] else f"❌ ({r['unique_code_hashes']} variants)") \
                      if r["code_deterministic"] is not None else "N/A"
            self._log(f"{r['label'][:35]:<35} {r['model'][:15]:<15} {raw_ok:<10} {code_ok:<10}")

        if save_report:
            with open(save_report, "w", encoding="utf-8") as f:
                json.dump(reports, f, indent=2)
            self._log(f"\n📄 Report saved to {save_report}")

        return reports

    # ─── Utilities ────────────────────────────────────────────────────────────

    def _execute_python(self, code: str, label: str = ""):
        """Execute Python code in a subprocess and stream output."""
        self._log(f"\n▶ Running Python ({label})…\n{'─'*40}")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                         delete=False, encoding="utf-8") as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=False, text=True
            )
            if result.returncode != 0:
                self._log(f"\n⚠️  Exit code {result.returncode}")
        finally:
            os.unlink(tmp_path)
        self._log('─'*40)

    def compile_c(self, code: str, output_name: str = "output") -> Optional[str]:
        """Compile C code with gcc, return path to binary or None."""
        src = f"/tmp/{output_name}.c"
        bin_path = f"/tmp/{output_name}"
        Path(src).write_text(code, encoding="utf-8")
        result = subprocess.run(["gcc", src, "-o", bin_path], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"❌ Compilation failed:\n{result.stderr}")
            return None
        print(f"✅ Compiled → {bin_path}")
        return bin_path
