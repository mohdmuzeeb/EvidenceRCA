"""Audit workflow tool (audit/audit_tool.py) on SYNTHETIC fixtures only.

No test reads the real sealed key or creates labels for the real 240 items. The fixtures below are a tiny
fake package in tmp_path; their "labels" exist only to exercise the code paths and are never written to the repo.
"""
import csv
import json
import subprocess

import pytest

from audit import audit_tool as at

COLS = ["audit_id", "context_id", "windows", "claim", "cited_records", "context_baseline_same_family",
        "context_analysis_same_family", "observed_call_edges", "support_label", "partial_reason",
        "evidence_relevance", "notes"]


def _write_csv(path, fields, rows, newline="\n"):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator=newline)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    """A fake 12-item package: 2 conditions' worth of key rows per condition, 2 base cases each."""
    items, key = [], []
    for i in range(12):
        cond = at.CONDITIONS[i % 3]
        aid = f"P{i + 1:03d}"
        items.append({"audit_id": aid, "context_id": "c", "windows": "w", "claim": f"OBSERVED: x{i}",
                      "cited_records": "t=+1s | a | metric m | 1", "context_baseline_same_family": "",
                      "context_analysis_same_family": "", "observed_call_edges": ""})
        key.append({"audit_id": aid, "condition": cond, "scenario": f"s{i % 4}", "predicate_group": "onset",
                    "verifier_accepted": i % 2 == 0, "subject_is_true_root": i % 4 == 0})
    blinded = tmp_path / "audit_blinded.csv"
    _write_csv(blinded, COLS, items, newline="\r\n")
    keyf = tmp_path / "key.json"
    keyf.write_text(json.dumps({"rows": key}))
    fill = {c: {"accepted_available": 100, "rejected_available": 300} for c in at.CONDITIONS}
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps({"n_items": 12, "csv_sha256": at.sha256(blinded), "key_sha256": at.sha256(keyf),
                               "stratum_fill": fill}))
    for name, val in {"BLINDED": blinded, "KEY": keyf, "MANIFEST": man, "AUDIT": tmp_path,
                      "WORKED": tmp_path / "worked_examples.csv", "PACKETS": tmp_path / "packets",
                      "SUBMISSIONS": tmp_path / "submissions", "ADJ": tmp_path / "adjudication",
                      "OUT": tmp_path / "results", "REPO": tmp_path}.items():
        monkeypatch.setattr(at, name, val)
    (tmp_path / "ANNOTATOR_GUIDE.md").write_text("guide")
    return {"tmp": tmp_path, "items": items, "key": key, "fill": fill}


def _labels(path, rows):
    _write_csv(path, ["audit_id", *at.LABEL_FIELDS], rows)
    return path


def _all(pkg, label="supported", rel="relevant"):
    return [{"audit_id": r["audit_id"], "support_label": label, "evidence_relevance": rel} for r in pkg["items"]]


def test_real_package_matches_manifest_and_is_unlabelled():
    man = at.check_package()      # real files; reads checksums only
    assert man["n_items"] == 240 and man["csv_sha256"].startswith("885a5c61")


def test_real_worked_examples_template_is_dev_only_and_unlabelled():
    rows = at.read_rows(at.WORKED)
    dev_ids = {r["audit_id"] for r in at.read_rows(at.DEV_SHEET)}
    assert len(rows) == 10 and {r["dev_audit_id"] for r in rows} <= dev_ids
    assert all(r["dev_audit_id"].startswith("A") for r in rows)          # dev ids are Axxx, Phase 4B ids are Pxxx
    assert len({r["predicate_group"] for r in rows}) == 5
    # labels are filled by the study lead; while pending, the packets step must refuse
    if any(r["support_label"] for r in rows):
        pytest.skip("worked examples have been labelled by the study lead")
    assert at.worked_examples_complete()


def test_packets_refuse_without_worked_examples(pkg):
    with pytest.raises(at.AuditError, match="worked examples"):
        at.make_packets()


def test_packets_never_contain_the_key(pkg):
    ex = [{"example_id": f"W{i:02d}", "claim": "c", "support_label": "unsupported", "evidence_relevance": "relevant",
           "explanation": "why", "labelled_by": "SL"} for i in range(10)]
    _write_csv(at.WORKED, at.WORKED_FIELDS, ex)
    at.make_packets()
    for a in ("A", "B"):
        d = pkg["tmp"] / "packets" / f"annotator_{a}"
        names = {p.name for p in d.iterdir()}
        assert names == {"items.html", f"labels_{a}.csv", "audit_blinded.csv", "ANNOTATOR_GUIDE.md",
                         "worked_examples.csv", "packet_manifest.json"}
        blob = "".join(p.read_text(errors="ignore") for p in d.iterdir())
        assert "verifier_accepted" not in blob and "subject_is_true_root" not in blob and "labelled_by" not in blob
        assert len(at.read_rows(d / f"labels_{a}.csv")) == 12
    at.make_packets(relabel="A")
    rel = at.read_rows(pkg["tmp"] / "packets" / "annotator_A-relabel" / "labels_A-relabel.csv")
    assert len(rel) == round(0.2 * 12)


def test_validate_reports_every_problem(pkg):
    rows = _all(pkg)
    rows[0]["support_label"] = "Supported "      # case/space typo -> stripped but still wrong case
    rows[1].update(support_label="partially_supported")                       # missing reason
    rows[2].update(partial_reason="value")                                    # reason without partial
    rows[3].update(evidence_relevance="")                                     # missing relevance
    rows[4].update(support_label="not_assessable", evidence_relevance="")     # allowed
    del rows[5]                                                               # missing item
    with pytest.raises(at.AuditError) as e:
        at.validate(_labels(pkg["tmp"] / "bad.csv", rows))
    msg = str(e.value)
    for frag in ("P001", "P002", "P003", "P004", "missing ['P006']"):
        assert frag in msg
    assert "P005" not in msg


def test_validate_accepts_semicolon_bom_and_full_sheet(pkg):
    p = pkg["tmp"] / "semi.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["audit_id", *at.LABEL_FIELDS])
        for r in pkg["items"]:
            w.writerow([r["audit_id"], "unsupported", "", "irrelevant", ""])
    assert len(at.validate(p)) == 12
    full = [{**r, "support_label": "supported", "evidence_relevance": "relevant"} for r in pkg["items"]]
    _write_csv(pkg["tmp"] / "full.csv", COLS, full)                  # LF instead of CRLF is fine
    assert len(at.validate(pkg["tmp"] / "full.csv")) == 12
    full[3]["cited_records"] = "edited"
    _write_csv(pkg["tmp"] / "full2.csv", COLS, full)
    with pytest.raises(at.AuditError, match="content differs"):
        at.validate(pkg["tmp"] / "full2.csv")


def test_register_is_write_once(pkg):
    f = _labels(pkg["tmp"] / "a.csv", _all(pkg))
    at.register(f, "A")
    with pytest.raises(at.AuditError, match="already registered"):
        at.register(f, "A")
    log = json.loads((pkg["tmp"] / "submissions" / "submission_log.json").read_text())
    assert log["submissions"][0]["annotator"] == "A" and log["submissions"][0]["n_items"] == 12


def test_kappa_and_ac1_known_values():
    a = ["supported"] * 20 + ["unsupported"] * 20
    assert at.cohen_kappa(a, a, at.SUPPORT) == pytest.approx(1.0)
    b = ["supported"] * 15 + ["unsupported"] * 5 + ["supported"] * 5 + ["unsupported"] * 15
    assert at.cohen_kappa(a, b, at.SUPPORT) == pytest.approx(0.5)       # po 0.75, pe 0.5
    assert at.gwet_ac1(a, b, at.SUPPORT) == pytest.approx((0.75 - 1 / 6) / (1 - 1 / 6))
    # weighted: a one-step ordinal disagreement costs half of a two-step one
    x = ["unsupported", "partially_supported", "supported"] * 10
    y1 = ["partially_supported", "partially_supported", "supported"] * 10
    y2 = ["supported", "partially_supported", "supported"] * 10
    assert at.cohen_kappa(x, y1, list(at.ORDINAL), True) > at.cohen_kappa(x, y2, list(at.ORDINAL), True)


def test_agreement_adjudication_finalize_flow(pkg):
    la = _all(pkg)
    lb = _all(pkg)
    lb[0]["support_label"] = "unsupported"
    lb[1]["evidence_relevance"] = "irrelevant"
    at.register(_labels(pkg["tmp"] / "a.csv", la), "A")
    at.register(_labels(pkg["tmp"] / "b.csv", lb), "B")
    at.agreement()
    adj = at.read_rows(pkg["tmp"] / "adjudication" / "adjudication_sheet.csv")
    assert [r["audit_id"] for r in adj] == ["P001", "P002"]
    assert adj[0]["adjudicated_support_label"] == "" and adj[0]["adjudicated_evidence_relevance"] == "relevant"
    assert adj[1]["adjudicated_support_label"] == "supported" and adj[1]["adjudicated_evidence_relevance"] == ""
    with pytest.raises(at.AuditError, match="incomplete"):
        at.finalize()                                                # decisions not entered yet
    adj[0].update(adjudicated_support_label="unsupported", resolution="discussion")
    adj[1].update(resolution="unresolved")
    _write_csv(pkg["tmp"] / "adjudication" / "adjudication_sheet.csv", at.ADJ_FIELDS, adj)
    at.finalize()
    ref = {r["audit_id"]: r for r in at.read_rows(pkg["tmp"] / "results" / "reference_labels.csv")}
    assert ref["P001"]["support_label"] == "unsupported" and ref["P001"]["source"] == "adjudicated_discussion"
    assert ref["P002"]["source"] == "unresolved" and ref["P003"]["source"] == "agreed"
    with pytest.raises(at.AuditError, match="written once"):
        at.finalize()


def test_unseal_refuses_uncommitted_reference(pkg):
    subprocess.run(["git", "init", "-q"], cwd=pkg["tmp"], check=True)
    (pkg["tmp"] / "results").mkdir()
    _write_csv(pkg["tmp"] / "results" / "reference_labels.csv", at.REF_FIELDS,
               [{**r, "source": "agreed"} for r in _all(pkg)])
    with pytest.raises(at.AuditError, match="committed"):
        at.unseal()
    assert not (pkg["tmp"] / "results" / "audit_analysis.json").exists()


def test_analyse_weights_strata_to_the_population(pkg):
    # accepted items: all supported; rejected items: all unsupported -> perfect verifier
    ref = [{"audit_id": k["audit_id"], "support_label": "supported" if k["verifier_accepted"] else "unsupported",
            "evidence_relevance": "relevant"} for k in pkg["key"]]
    out = at.analyse(ref, pkg["key"], pkg["fill"])
    for c in at.CONDITIONS:
        s = out["conditions"][c]["strict"]
        assert s["verified_claim_precision"]["estimate"] == 1.0
        assert s["recall_population"]["estimate"] == 1.0
        assert s["hallucination_rate_population"]["estimate"] == 0.75     # 300 rejected of 400, all unsupported
        assert s["kappa_verifier_vs_human"]["estimate"] == 1.0
    # all supported: the verifier misses every supported rejected claim -> recall = 100 / 400
    ref2 = [{**r, "support_label": "supported"} for r in ref]
    s = at.analyse(ref2, pkg["key"], pkg["fill"])["conditions"]["E0"]["strict"]
    assert s["recall_population"]["estimate"] == 0.25
    # not_assessable is excluded and counted
    ref3 = [{**r, "support_label": "not_assessable"} if r["audit_id"] == "P001" else r for r in ref]
    c = at.analyse(ref3, pkg["key"], pkg["fill"])["conditions"]["E0"]
    assert c["n_not_assessable"] == 1 and c["n_usable"] == c["n_items"] - 1
