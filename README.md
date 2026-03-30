# Mnemo

> *Store the intention, not the content.*

**Mnemo** uses the DNS protocol, the internet's distributed phone book, as a generative filesystem. Instead of storing files, it stores the *compressed description* of how to recreate them, using a local LLM as the reconstruction engine.

It's part infrastructure hack, part compression theory experiment, part philosophical provocation.

Named after Mnemosyne, the Greek goddess of memory, because what Mnemo stores isn't data, it's *the memory of how to produce data*.

---

## Inspiration and credit

This project owes its existence to **[doom-over-dns](https://github.com/resumex/doom-over-dns)** by [@resumex](https://github.com/resumex), who had the beautiful and slightly unhinged idea of running DOOM through DNS TXT records.

Discovering that project sparked two questions: *how constrained is DNS really as a storage medium?* And then, *what if the constraint itself was the feature?*

The answer that emerged: instead of fighting the ~500 KB ceiling by packing more data in, sidestep it entirely. Store not the content, but the *intent*. Let a local LLM reconstruct the content on demand. The DNS record becomes a seed. The model becomes the decompressor.

That leap, from "DNS as a weird hard drive" to "DNS as a generative filesystem", is what dnsforge explores. So: thank you doom-over-dns, for making a constrained space feel like an invitation.

---

## The idea in one sentence

> What if you could fit an entire program into a DNS TXT record, and reconstruct it on demand, without ever storing a single byte of the actual output?

---

## Why this is interesting

### DNS as a storage medium

DNS was never designed to store files. A Cloudflare zone caps out around 500–600 KB of TXT records. That's roughly the size of a GameBoy ROM, which is exactly where this project started (yes, you can store *Super Mario Land* in DNS records using base64-encoded, zlib-compressed chunks).

The obvious move: compression + base64 + chunking. It works. But you still hit a ceiling. And that ceiling is the interesting constraint.

### The Kolmogorov angle

In algorithmic information theory, the **Kolmogorov complexity** `K(x)` of a string `x` is defined as the length of the shortest program that outputs `x` on a universal Turing machine. It's the theoretical minimum description length of any piece of data.

A string like `AAAA...AAAA` (one million A's) has very low Kolmogorov complexity, a three-word description suffices. A truly random sequence has Kolmogorov complexity close to its own length, there is no shorter description than the sequence itself.

Most programs humans write sit somewhere in between, they are *structured*, *intentional*, *patterned*. The source code of a sorting algorithm, a game engine, a parser, these have far lower Kolmogorov complexity than their byte count suggests, because the algorithm itself encodes meaning that any informed reader (or model) can reconstruct from a much shorter cue.

**Mnemo exploits this gap.** Instead of storing a file, we store a prompt that describes it precisely enough for a deterministic LLM to regenerate it exactly. The DNS record holds a few hundred bytes of compressed intent. The LLM reconstructs potentially kilobytes or megabytes of structured output , code, data, binaries.

The LLM acts as a **learned approximation of a universal decompressor**: trained on the entire corpus of human-written code and text, it can expand a terse description into a full implementation. The prompt is the compressed form. The output is the decompressed form. DNS is just the storage layer.

This is not Kolmogorov compression in the strict theoretical sense, the LLM is not a universal Turing machine, and the output is not always perfectly reproducible, and that tension is exactly what makes this worth exploring empirically.

> *We are asking: what is the practical Kolmogorov complexity of human-authored programs, as measured by a 7B-parameter language model at temperature=0?*

### The trade-off

| Classic storage | Mnemo |
|---|---|
| Fast retrieval, high storage cost | Slow retrieval (LLM inference), near-zero storage cost |
| Output is static, stored in full | Output is regenerated on demand |
| No compute at read time | CPU/GPU at read time |
| Integrity via hash of stored bytes | Integrity via hash of *generated* output |
| Scales with content size | Scales with description complexity |

This is compression in a new sense: **trading storage space for compute time**, with the LLM as a learned dictionary for human-structured knowledge. The more structured and "human" the content, the better the compression ratio.

---

## How it works

```
┌──────────────────────────────────────────────────────────┐
│  WRITE                                                   │
│                                                          │
│  prompt.json ──► compress ──► base64 ──► DNS TXT record │
│                                                          │
│  { prompt, model, expected_sha256 }    ← ~300 bytes      │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  READ                                                    │
│                                                          │
│  DNS TXT ──► decompress ──► Ollama (temp=0, seed=42)    │
│                                  │                       │
│                                  ▼                       │
│                             generated output             │
│                                  │                       │
│                                  ▼                       │
│             SHA256(output) == expected_sha256 ?          │
│                           ✅ intact / ❌ drift           │
└──────────────────────────────────────────────────────────┘
```

### Multi-stage pipelines

The real power: chain prompts where each stage feeds into the next. Generate a game engine, then extend it with assets, then wrap it in a `main()`. Each stage builds on the previous output as context.

```json
{
  "name": "game_pipeline",
  "stages": [
    { "name": "engine", "prompt": "Write a GameEngine class with a 10x10 grid..." },
    { "name": "items",  "prompt": "Extend it with collectible items...", "input_from": "engine" },
    { "name": "final",  "prompt": "Write a runnable main() that demos it.", "input_from": "items" }
  ]
}
```

The entire pipeline definition , all three stages, fits in DNS. The code it generates never needs to be stored anywhere.

---

## Architecture

```
mnemo/
├── main.py             CLI entry point
├── dns_layer.py        Cloudflare DNS operations (chunks, index, compression)
├── llm_layer.py        Ollama interface (generation, determinism testing)
├── pipeline.py         Multi-stage orchestration + test suite runner
├── test_suite.json     8 benchmark prompts (trivial → very hard)
├── pipeline_game.json  Example 3-stage generative game pipeline
└── config.json         Your DNS + Ollama settings (not committed)
```

---

## Quickstart

### Requirements

```bash
pip install requests

# Ollama must be running locally
ollama serve
ollama pull mistral:7b
```

### Config

Create `config.json` in the project root:

```json
{
  "api_token_file": "cloudflareapi.txt",
  "zone_id": "your_cloudflare_zone_id",
  "domain": "yourdomain.example.com",
  "default_model": "mistral:7b",
  "ollama_url": "http://localhost:11434/api/generate"
}
```

`cloudflareapi.txt`, your Cloudflare API token, one line, no spaces. **Never commit this.**

### Basic usage

```bash
# Store any file in DNS (chunked, compressed, indexed)
python main.py upload myfile.rom

# Retrieve it
python main.py download myfile.rom --out restored.rom

# Store a prompt definition in DNS
python main.py upload-prompt myprompt.json

# Fetch prompt from DNS → run LLM → execute output
python main.py run-dns-prompt myprompt.json --execute

# Run a multi-stage generative pipeline
python main.py run-pipeline pipeline_game.json --execute

# See everything stored in DNS
python main.py list

# Wipe the zone
python main.py purge
```

---

## Test protocol

This is the core research agenda. Run these phases in order and record your results in `results/`.

### Phase 1 — Determinism baseline

*Can we reliably hash LLM outputs? Where does it break?*

```bash
# Quick sanity check (5 runs, same prompt)
python main.py test-determinism \
  "Write a Python script that prints numbers 1 to 100. No explanation. Only code." \
  --runs 5

# Full 8-prompt benchmark (trivial → Conway's Game of Life)
python main.py test-suite test_suite.json --runs 5 --report results/phase1.json
```

We're mapping the **determinism cliff**: the point where prompt complexity outpaces model consistency.

### Phase 2 — Effect of `seed`

*Does `seed=42` in Ollama meaningfully improve reproducibility?*

Toggle `"seed": 42` in `DETERMINISTIC_OPTIONS` inside `llm_layer.py`, rerun the suite, compare JSON reports.

### Phase 3 — Model comparison

*Which local model is most deterministic on code tasks?*

```json
{ "models": ["mistral:7b", "llama3:8b", "codellama:7b", "phi3:mini"] }
```

Hypothesis: code-specialized models have a narrower output distribution on code prompts, making them more deterministic.

### Phase 4 — Multi-stage pipelines

*Does variance compound or average out across stages?*

```bash
python main.py run-pipeline pipeline_game.json
# Run 3 times. Compare SHA256 of the final stage output.
```

### Phase 5 — End-to-end DNS round trip

*Does the full system hold together?*

```bash
python main.py upload-prompt pipeline_game.json
python main.py run-dns-pipeline pipeline_game.json --execute
```

### Phase 6 — Stress tests

```bash
# Pure JSON generation
python main.py test-determinism \
  "Generate a JSON array of 50 fake users with name, email, age. Only JSON." --runs 5

# C code
python main.py test-determinism \
  "Write a C program that prints a 10x10 multiplication table. Only code." --runs 5

# High-complexity: Conway's Game of Life
python main.py test-determinism \
  "Write Python Conway's Game of Life, 10x10 grid, glider pattern, 5 steps, print each. Only code." --runs 3
```

---

## Prompt engineering for determinism

The most reproducible prompts share these traits:

- **Unique obvious solution**: no algorithmic choice left to the model
- **`No explanation. Only code.`** at the end, eliminates preamble variance
- **Precise numeric constraints**: exact sizes, ranges, test inputs
- **Output format specified**, "print as JSON", "one result per line"
- **Test data embedded in the prompt**, don't let the model choose its own examples

The less creative freedom, the more deterministic the output. This is not a bug, it's the design principle.

---

## Expected determinism map

| Prompt type | Stability | Reason |
|---|---|---|
| Simple loop / arithmetic | ✅ High | Single obvious implementation |
| Regex / string parsing | ✅ High | Pattern uniquely constrained |
| Sorting algorithms | ✅ Medium-high | Well-defined classics |
| OOP class design | ⚠️ Medium | Variable naming, method ordering |
| Multi-class programs | ❌ Low | Too many valid implementations |
| Pure JSON output | ✅ High | Structure fully specified |
| C code (simple) | ✅ Medium-high | Less idiomatic variance than Python |
| Complex simulations | ❌ Low | High branching, many conventions |

---

## The deeper question

Mnemo is fundamentally asking: *what is the minimum description length of a program, as understood by a language model?*

Not in the abstract Kolmogorov sense, but empirically, with a specific model, specific parameters, and a specific prompt format. If `mistral:7b` at `temperature=0, seed=42` deterministically maps a 200-byte prompt to a 4 KB Python program, then that prompt *is* the program, for all practical purposes.

The LLM is a **learned compression dictionary** built from human intent. DNS just happens to be a convenient, globally distributed, zero-infrastructure place to store the keys.

What this project wants to find out: how deep does that dictionary go?

---

## Results

Results from test runs will be published in `results/` as the protocol is executed. First results incoming.

---

## Contributing

Contributions especially welcome for:
- Results from different Ollama models or hardware
- Prompts that reliably produce deterministic complex outputs
- Support for other DNS providers (Route53, Gandi, Porkbun...)
- A proper prompt registry format

---

## License

MIT
