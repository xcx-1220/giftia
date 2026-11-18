"""Run the fixed three-document smoke cases against the isolated real gateway."""

import argparse
import json
from pathlib import Path
import re
from time import monotonic

from stage1 import Lab
from stage5_probe import BENCH_VISITOR
from verify import require
from verify_stage4 import GUEST, WebCheck
from web_gateway import CITATION, NO_EVIDENCE

CASES = (
    ("live-pair", "请分别说明实验室和归还处的标签，每个标签后紧跟它所在页的页码引用。",
     {1: "ORBIT-41", 2: "LANTERN-72"}),
    ("live-followup", "你上一条说的第二个地点，归还截止时间是几点？只回答时间并注明来源。",
     {2: "16:30"}),
    ("live-absent", "这份资料里有联系电话吗？", {}),
)


def run(check, pdf):
    require(not check.state(BENCH_VISITOR)["items"], "Use the fresh document smoke state")
    attachment = check.upload(pdf.read_bytes(), identity=BENCH_VISITOR)
    check.wait(attachment, "ready", BENCH_VISITOR)
    pages = {}
    for page in (1, 2):
        route = f"/attachments/{attachment}/pages/{page}"
        pages[page] = check.api(route, identity=BENCH_VISITOR)["text"]
        check.api(route, identity=GUEST, code=404)
    for job_id, question, facts in CASES:
        begin = monotonic()
        check.send(job_id, question, attachment, identity=BENCH_VISITOR)
        try:
            job = check.wait(job_id, "ready", BENCH_VISITOR, timeout=80)
            web_ms = round((monotonic() - begin) * 1000, 1)
            timing = check.document_timing(job_id)
            answer = job["reply"]
            cited = [item["page"] for item in job["citations"]]
            preceding = re.split(CITATION, answer)
            mapping = {int(preceding[index + 1]): preceding[index] for index in range(0, len(preceding) - 1, 2)}
            supported = (set(cited) == set(facts)
                         and all(fact in mapping.get(page, "") and fact in pages[page] for page, fact in facts.items()))
            if not facts:
                supported = answer.strip() == NO_EVIDENCE
            result = {"case": job_id, "reply": answer, "pages": cited, "expectedFactCitation": supported,
                      **timing, "webReadyMs": web_ms}
        except (RuntimeError, TimeoutError, ValueError, KeyError) as error:
            result = {"case": job_id, "failure": str(error), "elapsedMs": round((monotonic() - begin) * 1000, 1)}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    args = parser.parse_args()
    run(WebCheck(Lab(args.root, 18131, 18132)), args.pdf)
