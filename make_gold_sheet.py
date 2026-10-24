"""Randomly sample passages to write the Milestone 2 gold set from.

    python make_gold_sheet.py

Writes two files:
- data/corpus/gold_passages.md   the sampled passages to read (git-ignored: it quotes Bungie's text)
- data/gold_set.csv              the gold set to fill in, one row per passage, plus blank rows for
                                 multi-hop, unanswerable, and distractor-trap questions

Write each question BEFORE running anything that retrieves (rag.py ask, eval_gold.py), in the words a
player would type, not the passage's words. The sample is seeded, so rerunning gives the same passages.
"""
import csv
import random
from pathlib import Path

import rag

SEED = 790
N_PASSAGES = 32          # A/C/D/E-style questions, one per sampled passage
N_MULTIHOP = 4           # B: you pick two documents yourself
N_UNANSWERABLE = 5       # F: questions the corpus can't answer
N_TRAPS = 3              # questions where a distractor (gameplay) page looks relevant
MIN_CHARS = 300          # skip tiny chunks (the end of an entry)

OUT_MD = rag.ROOT / "data" / "corpus" / "gold_passages.md"
OUT_CSV = rag.ROOT / "data" / "gold_set.csv"
FIELDS = ["question_id", "question", "reference_answer", "doc_id", "location", "question_type",
          "written_by", "notes"]


def main():
    if OUT_CSV.exists():
        raise SystemExit(f"{OUT_CSV} already exists. Delete it first if you really want a new sample.")
    docs = rag.load_docs()
    chunks = [c for c in rag.chunk_corpus(docs) if len(c["text"]) >= MIN_CHARS]
    lore = [c for c in chunks if c["role"] == "lore"]
    distractors = [c for c in chunks if c["role"] == "distractor"]

    rng = random.Random(SEED)
    # One passage per document, so no document gets two questions
    rng.shuffle(lore)
    sample, seen = [], set()
    for c in lore:
        if c["doc_id"] not in seen:
            sample.append(c)
            seen.add(c["doc_id"])
        if len(sample) == N_PASSAGES:
            break
    trap_pages = rng.sample(distractors, min(N_TRAPS, len(distractors)))

    lines = ["# Gold-set passages", "",
             f"Randomly sampled with seed {SEED} from {len(lore):,} lore chunks. For each passage, write one "
             "question a player would ask that this passage answers, in a player's words, plus a short "
             "reference answer. Fill them into `data/gold_set.csv`.", "",
             "Question types: **A** lookup · **B** multi-hop (two documents) · **C** numbers/lists · "
             "**D** versions (D1 vs D2, or a distractor that looks right) · **E** exact names/IDs · "
             "**F** unanswerable", ""]
    for i, c in enumerate(sample, 1):
        lines += [f"## G{i:02d} · {c['title']}",
                  f"`{c['doc_id']}` · {c['game']} · {c['source']} · book: {c['book'] or '-'} · passage `{c['chunk_id']}`",
                  "", "> " + c["text"].replace("\n", "\n> "), ""]
    lines += ["---", "", "# Distractor pages (for trap questions)", "",
              "Gameplay pages that share vocabulary with the lore. Write a lore question that one of these "
              "might wrongly answer, with the real answer in a lore document.", ""]
    for i, c in enumerate(trap_pages, 1):
        lines += [f"## T{i:02d} · {c['title']}", f"`{c['doc_id']}` · passage `{c['chunk_id']}`", "",
                  "> " + c["text"][:600].replace("\n", "\n> "), ""]
    OUT_MD.write_text("\n".join(lines) + "\n")

    rows = [{"question_id": f"G{i:02d}", "doc_id": c["doc_id"], "location": c["chunk_id"],
             "written_by": "Henry Halvorsen"} for i, c in enumerate(sample, 1)]
    rows += [{"question_id": f"M{i:02d}", "question_type": "B", "written_by": "Henry Halvorsen",
              "notes": "doc_id: two ids separated by ;"} for i in range(1, N_MULTIHOP + 1)]
    rows += [{"question_id": f"T{i:02d}", "question_type": "D", "written_by": "Henry Halvorsen",
              "notes": f"trap: {c['doc_id']} looks relevant; doc_id = the lore doc that really answers"}
             for i, c in enumerate(trap_pages, 1)]
    rows += [{"question_id": f"U{i:02d}", "question_type": "F", "doc_id": "", "location": "",
              "reference_answer": rag.DECLINE, "written_by": "Henry Halvorsen"}
             for i in range(1, N_UNANSWERABLE + 1)]
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in FIELDS})

    print(f"Sampled {len(sample)} passages from {len(lore):,} lore chunks (seed {SEED})")
    print(f"  read:    {OUT_MD.relative_to(rag.ROOT)}")
    print(f"  fill in: {OUT_CSV.relative_to(rag.ROOT)} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
