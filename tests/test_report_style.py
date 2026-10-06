from __future__ import annotations

from status.skills.report_style import classify_report_entry


def test_agentic_status_pipeline_maps_to_partner_labs() -> None:
    assert classify_report_entry(
        project="opdev/agentic-status-report",
        epic_name="Agentic Weekly Status Pipeline",
        outcome="Continued wiring lock-and-report through synthesis.",
    ) == ("Partner Enablement", "Partner Labs")


def test_ocp_virt_cookbook_maps_to_partner_enablement() -> None:
    assert classify_report_entry(
        project="RedHatQuickCourses/ocp-virt-cookbook",
        epic_name="OCP-V Partner Onboarding Strategy - OCP Virt CookBook",
        outcome="Continued four onboarding tutorials.",
    ) == ("Partner Enablement", "OCP-V Partner Onboarding Strategy")


def test_guestcluster_maps_to_certification_test_suite() -> None:
    assert classify_report_entry(
        project="opdev/guestcluster",
        epic_name=None,
        outcome="Fixed lease cleanup and TTL handling.",
    ) == ("Certification / CI", "Best Practices Certification Test Suite")


def test_upstream_security_work_maps_to_mindshare() -> None:
    assert classify_report_entry(
        project="container-libs",
        epic_name=None,
        outcome="Reported an upstream security advisory.",
    ) == ("Mindshare", "Upstream Open Source Leadership")


def test_partner_certification_support_stays_in_partner_enablement() -> None:
    assert classify_report_entry(
        project="EET",
        epic_name="Assist Tailscale with certifying Kubernetes Operator workload for OpenShift",
        outcome="Completed Red Hat Preflight certification checks.",
    ) == ("Partner Enablement", "Tailscale")


def test_partner_integration_uses_concise_legacy_name() -> None:
    assert classify_report_entry(
        project="EET",
        epic_name="[Omnissa] Integration of Horizon with OpenShift Virtualization",
        outcome="Reviewed the scalability plan.",
    ) == ("Partner Enablement", "Omnissa")


def test_partner_labs_evidence_label_prevents_generic_jira_project_name() -> None:
    assert classify_report_entry(
        project="EET",
        epic_name=None,
        outcome="The intake-column alignment fix was in progress.",
        evidence_labels={
            "EET-5573": "partner Labs Maintenance: Resolve column issue with intake form"
        },
    ) == ("Partner Enablement", "Partner Labs")
