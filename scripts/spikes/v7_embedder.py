"""Spike V7 (spec §22 V7, §19.7; Task 21): the arctic-embed-l-v2.0 ONNX exports in this container.

    embedder/download_model.sh .model-cache/arctic          # the locked files: fp32 + tokenizer
    uv run --group embedder python scripts/spikes/v7_embedder.py .model-cache/arctic \\
        [--onnx onnx/model_int8.onnx --fp32 .model-cache/arctic]
    uv run --group embedder python scripts/spikes/v7_embedder.py .model-cache/arctic --padded-batch 16 \\
        [--onnx onnx/model_int8.onnx] [--fp32 .model-cache/arctic]

It measures the export that embedder/model.lock pins (fp32 since ruling T21-a), or the one `--onnx` names (the
int8, fp16 and fp32 files are known by their sha256 in EXPORTS; an int8 or fp16 file is downloaded beside the
locked ones with a scratch lock of its sha256, docs/spikes/V7.md). `--fp32` names a directory holding the fp32
export (`onnx/model.onnx`, `onnx/model.onnx_data`, `tokenizer.json`, checked against FP32_FILES), the parity
reference.

The main run loads `embedder.app.OnnxModel` (the service's own code: one inference per text, truncation at
1,024 tokens, the export's `sentence_embedding`) and embeds the descriptions of 50 ads of the committed
scrubbed fixtures (never `.fixtures-raw/`), in requests of 16 as the embed node sends them. It prints the
docs/s of the ads as they are and of the same ads lengthened to exactly 1,024 tokens, the peak RSS
(`resource.getrusage`), the cosine parity of every ad against the fp32 export, and the query-prefix check.
`--padded-batch N` instead measures the alternative OnnxModel does not take: the 1,024-token ads as padded
batches of N, and how a short ad's vector moves when it is padded next to a 1,024-token one. It runs alone,
so its peak RSS is its own.

Only numbers are printed, never an ad's text.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import resource
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

ROOT: Final = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # `embedder` is a directory of the repository root, not an installed package

from jobhunter.sources.arbeitnow import _description as arbeitnow_description  # noqa: E402
from jobhunter.sources.parsing.description import plain_description  # noqa: E402

from embedder.app import MAX_BATCH, MAX_TOKENS, OnnxModel, locked_onnx_file, verify_checksums  # noqa: E402

FIXTURES: Final = ROOT / "tests" / "contract" / "fixtures"
LOCK: Final = ROOT / "embedder" / "model.lock"
# The fp32 reference at the locked revision, sha256 from the Hugging Face LFS metadata of that revision.
FP32_ONNX: Final = "onnx/model.onnx"
# The exports V7 measured, each graph with its files and their sha256 (the revision's LFS object ids).
EXPORTS: Final = {
    "onnx/model_int8.onnx": {
        "onnx/model_int8.onnx": "4b164a8bd09dd9806e035bdf3c34a2d81848b3db9642ba2e342b8367c00872d8"
    },
    "onnx/model_fp16.onnx": {
        "onnx/model_fp16.onnx": "0b6846bcf4c0d9e53fdfc70d6dc1bbe3bb40fd205ebb7f0365da5b07064ddccc"
    },
    "onnx/model.onnx": {
        "onnx/model.onnx": "f74aa79745ccfb1e75daa7e8e6552a78402d4de193eb8ca67a931358d3e0a25e",
        "onnx/model.onnx_data": "fe7d75ff258fbda10a6bea63c5422df5579d625355b1aca69ba6923c0ba604a9",
    },
}
FP32_FILES: Final = {
    "onnx/model.onnx": "f74aa79745ccfb1e75daa7e8e6552a78402d4de193eb8ca67a931358d3e0a25e",
    "onnx/model.onnx_data": "fe7d75ff258fbda10a6bea63c5422df5579d625355b1aca69ba6923c0ba604a9",
    "tokenizer.json": "39feb9863a378165ab9c5c689047203d789422966c0c58721c5309fd039a8edc",
}


def _arbeitnow(raw: Any) -> str:
    return arbeitnow_description(raw)[0]


def _plain(raw: object) -> str:
    return plain_description(raw)[0]


# (fixture file, path to the item list, description key, the adapter's description text): 30 + 10 + 10 ads,
# each description as the pipeline stores it in `description_text` (Arbeitnow's HTML sanitised, flattened).
CORPUS: Final[tuple[tuple[str, tuple[str, ...], str, Callable[[object], str]], ...]] = (
    ("arbeitnow/page1.json", ("data",), "description", _arbeitnow),
    ("jsearch/search_v2_page1.json", ("data", "jobs"), "job_description", _plain),
    ("serpapi/google_jobs_hl_en.json", ("jobs_results",), "description", _plain),
)
N_ADS: Final = 50
QUERY: Final = "python developer"
# Two short ads written for this spike (not fixture text): the fixtures hold no nursing ad.
PYTHON_AD: Final = "Senior Python developer (Django, FastAPI, PostgreSQL) for the backend team of a startup."
NURSING_AD: Final = "Registered nurse (m/f/d) for the intensive care unit of a university hospital."

ACCEPT_DOCS_PER_S: Final = 1.5
ACCEPT_RSS_BYTES: Final = 3 * 10**9
ACCEPT_PARITY: Final = 0.99


def load_ads() -> list[tuple[str, str]]:
    """(fixture reference, description text) of the 50 ads."""
    ads: list[tuple[str, str]] = []
    for name, path, key, text_of in CORPUS:
        items: Any = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        for step in path:
            items = items[step]
        for i, item in enumerate(items):
            text = text_of(item.get(key))
            if text:
                ads.append((f"{name}#{i}", text))
    if len(ads) != N_ADS:
        raise SystemExit(f"expected {N_ADS} fixture ads with a description, found {len(ads)}")
    return ads


def peak_rss_bytes() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # KiB on Linux


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))


def sha256_of(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def token_lengths(model_dir: Path, texts: Sequence[str]) -> list[int]:
    """Untruncated token counts, special tokens included."""
    from tokenizers import Tokenizer

    tokenizer: Any = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return [len(e.ids) for e in tokenizer.encode_batch(list(texts))]


def lengthened(model_dir: Path, texts: Sequence[str]) -> list[str]:
    """Each text repeated until it is over 1,024 tokens, so that it is truncated to exactly 1,024."""
    lengths = token_lengths(model_dir, texts)  # each with its 2 special tokens
    return ["\n".join([t] * (MAX_TOKENS // max(n - 2, 1) + 1)) for t, n in zip(texts, lengths, strict=True)]


def run(model: OnnxModel, texts: Sequence[str]) -> tuple[float, list[list[float]], list[bool], list[int]]:
    """Embed `texts` as passages in requests of 16; the seconds taken and the results."""
    vectors: list[list[float]] = []
    truncated: list[bool] = []
    tokens: list[int] = []
    started = time.perf_counter()
    for i in range(0, len(texts), MAX_BATCH):
        v, t, n = model.embed(texts[i : i + MAX_BATCH], "passage")
        vectors += v
        truncated += t
        tokens += n
    return time.perf_counter() - started, vectors, truncated, tokens


def check_fp32(fp32_dir: Path) -> None:
    for name, expected in FP32_FILES.items():
        actual = sha256_of(fp32_dir / name)
        if actual != expected:
            raise SystemExit(f"fp32 reference {name}: sha256 {actual}, expected {expected}")


def verdict(ok: bool) -> str:
    return "PASS" if ok else "MISS"


def environment(threads: int) -> None:
    import onnxruntime as ort

    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if "model name" in line
        ),
        platform.processor(),
    )
    flags = Path("/proc/cpuinfo").read_text()
    vnni = "avx512_vnni" in flags or "avx_vnni" in flags
    load = ", ".join(f"{x:.2f}" for x in os.getloadavg())
    print(f"cpu: {cpu}; {os.cpu_count()} CPUs; VNNI: {vnni}; load average at start: {load}")
    print(f"onnxruntime {ort.__version__}; intra-op threads: {threads}")


def check_export(model_dir: Path, onnx_file: str) -> None:
    files = EXPORTS.get(onnx_file, {})
    for name, digest in files.items():
        path = model_dir / name
        if not path.is_file() or sha256_of(path) != digest:
            raise SystemExit(f"{name}: missing, or not the sha256 of the known {onnx_file} export")
    if not files:
        raise SystemExit(f"{onnx_file} is not one of the known exports")


def main_run(model_dir: Path, onnx_file: str, fp32_dir: Path | None, threads: int) -> None:
    ads = load_ads()
    texts = [t for _, t in ads]
    lengths = token_lengths(model_dir, texts)
    by_source = {name: sum(ref.startswith(name) for ref, _ in ads) for name, *_ in CORPUS}
    print(f"ads: {len(ads)} {by_source}")
    median = statistics.median(lengths)
    over = sum(n > MAX_TOKENS for n in lengths)
    print(
        f"untruncated tokens per ad: min {min(lengths)}, median {median:.0f}, max {max(lengths)};"
        f" over {MAX_TOKENS}: {over}"
    )

    started = time.perf_counter()
    model = OnnxModel(model_dir, onnx_file=onnx_file, threads=threads)
    loaded = time.perf_counter() - started
    rss_loaded = peak_rss_bytes() / 1e9
    print(f"{onnx_file} load: {loaded:.1f} s; output: {model.output_name}; peak RSS {rss_loaded:.2f} GB")
    model.embed([texts[0]], "passage")  # warm-up, not timed

    seconds, vectors, truncated, tokens = run(model, texts)
    rate_ads = len(texts) / seconds
    mean_tokens = statistics.mean(tokens)
    print(
        f"A. the 50 ads as stored: {rate_ads:.2f} docs/s ({seconds:.1f} s);"
        f" mean {mean_tokens:.0f} tokens per ad;"
        f" {sum(truncated)} truncated"
    )
    long_texts = lengthened(model_dir, texts)
    seconds_full, _, truncated_full, tokens_full = run(model, long_texts)
    if not all(truncated_full) or set(tokens_full) != {MAX_TOKENS}:
        raise SystemExit(f"the lengthened ads are not all exactly {MAX_TOKENS} tokens")
    rate_full = len(long_texts) / seconds_full
    print(f"B. the 50 ads at exactly {MAX_TOKENS} tokens: {rate_full:.2f} docs/s ({seconds_full:.1f} s)")
    rss = peak_rss_bytes()
    print(f"peak RSS after A and B: {rss / 1e9:.2f} GB ({rss / 2**20:.0f} MiB)")

    # the query prefix: `query: python developer` against two short ads, and the same text as a passage
    (q,), _, (q_tokens,) = model.embed([QUERY], "query")
    (p_python, p_nursing, p_same), _, (_, _, p_tokens) = model.embed(
        [PYTHON_AD, NURSING_AD, QUERY], "passage"
    )
    c_python, c_nursing, c_same = cosine(q, p_python), cosine(q, p_nursing), cosine(q, p_same)
    prefix_ok = c_python > c_nursing and c_same < ACCEPT_PARITY
    print(
        f"prefix: cos(query, Python ad) {c_python:.4f} > cos(query, nursing ad) {c_nursing:.4f};"
        f" cos(query, same text as passage) {c_same:.4f}; tokens query {q_tokens} vs passage {p_tokens}"
    )
    python_ads = [i for i, t in enumerate(texts) if "python" in t.lower()]
    sims = [cosine(q, v) for v in vectors]
    ranked = sorted(range(len(sims)), key=lambda i: -sims[i])
    others = [s for i, s in enumerate(sims) if i not in python_ads]
    python_mean = statistics.mean(sims[i] for i in python_ads)
    print(
        f"prefix on the fixtures: {len(python_ads)} ads mention Python, mean cos {python_mean:.4f} against"
        f" {statistics.mean(others):.4f} for the other {len(others)}; Python ads among the query's top 10:"
        f" {sum(i in python_ads for i in ranked[:10])}"
    )
    # a text's vector does not depend on its neighbours: alone and next to a 1,024-token text
    short = min(range(len(texts)), key=lambda i: lengths[i])
    (alone,), _, _ = model.embed([texts[short]], "passage")
    (_, beside), _, _ = model.embed([long_texts[0], texts[short]], "passage")
    print(f"the shortest ad alone vs next to a {MAX_TOKENS}-token ad: cos {cosine(alone, beside):.6f}")

    parity_ok: bool | None = None
    if fp32_dir is not None:
        check_fp32(fp32_dir)
        started = time.perf_counter()
        reference = OnnxModel(fp32_dir, onnx_file=FP32_ONNX, threads=threads)
        print(f"fp32 load: {time.perf_counter() - started:.1f} s; output: {reference.output_name}")
        seconds_fp32, ref_vectors, _, ref_tokens = run(reference, texts)
        if ref_tokens != tokens:
            raise SystemExit("the fp32 reference tokenised the ads differently")
        parity = [cosine(a, b) for a, b in zip(vectors, ref_vectors, strict=True)]
        parity_ok = min(parity) >= ACCEPT_PARITY
        print(
            f"parity {onnx_file} vs fp32 over the 50 ads: min {min(parity):.4f},"
            f" median {statistics.median(parity):.4f},"
            f" mean {statistics.mean(parity):.4f}; fp32 {len(texts) / seconds_fp32:.2f} docs/s"
        )

    print("acceptance (this container):")
    full_ok, ads_ok = rate_full >= ACCEPT_DOCS_PER_S, rate_ads >= ACCEPT_DOCS_PER_S
    print(f"  docs/s at {MAX_TOKENS} tokens >= {ACCEPT_DOCS_PER_S}: {rate_full:.2f} {verdict(full_ok)}")
    print(f"  docs/s of the ads as stored >= {ACCEPT_DOCS_PER_S}: {rate_ads:.2f} {verdict(ads_ok)}")
    print(f"  peak RSS <= 3 GB: {rss / 1e9:.2f} GB {verdict(rss <= ACCEPT_RSS_BYTES)}")
    print(
        f"  parity >= {ACCEPT_PARITY} on every ad: {'not run' if parity_ok is None else verdict(parity_ok)}"
    )
    print(f"  query prefix: {verdict(prefix_ok)}")


def padded_run(model_dir: Path, onnx_file: str, fp32_dir: Path | None, threads: int, batch: int) -> None:
    """The road not taken: the 1,024-token ads as padded batches of `batch` through onnxruntime directly."""
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    texts = [t for _, t in load_ads()]
    long_texts = lengthened(model_dir, texts)
    tokenizer: Any = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)  # and the file's own padding: to the batch's longest

    def session(path: Path) -> Any:
        options: Any = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])

    def embed(s: Any, batch_texts: Sequence[str]) -> Any:
        encodings = tokenizer.encode_batch(list(batch_texts))
        feed = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        return s.run(["sentence_embedding"], feed)[0]

    export = session(model_dir / onnx_file)
    embed(export, long_texts[:1])  # warm-up
    n = len(long_texts) - len(long_texts) % batch
    started = time.perf_counter()
    for i in range(0, n, batch):
        embed(export, long_texts[i : i + batch])
    seconds = time.perf_counter() - started
    print(
        f"padded batches of {batch} at {MAX_TOKENS} tokens: {n / seconds:.2f} docs/s"
        f" ({n} ads, {seconds:.1f} s)"
    )
    print(f"peak RSS: {peak_rss_bytes() / 1e9:.2f} GB")
    lengths = token_lengths(model_dir, texts)
    short = texts[min(range(len(texts)), key=lambda i: lengths[i])]
    sessions = [(onnx_file, export)]
    if fp32_dir is not None:
        check_fp32(fp32_dir)
        sessions.append(("fp32", session(fp32_dir / FP32_ONNX)))
    for name, s in sessions:
        alone = embed(s, [short])[0]
        beside = embed(s, [long_texts[0], short])[1]
        print(
            f"{name}: the shortest ad alone vs padded next to a {MAX_TOKENS}-token ad:"
            f" cos {cosine(alone, beside):.6f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("model_dir", type=Path, help="the locked model files (embedder/download_model.sh)")
    parser.add_argument("--onnx", help="the export to measure (default: the one model.lock pins)")
    parser.add_argument("--fp32", type=Path, help="the fp32 reference export, for the parity")
    parser.add_argument("--threads", type=int, default=int(os.environ.get("EMBEDDER_THREADS", "3")))
    parser.add_argument("--padded-batch", type=int, metavar="N", help="measure padded batches of N instead")
    args = parser.parse_args()
    print(f"model.lock: {verify_checksums(args.model_dir, LOCK)} verified")
    onnx_file = args.onnx or locked_onnx_file(LOCK)
    check_export(args.model_dir, onnx_file)
    print(f"measured export: {onnx_file} (sha256 verified)")
    environment(args.threads)
    if args.padded_batch:
        padded_run(args.model_dir, onnx_file, args.fp32, args.threads, args.padded_batch)
    else:
        main_run(args.model_dir, onnx_file, args.fp32, args.threads)


if __name__ == "__main__":
    main()
