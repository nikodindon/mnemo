#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dns_layer.py — Cloudflare DNS storage abstraction
Handles all read/write operations to DNS TXT records.
"""

import requests
import base64
import zlib
import gzip
import json
import hashlib
import os


class DNSStorage:
    def __init__(self, api_token: str, zone_id: str, domain: str):
        self.domain = domain
        self.zone_id = zone_id
        self.cf_api = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        self.index_name = f"index.{domain}"

    # ─── Raw DNS operations ───────────────────────────────────────────────────

    def create_record(self, name: str, content: str, ttl: int = 120):
        data = {"type": "TXT", "name": name, "content": content, "ttl": ttl}
        r = requests.post(self.cf_api, headers=self.headers, json=data)
        result = r.json()
        if not result.get("success"):
            raise Exception(f"DNS create failed: {result}")
        return result

    def list_records(self) -> list:
        r = requests.get(self.cf_api, headers=self.headers)
        return r.json().get("result", [])

    def delete_record(self, record_id: str):
        requests.delete(f"{self.cf_api}/{record_id}", headers=self.headers)

    def purge_all(self) -> int:
        records = self.list_records()
        for r in records:
            self.delete_record(r["id"])
        return len(records)

    def delete_by_name(self, name: str):
        for r in self.list_records():
            if r["name"] == name:
                self.delete_record(r["id"])

    # ─── Compression ──────────────────────────────────────────────────────────

    @staticmethod
    def compress(data: bytes, mode: str = "zlib") -> bytes:
        if mode == "gzip":
            return gzip.compress(data)
        return zlib.compress(data)

    @staticmethod
    def decompress(data: bytes, mode: str = "zlib") -> bytes:
        try:
            if mode == "gzip":
                return gzip.decompress(data)
            return zlib.decompress(data)
        except Exception:
            try:
                return gzip.decompress(data)
            except Exception:
                return zlib.decompress(data)

    # ─── Index management ────────────────────────────────────────────────────

    def get_index(self) -> dict:
        for r in self.list_records():
            if r["name"] == self.index_name:
                try:
                    return json.loads(r["content"])
                except json.JSONDecodeError:
                    pass
        return {}

    def set_index(self, index: dict):
        self.delete_by_name(self.index_name)
        self.create_record(self.index_name, json.dumps(index))

    def update_index_entry(self, filename: str, meta: dict):
        index = self.get_index()
        index[filename] = meta
        self.set_index(index)

    # ─── File upload / download ───────────────────────────────────────────────

    def upload_file(self, path: str, chunk_size: int = 1000,
                    compression: str = "zlib", verbose: bool = True) -> dict:
        filename = os.path.basename(path)

        with open(path, "rb") as f:
            raw = f.read()

        sha256 = hashlib.sha256(raw).hexdigest()
        compressed = self.compress(raw, compression)
        encoded = base64.b64encode(compressed).decode()
        chunks = [encoded[i:i+chunk_size] for i in range(0, len(encoded), chunk_size)]

        if verbose:
            print(f"📤 Upload '{filename}' → {len(chunks)} chunks ({compression})")

        for i, chunk in enumerate(chunks):
            name = f"p{i}.{filename}.{self.domain}"
            self.create_record(name, chunk)
            if verbose:
                print(f"  chunk {i+1}/{len(chunks)}", end="\r")

        meta = {"chunks": len(chunks), "compression": compression, "sha256": sha256}
        self.update_index_entry(filename, meta)

        if verbose:
            print(f"\n✅ Upload done — SHA256: {sha256}")

        return meta

    def download_file(self, filename: str, output_path: str = None,
                      verbose: bool = True) -> bytes:
        index = self.get_index()
        if filename not in index:
            raise FileNotFoundError(f"'{filename}' not found in DNS index")

        meta = index[filename]
        n_chunks = meta["chunks"]
        compression = meta["compression"]
        sha_expected = meta["sha256"]

        if verbose:
            print(f"📥 Download '{filename}' ({n_chunks} chunks, {compression})")

        records = self.list_records()
        domain_parts = len(self.domain.split("."))
        chunks = {}

        for r in records:
            name = r["name"]
            parts = name.split(".")
            if not parts[0].startswith("p") or not parts[0][1:].isdigit():
                continue
            rec_filename = ".".join(parts[1:-domain_parts])
            if rec_filename != filename:
                continue
            chunks[int(parts[0][1:])] = r["content"]

        if len(chunks) != n_chunks:
            print(f"⚠️  Missing chunks: got {len(chunks)}/{n_chunks}")

        encoded = "".join(chunks[i] for i in sorted(chunks.keys()))
        raw = self.decompress(base64.b64decode(encoded), compression)

        sha_actual = hashlib.sha256(raw).hexdigest()
        if verbose:
            ok = "✅" if sha_actual == sha_expected else "❌"
            print(f"{ok} SHA256: {sha_actual}")

        if output_path:
            with open(output_path, "wb") as f:
                f.write(raw)
            if verbose:
                print(f"💾 Written to {output_path}")

        return raw

    def list_files(self) -> dict:
        return self.get_index()
