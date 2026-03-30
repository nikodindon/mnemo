#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — DNS Generative Storage CLI
Usage: python main.py [command] [options]
"""

import argparse
import json
import os
import sys

from dns_layer import DNSStorage
from llm_layer import LLMRunner
from pipeline import Pipeline

# ─── Config ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if os.path.exists("config.json"):
        with open("config.json") as f:
            return json.load(f)
    # Fallback: env vars or hardcoded
    return {
        "api_token_file": "cloudflareapi.txt",
        "zone_id": "cd6413f95774b23096f366dee3542df8",
        "domain": "nikodindon.dpdns.org",
        "default_model": "mistral:7b",
        "ollama_url": "http://localhost:11434/api/generate",
    }

def build_services(cfg: dict):
    token = open(cfg["api_token_file"]).read().strip()
    dns = DNSStorage(token, cfg["zone_id"], cfg["domain"])
    llm = LLMRunner(model=cfg["default_model"], ollama_url=cfg["ollama_url"])
    pipe = Pipeline(dns, llm)
    return dns, llm, pipe

# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="DNS Generative Storage — file storage + AI pipeline over DNS",
        formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── DNS file operations ──────────────────────────────────────────────────
    up = sub.add_parser("upload", help="Upload a file to DNS")
    up.add_argument("path", help="File to upload")
    up.add_argument("--chunk-size", type=int, default=1000)
    up.add_argument("--gzip", action="store_true")

    dl = sub.add_parser("download", help="Download a file from DNS")
    dl.add_argument("filename")
    dl.add_argument("--out", default=None, help="Output path")

    sub.add_parser("list", help="List files stored in DNS")
    sub.add_parser("purge", help="Delete all DNS records")

    # ── Prompt operations ────────────────────────────────────────────────────
    rp = sub.add_parser("run-prompt", help="Run a prompt JSON file locally")
    rp.add_argument("path", help="Path to prompt .json")
    rp.add_argument("--execute", action="store_true", help="Execute generated Python")
    rp.add_argument("--model", default=None)

    rd = sub.add_parser("run-dns-prompt",
                         help="Fetch prompt from DNS and run it")
    rd.add_argument("filename")
    rd.add_argument("--execute", action="store_true")

    # ── Pipeline operations ──────────────────────────────────────────────────
    pl = sub.add_parser("run-pipeline", help="Run a multi-stage pipeline JSON")
    pl.add_argument("path", help="Path to pipeline .json")
    pl.add_argument("--execute", action="store_true", help="Execute final stage output")

    pd = sub.add_parser("run-dns-pipeline",
                         help="Fetch pipeline from DNS and run it")
    pd.add_argument("filename")
    pd.add_argument("--execute", action="store_true")

    # ── Determinism testing ──────────────────────────────────────────────────
    dt = sub.add_parser("test-determinism",
                         help="Test prompt determinism across N runs")
    dt.add_argument("prompt", nargs="?", default=None,
                    help="Prompt string (or use --file)")
    dt.add_argument("--file", default=None, help="Prompt .json file")
    dt.add_argument("--runs", type=int, default=5)
    dt.add_argument("--model", default=None)

    ts = sub.add_parser("test-suite",
                         help="Run full determinism test suite from JSON")
    ts.add_argument("path", help="Path to test suite .json")
    ts.add_argument("--runs", type=int, default=5)
    ts.add_argument("--report", default=None, help="Save JSON report to file")

    # ── Upload prompt/pipeline ────────────────────────────────────────────────
    up2 = sub.add_parser("upload-prompt",
                          help="Upload a prompt or pipeline JSON to DNS")
    up2.add_argument("path")
    up2.add_argument("--gzip", action="store_true")

    args = parser.parse_args()

    cfg = load_config()
    dns, llm, pipe = build_services(cfg)

    if args.model if hasattr(args, "model") and args.model else False:
        llm.model = args.model
        pipe.llm.model = args.model

    # ── Dispatch ──────────────────────────────────────────────────────────────

    if args.cmd == "upload":
        compression = "gzip" if args.gzip else "zlib"
        dns.upload_file(args.path, args.chunk_size, compression)

    elif args.cmd == "download":
        out = args.out or f"reconstructed_{args.filename}"
        dns.download_file(args.filename, output_path=out)

    elif args.cmd == "list":
        files = dns.list_files()
        if not files:
            print("No files stored in DNS.")
        for name, meta in files.items():
            print(f"\n📄 {name}")
            for k, v in meta.items():
                print(f"  {k}: {v}")

    elif args.cmd == "purge":
        n = dns.purge_all()
        print(f"🧹 {n} records deleted")

    elif args.cmd == "run-prompt":
        pipe.run_prompt_file(args.path, execute=args.execute)

    elif args.cmd == "run-dns-prompt":
        pipe.run_prompt_from_dns(args.filename, execute=args.execute)

    elif args.cmd == "run-pipeline":
        pipe.run_pipeline_file(args.path, execute_final=args.execute)

    elif args.cmd == "run-dns-pipeline":
        pipe.run_pipeline_from_dns(args.filename, execute_final=args.execute)

    elif args.cmd == "test-determinism":
        prompt = args.prompt
        if args.file:
            with open(args.file) as f:
                data = json.load(f)
            prompt = data["prompt"]
        if not prompt:
            print("Error: provide a prompt string or --file")
            sys.exit(1)
        llm.test_determinism(prompt, runs=args.runs, model=args.model or llm.model)

    elif args.cmd == "test-suite":
        with open(args.path) as f:
            suite = json.load(f)
        models = suite.get("models", [llm.model])
        pipe.run_determinism_suite(
            suite["prompts"],
            runs=args.runs,
            models=models,
            save_report=args.report,
        )

    elif args.cmd == "upload-prompt":
        compression = "gzip" if args.gzip else "zlib"
        dns.upload_file(args.path, compression=compression)


if __name__ == "__main__":
    main()
