"""Re-evaluate saved bytes after a documented oracle bug; never rerun the Agent."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from run import ROOT, check
from measurements import trial_measurements


def rescore(folder):
    suite=Path(folder)/'suite.json'
    if not suite.exists(): suite=ROOT/'evaluation/suite.json'
    cases={c["id"]:c for c in json.loads(suite.read_text(encoding="utf-8"))["cases"]}
    for path in Path(folder).glob("*/summary.json"):
        summary=json.loads(path.read_text(encoding="utf-8"))
        if "record_path" not in summary: continue
        case=cases[summary["case"]]
        errors=[]
        reports=[]
        for key,oracle in [("record_path",case["oracle"]),("followup_record_path",case.get("followup_oracle"))]:
            if not oracle or key not in summary: continue
            report=json.loads(Path(summary[key]).read_text(encoding="utf-8"))
            reports.append(report)
            assets={a["id"]:{"metadata":a,"files":{n:Path(p).read_bytes() for n,p in a["paths"].items()}} for a in report.get("artifacts",[])}
            errors.extend(check(report,assets,oracle))
        summary.setdefault("original_oracle_result",{k:summary[k] for k in ["attained","errors"]})
        summary.update(attained=not errors,errors=errors,oracle_revision="2: read csv_table dictionary rows by declared column order")
        summary.update(trial_measurements(reports))
        path.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    results=[json.loads(p.read_text(encoding="utf-8")) for p in Path(folder).glob("*/summary.json")]
    print(json.dumps({"runs":len(results),"attained":sum(r["attained"] for r in results)},ensure_ascii=False))


if __name__=="__main__": rescore(sys.argv[1])
