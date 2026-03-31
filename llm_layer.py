#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_layer.py — Ollama LLM interface
Handles generation, determinism testing, and hash verification.
"""

import requests
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional


OLLAMA_URL = "http://localhost:11434/api/generate"

# Strict determinism params — temperature=0 forces greedy decoding
DETERMINISTIC_OPTIONS = {
    "temperature": 0,
    "top_p": 1,
    "top_k": 1,
    "seed": 42,
    "num_predict": 2048,
}


@dataclass
class GenerationResult:
    prompt: str
    model: str
    raw_output: str
    extracted_code: Optional[str]
    sha256_raw: str
    sha256_code: Optional[str]
    options: dict = field(default_factory=dict)

    def display(self):
        print(f"\n{'─'*60}")
        print(f"Model  : {self.model}")
        print(f"SHA raw: {self.sha256_raw}")
        if self.extracted_code:
            print(f"SHA cod: {self.sha256_code}")
            print(f"\n--- Extracted code ---\n{self.extracted_code}")
        else:
            print(f"\n--- Raw output ---\n{self.raw_output}")
        print('─'*60)


def extract_code(text: str) -> Optional[str]:
    """Extract code from markdown fences. Returns None if no fences found."""
    pattern = r"```(?:\w+)?\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    if matches:
        return "\n".join(m.strip() for m in matches)
    # If no fences but text looks like pure code, return as-is
    if not any(phrase in text.lower() for phrase in
               ["here", "this script", "the following", "below"]):
        return text.strip()
    return None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class LLMRunner:
    def __init__(self, model: str = "mistral:7b",
                 ollama_url: str = OLLAMA_URL, timeout: int = None):
        self.model = model
        self.ollama_url = ollama_url
        self.timeout = timeout  # None = wait forever
        # Base URL without /api/generate, for model management calls
        self.ollama_base = ollama_url.replace("/api/generate", "")

    def _unload_model(self, model: str):
        """
        Force Ollama to unload the model from memory.
        This ensures the next run starts from a clean state,
        with no residual KV cache or RNG state from previous calls.
        """
        try:
            requests.post(
                f"{self.ollama_base}/api/generate",
                json={"model": model, "keep_alive": 0},
                timeout=10
            )
            time.sleep(1)  # Give Ollama a moment to actually unload
        except Exception:
            pass  # Non-fatal — we'll just note it

    def generate(self, prompt: str, model: str = None,
                 options: dict = None, system: str = None) -> GenerationResult:
        model = model or self.model
        opts = {**DETERMINISTIC_OPTIONS, **(options or {})}

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": opts,
        }
        if system:
            payload["system"] = system

        r = requests.post(self.ollama_url, json=payload, timeout=self.timeout)
        if r.status_code != 200:
            raise Exception(f"Ollama error {r.status_code}: {r.text}")

        raw = r.json()["response"]
        code = extract_code(raw)

        return GenerationResult(
            prompt=prompt,
            model=model,
            raw_output=raw,
            extracted_code=code,
            sha256_raw=sha256(raw),
            sha256_code=sha256(code) if code else None,
            options=opts,
        )

    def test_determinism(self, prompt: str, runs: int = 5,
                         model: str = None, verbose: bool = True,
                         unload_between_runs: bool = True) -> dict:
        """
        Run the same prompt N times with independent Ollama contexts.

        KEY FIX: Between each run, we:
          1. Unload the model from memory (clears KV cache + RNG state)
          2. Vary the seed per run (simulates truly independent executions)

        This means we're testing real cross-session reproducibility,
        not just within-session stability (which was the previous bug).
        """
        model = model or self.model

        if verbose:
            print(f"\n🔬 Determinism test — model={model}, runs={runs}")
            print(f"   Independent runs: {'YES (unload between runs)' if unload_between_runs else 'NO (same session)'}")
            print(f"   Prompt: {prompt[:80]}{'...' if len(prompt)>80 else ''}\n")

        results = []
        raw_hashes = []
        code_hashes = []
        raw_outputs = []

        # Seeds to use across runs — spread across the integer space
        # so each run truly simulates a fresh independent execution
        seeds = [42, 1337, 99999, 7, 123456789]
        seeds = (seeds * ((runs // len(seeds)) + 1))[:runs]

        for i in range(runs):
            if unload_between_runs and i > 0:
                self._unload_model(model)

            # Each run uses a different seed to simulate independent execution
            run_opts = {"seed": seeds[i]}
            result = self.generate(prompt, model=model, options=run_opts)

            results.append(result)
            raw_hashes.append(result.sha256_raw)
            raw_outputs.append(result.raw_output)
            if result.sha256_code:
                code_hashes.append(result.sha256_code)

            status = "✅" if (i == 0 or raw_hashes[-1] == raw_hashes[0]) else "❌"
            if verbose:
                print(f"  Run {i+1}/{runs} [seed={seeds[i]:>10}] {status}"
                      f"  SHA_raw={result.sha256_raw[:16]}…"
                      f"  SHA_code={result.sha256_code[:16] if result.sha256_code else 'N/A'}…")

        unique_raw  = len(set(raw_hashes))
        unique_code = len(set(code_hashes)) if code_hashes else None

        # Diff report: show first divergence if any
        diff_info = None
        if unique_raw > 1:
            for i in range(1, runs):
                if raw_hashes[i] != raw_hashes[0]:
                    # Find first line that differs
                    lines_a = raw_outputs[0].splitlines()
                    lines_b = raw_outputs[i].splitlines()
                    for ln, (a, b) in enumerate(zip(lines_a, lines_b)):
                        if a != b:
                            diff_info = {
                                "first_divergence_run": i + 1,
                                "line": ln + 1,
                                "run1": a[:120],
                                "runN": b[:120],
                            }
                            break
                    break

        report = {
            "model": model,
            "runs": runs,
            "prompt": prompt,
            "seeds_used": seeds,
            "unique_raw_hashes":  unique_raw,
            "unique_code_hashes": unique_code,
            "raw_deterministic":  unique_raw == 1,
            "code_deterministic": unique_code == 1 if unique_code is not None else None,
            "raw_hashes":   raw_hashes,
            "code_hashes":  code_hashes,
            "diff_info":    diff_info,
            # The SHA to store in DNS if deterministic
            "best_sha256":      raw_hashes[0]  if unique_raw  == 1 else None,
            "best_sha256_code": code_hashes[0] if unique_code == 1 else None,
        }

        if verbose:
            print(f"\n📊 Results:")
            print(f"  Raw deterministic : {'✅ YES' if report['raw_deterministic'] else '❌ NO'} "
                  f"({unique_raw} unique hashes over {runs} independent runs)")
            if unique_code is not None:
                print(f"  Code deterministic: {'✅ YES' if report['code_deterministic'] else '❌ NO'} "
                      f"({unique_code} unique hashes)")
            if diff_info:
                print(f"\n  First divergence at run {diff_info['first_divergence_run']}, line {diff_info['line']}:")
                print(f"    Run 1 : {diff_info['run1']}")
                print(f"    Run {diff_info['first_divergence_run']} : {diff_info['runN']}")

        return report

    def run_stage(self, stage: dict, previous_output: str = None) -> GenerationResult:
        """Execute a single pipeline stage."""
        prompt = stage["prompt"]
        if previous_output and stage.get("inject_previous", True):
            prompt = f"{prompt}\n\n# Previous stage output:\n{previous_output}"
        model = stage.get("model", self.model)
        result = self.generate(prompt, model=model)
        expected = stage.get("expected_sha")
        if expected:
            target = result.sha256_code or result.sha256_raw
            ok = target == expected
            print(f"  Stage '{stage['name']}': {'✅ hash match' if ok else '❌ hash MISMATCH'}")
        else:
            print(f"  Stage '{stage['name']}': generated (no expected hash)")
        return result


def load_prompt_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

