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
from dataclasses import dataclass, field
from typing import Optional


OLLAMA_URL = "http://localhost:11434/api/generate"

# Strict determinism params — temperature=0 forces greedy decoding
DETERMINISTIC_OPTIONS = {
    "temperature": 0,
    "top_p": 1,
    "top_k": 1,
    "seed": 42,          # explicit seed for reproducibility (Ollama ≥ 0.1.26)
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
    """Extract code from markdown fences, or return None if no fences found."""
    pattern = r"```(?:\w+)?\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    if matches:
        return "\n".join(m.strip() for m in matches)
    # If no fences but text looks like pure code, return as-is
    if not any(phrase in text.lower() for phrase in ["here", "this script", "the following", "below"]):
        return text.strip()
    return None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class LLMRunner:
    def __init__(self, model: str = "mistral:7b", ollama_url: str = OLLAMA_URL):
        self.model = model
        self.ollama_url = ollama_url

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

        r = requests.post(self.ollama_url, json=payload, timeout=120)
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
                         model: str = None, verbose: bool = True) -> dict:
        """
        Run the same prompt N times and measure SHA256 consistency.
        Returns a report dict with stats.
        """
        model = model or self.model
        if verbose:
            print(f"\n🔬 Determinism test — model={model}, runs={runs}")
            print(f"   Prompt: {prompt[:80]}{'...' if len(prompt)>80 else ''}\n")

        results = []
        raw_hashes = []
        code_hashes = []

        for i in range(runs):
            result = self.generate(prompt, model=model)
            results.append(result)
            raw_hashes.append(result.sha256_raw)
            if result.sha256_code:
                code_hashes.append(result.sha256_code)

            status = "✅" if (i == 0 or raw_hashes[-1] == raw_hashes[0]) else "❌"
            if verbose:
                print(f"  Run {i+1}/{runs} {status}  SHA_raw={result.sha256_raw[:16]}…  "
                      f"SHA_code={result.sha256_code[:16] if result.sha256_code else 'N/A'}…")

        unique_raw = len(set(raw_hashes))
        unique_code = len(set(code_hashes)) if code_hashes else None

        report = {
            "model": model,
            "runs": runs,
            "prompt": prompt,
            "unique_raw_hashes": unique_raw,
            "unique_code_hashes": unique_code,
            "raw_deterministic": unique_raw == 1,
            "code_deterministic": unique_code == 1 if unique_code is not None else None,
            "raw_hashes": raw_hashes,
            "code_hashes": code_hashes,
            "best_sha256": raw_hashes[0] if unique_raw == 1 else None,
            "best_sha256_code": code_hashes[0] if unique_code == 1 else None,
        }

        if verbose:
            print(f"\n📊 Results:")
            print(f"  Raw deterministic : {'✅ YES' if report['raw_deterministic'] else '❌ NO'} "
                  f"({unique_raw} unique hashes)")
            if unique_code is not None:
                print(f"  Code deterministic: {'✅ YES' if report['code_deterministic'] else '❌ NO'} "
                      f"({unique_code} unique hashes)")

        return report

    def run_stage(self, stage: dict, previous_output: str = None) -> GenerationResult:
        """
        Execute a single pipeline stage.
        stage = { "name", "prompt", "model", "expected_sha", "inject_previous" }
        """
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
