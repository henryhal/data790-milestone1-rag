"""Run the gold set through the system and score it.

    python eval_gold.py                       # undefended baseline -> results/gold_baseline.*
    python eval_gold.py --guard layered       # with the defenses   -> results/gold_layered.*

Scores, per question and overall:
- retrieval: hit@1 / hit@3 / hit@5 = a chunk from the labeled document is in the top k
- answers: an LLM judge scores correctness 1-5 against the reference answer (accuracy = score 4-5)
- unanswerable questions: did it decline?
- defense cost: questions wrongly blocked by a guard, latency (p50/p95) and cost per query
"""
import re
import csv
import json
import argparse

import numpy as np

import rag

GOLD_PATH = rag.ROOT / "data" / "gold_set.csv"

JUDGE_PROMPT = """You are grading an answer against a reference answer.
Score 1-5: 5 = has all key facts of the reference and nothing wrong, 3 = partly correct or missing important facts, 1 = wrong or doesn't answer. Extra correct detail is fine.
Reply with only the number.

Question: {QUESTION}
Reference answer: {REFERENCE}
Answer to grade: {ANSWER}"""


def load_gold(path=GOLD_PATH):
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("question") or "").strip()]
    for r in rows:
        r["gold_docs"] = [d.strip() for d in (r.get("doc_id") or "").split(";") if d.strip()]
        r["answerable"] = bool(r["gold_docs"])
    return rows


def judge(question, reference, answer):
    prompt = (JUDGE_PROMPT.replace("{QUESTION}", question).replace("{REFERENCE}", reference)
              .replace("{ANSWER}", answer))
    out = rag.chat([{"role": "user", "content": prompt}], max_tokens=5)
    m = re.search(r"[1-5]", out["text"])
    return (int(m.group()) if m else None), out["cost_usd"]


def evaluate(guard_name="none", label=None, limit=None):
    label = label or ("baseline" if guard_name == "none" else guard_name)
    guard = rag.load_guard(guard_name)
    gold = load_gold()[:limit]
    rows, judge_cost = [], 0.0
    for i, q in enumerate(gold, 1):
        print(f"  [{label}] {i}/{len(gold)} {q['question_id']}", end="\r")
        r = rag.ask(q["question"], guard=guard)
        retrieved_docs = [c["doc_id"] for c in r["retrieved"]]
        row = {"question_id": q["question_id"], "question_type": q.get("question_type", ""),
               "answerable": q["answerable"], "question": q["question"],
               "blocked": r["blocked_by"] is not None, "blocked_by": r["blocked_by"],
               "declined": r["declined"], "latency_ms": r["latency_ms"], "cost_usd": r["cost_usd"],
               "gateway_flags": ";".join(r["gateway_flags"]), "answer": r["answer"],
               "retrieved": ";".join(retrieved_docs), "events": " | ".join(r["events"])}
        if q["answerable"]:
            for k in (1, 3, 5):
                row[f"hit@{k}"] = float(any(d in q["gold_docs"] for d in retrieved_docs[:k]))
            if r["blocked_by"] or r["declined"]:
                row["correctness"] = 1            # blocking or declining an answerable question counts as wrong
            else:
                row["correctness"], c = judge(q["question"], q["reference_answer"], r["answer"])
                judge_cost += c
            row["accurate"] = float((row["correctness"] or 0) >= 4)
        else:
            row["correct_decline"] = float(r["declined"] or r["blocked_by"] is not None)
        rows.append(row)
    print()

    ans = [r for r in rows if r["answerable"]]
    unans = [r for r in rows if not r["answerable"]]
    lat = [r["latency_ms"] for r in rows]
    mean = lambda xs: float(np.mean(xs)) if xs else None
    summary = {
        "label": label, "guard": guard_name, "questions": len(rows),
        "answerable": len(ans), "unanswerable": len(unans),
        "hit@1": mean([r["hit@1"] for r in ans]), "hit@3": mean([r["hit@3"] for r in ans]),
        "hit@5": mean([r["hit@5"] for r in ans]),
        "accuracy (score 4-5)": mean([r["accurate"] for r in ans]),
        "mean correctness (1-5)": mean([r["correctness"] or 0 for r in ans]),
        "answerable declined": sum(r["declined"] for r in ans),
        "unanswerable correctly declined": f"{sum(r['correct_decline'] for r in unans):.0f}/{len(unans)}",
        "wrongly blocked (false positives)": sum(r["blocked"] for r in rows),
        "wrongly blocked rate": mean([float(r["blocked"]) for r in ans]),
        "latency p50 (ms)": float(np.percentile(lat, 50)), "latency p95 (ms)": float(np.percentile(lat, 95)),
        "cost per query ($)": mean([r["cost_usd"] for r in rows]),
        "judge cost total ($)": judge_cost,
        "by question type (hit@5)": {t: mean([r["hit@5"] for r in ans if r["question_type"] == t])
                                     for t in sorted({r["question_type"] for r in ans})},
    }

    out_csv = rag.RESULTS_DIR / f"gold_{label}.csv"
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    (rag.RESULTS_DIR / f"gold_{label}_summary.json").write_text(json.dumps(summary, indent=2))
    return summary, out_csv


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--guard", default="none", help="none (default) or a guard name from defenses.py")
    p.add_argument("--label", default=None, help="name for the results files")
    p.add_argument("--limit", type=int, default=None, help="only the first N questions (for a quick test)")
    args = p.parse_args()
    summary, out_csv = evaluate(args.guard, args.label, args.limit)
    print(json.dumps(summary, indent=2))
    print(f"\nPer-question results: {out_csv.relative_to(rag.ROOT)}")


if __name__ == "__main__":
    main()
