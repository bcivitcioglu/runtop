"""Estimate a --keys tape's running time: python3 scripts/tapes/duration.py runtop-demo.keys"""
import pathlib
import sys

tokens = pathlib.Path(sys.argv[1]).read_text().split()
total = 0.8
for i, tok in enumerate(tokens):
    if tok.startswith("wait:"):
        total += float(tok[5:])
        continue
    nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
    typing = (len(tok) == 1 or tok == "space") and (len(nxt) == 1 or nxt == "space")
    total += 0.12 if typing else 0.45
print(f"{total:.1f}")
