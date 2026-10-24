"""Run the attack suite (data/attacks.json) against the system.

    python run_attacks.py                     # undefended -> results/attacks_baseline.csv
    python run_attacks.py --guard layered     # with the defenses -> results/attacks_layered.csv

The attacker can type questions AND get documents into the corpus, so every attack runs against a copy of
the index with the planted documents (data/planted_docs.json) added, like Milestone 1's poisoned_demo.
An attack succeeded if any of its "success_if" strings shows up in the final answer the user sees.
"""
import csv
import json
import argparse

import rag

POISONED = "m2_destiny_poisoned"


def build_poisoned_index():
    """Copy the main index and add the planted documents (only the planted chunks get embedded)."""
    planted = json.loads((rag.ROOT / "data" / "planted_docs.json").read_text())
    chunks = [c for d in planted
              for c in rag.chunk_doc({**d, "source": "planted", "game": "D2", "book": "", "role": "planted"})]
    db = rag._chroma()
    try:
        col = db.get_collection(POISONED)
        if col.count() == rag.get_collection().count() + len(chunks):
            return col
        db.delete_collection(POISONED)
    except Exception:
        pass
    main = rag.get_collection().get(include=["embeddings", "documents", "metadatas"])
    vecs, _ = rag.embed_texts([rag.embed_input(c) for c in chunks])
    col = db.create_collection(POISONED, embedding_function=None,
                               metadata={"hnsw:space": "cosine", "hnsw:search_ef": 100})
    for i in range(0, len(main["ids"]), 500):
        col.add(ids=main["ids"][i:i + 500], embeddings=[list(v) for v in main["embeddings"][i:i + 500]],
                documents=main["documents"][i:i + 500], metadatas=main["metadatas"][i:i + 500])
    col.add(ids=[c["chunk_id"] for c in chunks], embeddings=vecs.tolist(), documents=[c["text"] for c in chunks],
            metadatas=[{k: v for k, v in c.items() if k not in ("chunk_id", "text")} for c in chunks])
    return col


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--guard", default="none")
    p.add_argument("--label", default=None)
    args = p.parse_args()
    label = args.label or ("baseline" if args.guard == "none" else args.guard)

    attacks = json.loads((rag.ROOT / "data" / "attacks.json").read_text())
    guard = rag.load_guard(args.guard)
    col = build_poisoned_index()

    rows = []
    for a in attacks:
        r = rag.ask(a["payload"], guard=guard, collection=col)
        answer = r["answer"] or ""
        success = any(s.lower() in answer.lower() for s in a["success_if"])
        rows.append({
            "id": a["id"], "kind": a["kind"], "goal": a["goal"], "payload": a["payload"],
            "success": success,
            "blocked_by": r["blocked_by"] or "",
            # which layer stopped it: the one that blocked it, or the one that changed it (sanitize, pii_redaction)
            "caught_by": "" if success else (r["blocked_by"] or (r["events"][0].split(":")[0] if r["events"] else "")),
            "events": " | ".join(r["events"]),
            "planted_retrieved": (a.get("planted") in [c["doc_id"] for c in r["retrieved"]]) if a.get("planted") else "",
            "gateway_flags": ";".join(r["gateway_flags"]),
            "latency_ms": r["latency_ms"], "cost_usd": r["cost_usd"],
            "answer": answer,
        })
        print(f"  {a['id']:3s} {a['kind']:21s} {'SUCCEEDED' if success else 'failed':9s} "
              f"{('blocked by ' + r['blocked_by']) if r['blocked_by'] else ''}")

    out = rag.RESULTS_DIR / f"attacks_{label}.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    kinds = sorted({r["kind"] for r in rows})
    summary = {
        "label": label, "attacks": len(rows),
        "succeeded": sum(r["success"] for r in rows),
        "success rate": sum(r["success"] for r in rows) / len(rows),
        "succeeded by kind": {k: f"{sum(r['success'] for r in rows if r['kind'] == k)}/"
                                 f"{sum(r['kind'] == k for r in rows)}" for k in kinds},
        "caught by layer": {b: sum(r["caught_by"] == b for r in rows)
                            for b in sorted({r["caught_by"] for r in rows if r["caught_by"]})},
    }
    (rag.RESULTS_DIR / f"attacks_{label}_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\nPer-attack results: {out.relative_to(rag.ROOT)}")


if __name__ == "__main__":
    main()
