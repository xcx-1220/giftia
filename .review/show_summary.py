import json
import pathlib

rows = json.loads(pathlib.Path(".review/evidence/summary.json").read_text(encoding="utf-8"))
print(f"{'套件':24}{'退出码':>7}{'耗时':>9}{'PASS':>7}{'FAIL':>7}")
for r in rows:
    c = r["counts"]
    print(f"{r['label']:24}{r['exit_code']:>7}{r['elapsed_s']:>8}s{c.get('pass_marks', 0):>7}{c.get('fail_marks', 0):>7}")
