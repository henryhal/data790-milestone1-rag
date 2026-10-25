# Milestone 2: Security-Hardened Destiny Lore Guide

DATA 790 - Henry Halvorsen

A RAG system that answers players' questions about the story of Destiny and Destiny 2, citing the lore entries it used. It answers from 3,411 lore documents (Bungie's official Destiny 2 lore and Destiny 1 Grimoire, plus Destiny Wiki pages), and it protects its system prompt, its persona, the player data in its corpus, and its API keys.

Milestone 1 (the AI-security RAG system) is preserved under the `milestone-1` tag. This milestone keeps the same pipeline, with a new, larger corpus, an attack suite, and layered defenses.

## Results

| Gold set (44 questions) | Undefended | With defenses |
|---|---|---|
| Right document in top 5 (hit@5) | 82% | 82% |
| Accuracy (judge score 4-5, 39 answerable) | 79% | 77% |
| Unanswerable questions declined | 5/5 | 5/5 |
| Wrongly blocked by my defenses | - | 0 |
| Wrongly blocked by the UNC gateway's content filter | 4 | 3 |
| Latency p50 / p95 | 1.5 s / 3.0 s | 2.7 s / 4.5 s |
| Cost per query | $0.00054 | $0.00064 |

| Attack suite (20 attacks) | Undefended | With defenses |
|---|---|---|
| Attacks that succeeded | 8/20 | 2/20 |
| Direct / role override / context manipulation / indirect | 1/5, 1/4, 3/4, 3/7 | 0/5, 0/4, 0/4, 2/7 |

Every number comes from a file in `results/`. Costs use public OpenAI list prices (the UNC gateway is prepaid).

## How to run

1. Clone and install (Python 3.10):
   ```
   git clone https://github.com/henryhal/data790-milestone1-rag.git
   cd data790-milestone1-rag
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and add your UNC AI Gateway key and a Bungie API key (free at bungie.net/en/Application). The `.env` can also sit in a parent folder.
3. Build the corpus (the lore texts are copyrighted, so they aren't in the repo; this re-downloads them): open `build_corpus.ipynb` and Run All. It needs `data/corpus/raw/destiny1_grimoire.csv`.
4. Build the search index:
   ```
   python rag.py index
   ```
5. Gold-set eval (undefended, then with defenses):
   ```
   python eval_gold.py
   python eval_gold.py --guard layered
   ```
6. Attack suite (undefended, then with defenses):
   ```
   python run_attacks.py
   python run_attacks.py --guard layered
   ```
7. Ask one question: `python rag.py ask "who are oryx's sisters"` (add `--guard layered` for the defended version).

LLM responses are cached in `results/m2_llm_cache.json`, so reruns give the same numbers without calling the API.

## Files

- `build_corpus.ipynb` - pulls and cleans the three sources, writes `corpus/manifest.jsonl` (source and license for every document) and `corpus/corpus_report.md`
- `rag.py` - the pipeline: chunking (sentence_800, from Milestone 1), embeddings, Chroma, the Archivist prompt with a canary string
- `defenses.py` - the layers from the security demo (pattern check, LLM classifier, chunk sanitizing, output filter) plus delimiters, a canary filter, and Lab 13's PII redaction
- `data/gold_set.csv`, `make_gold_sheet.py`, `eval_gold.py` - the gold set (randomly sampled passages, seed 790) and its eval
- `data/attacks.json`, `data/planted_docs.json`, `run_attacks.py` - 20 attacks, 7 planted documents (one with fake player PII), and the runner
- `results/` - every eval and attack result (`gold_*`, `attacks_*`)
- `Milestone1_Production_RAG.ipynb` - the Milestone 1 notebook, kept for reference
