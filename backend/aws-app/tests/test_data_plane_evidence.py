from agent_runtime.data_plane_evidence import DataPlaneEvidence


def test_parses_pfcp_session_modification_seid_teid_and_qer() -> None:
    parsed = DataPlaneEvidence.parse_pfcp_log(
        "PFCP Session Modification Request SEID=101 TEID=0xabc QER ID=9\n"
        "PFCP Session Modification Response SEID=101 accepted"
    )
    assert parsed["sessionModificationRequests"] == 1
    assert parsed["sessionModificationResponses"] == 1
    assert parsed["seids"] == ["101"]
    assert parsed["teids"] == ["2748"]
    assert parsed["qerIds"] == ["9"]


def test_parses_gtp5g_pdr_far_qer_and_teid() -> None:
    parsed = DataPlaneEvidence.parse_gtp5g_dump(
        "PDR\nPDR-ID: 1 SEID=101 TEID: 0xabc\nFAR\nFAR-ID: 2\nQER\nQER-ID: 9"
    )
    assert parsed == {
        "pdrCount": 1,
        "farCount": 1,
        "qerCount": 1,
        "teids": ["2748"],
        "seids": ["101"],
        "qerIds": ["9"],
    }


def test_canonicalizes_hex_pfcp_and_decimal_gtp5g_json_identifiers() -> None:
    pfcp = DataPlaneEvidence.parse_pfcp_log(
        "PFCP Session Modification Request sent SEID=1 SEID=2 TEID=0x2 TEID=0x1 QERID=1\n"
        "PFCP Session Modification Response accepted SEID=1 SEID=2 TEID=0x2 TEID=0x1"
    )
    kernel = DataPlaneEvidence.parse_gtp5g_dump(
        'PDR\n{"ID":1,"SEID":2,"PDI":{"FTEID":{"TEID":2}}}\n'
        'FAR\n{"ID":1,"SEID":2,"ForwardingParameters":{"OuterHeaderCreation":{"TEID":1}}}\n'
        'QER\n{"ID":1,"SEID":2,"QER ID":1,"MBR":{"UL":8000,"DL":8000}}'
    )

    assert pfcp["seids"] == ["1", "2"]
    assert pfcp["teids"] == ["1", "2"]
    assert kernel == {
        "pdrCount": 1,
        "farCount": 1,
        "qerCount": 1,
        "teids": ["1", "2"],
        "seids": ["2"],
        "qerIds": ["1"],
    }
    assert set(pfcp["seids"]) & set(kernel["seids"]) == {"2"}
    assert set(pfcp["teids"]) & set(kernel["teids"]) == {"1", "2"}


def test_parses_nested_full_json_dump_and_numeric_strings() -> None:
    parsed = DataPlaneEvidence.parse_gtp5g_dump(
        '{"PDRs":[{"PDR-ID":"01","F-SEID":"0x2","F-TEID":{"TEID":"0x02"}}],'
        '"FARs":[{"FAR-ID":1,"OuterHeaderCreation":{"TEID":1}}],'
        '"QERs":[{"QER-ID":"0x1","SEID":2}]}'
    )

    assert parsed == {
        "pdrCount": 1,
        "farCount": 1,
        "qerCount": 1,
        "teids": ["1", "2"],
        "seids": ["2"],
        "qerIds": ["1"],
    }


def test_assess_confirms_only_when_all_three_evidence_levels_are_present() -> None:
    trusted_evidence = {
        "pfcp": {
            "sessionModificationRequests": 1,
            "sessionModificationResponses": 1,
            "seids": ["101"],
        },
        "kernel": {"pdrCount": 1, "farCount": 1, "qerCount": 1, "teids": ["0xabc"]},
        "effect": {
            "measurementSource": "ue-tun-iperf3",
            "beforeMbps": 5,
            "afterMbps": 1,
            "expectedMbps": 1,
        },
    }
    assessed = DataPlaneEvidence.assess(trusted_evidence)
    assert assessed["actuatorStatus"] == "confirmed"
    assert assessed["pfcpEvidence"]["status"] == "confirmed"
    assert assessed["kernelEvidence"]["status"] == "confirmed"
    assert assessed["effectEvidence"]["status"] == "confirmed"


def test_assess_marks_stock_runtime_unsupported_instead_of_success() -> None:
    trusted_evidence = {
        "pfcp": {"supported": False},
        "kernel": {"supported": False},
    }
    assessed = DataPlaneEvidence.assess(trusted_evidence)
    assert assessed["actuatorStatus"] == "unsupported"
    assert assessed["pfcpEvidence"]["status"] == "unsupported"
    assert assessed["kernelEvidence"]["status"] == "unsupported"
