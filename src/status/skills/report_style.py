"""Deterministic legacy report taxonomy and management-facing names."""

from __future__ import annotations

import re
from typing import Literal

ReportCategory = Literal["Partner Enablement", "Certification / CI", "Mindshare"]

PARTNER_LABS_REPOS = {
    "opdev/agentic-status-report",
    "redhat-openshift-partner-labs/fleet-clusters",
    "redhat-openshift-partner-labs/partner-labs-rhpds",
    "redhat-openshift-partner-labs/console-plugin",
}

PARTNER_NAMES = {
    "commvault": "Commvault",
    "dh2i": "DH2i",
    "dynatrace": "Dynatrace",
    "fis open payment": "FIS",
    "hycu": "HYCU",
    "ibm": "IBM",
    "omnissa": "Omnissa",
    "tailscale": "Tailscale",
}


def _contains(text: str, *phrases: str) -> bool:
    return any(phrase in text for phrase in phrases)


def classify_report_entry(
    *,
    project: str,
    epic_name: str | None,
    outcome: str,
    evidence_labels: dict[str, str] | None = None,
) -> tuple[ReportCategory, str]:
    """Return the legacy section and bold display name for a confirmed entry."""
    project_clean = project.strip()
    name_clean = (epic_name or "").strip()
    label_values = [label.strip() for label in (evidence_labels or {}).values() if label.strip()]
    label_text = " ".join(label_values)
    text = f"{project_clean} {name_clean} {outcome} {label_text}".lower()

    if project_clean.lower() in PARTNER_LABS_REPOS or _contains(
        text,
        "agentic weekly status",
        "weekly status pipeline",
        "partner labs",
        "hosting skills on openai",
        "openai instead of anthropic",
    ):
        return "Partner Enablement", "Partner Labs"

    if _contains(
        text,
        "ocp-v partner onboarding",
        "ocp virt cookbook",
        "ocp-virt-cookbook",
        "openshift virtualization partner onboarding",
    ):
        return "Partner Enablement", "OCP-V Partner Onboarding Strategy"

    identity = f"{project_clean} {name_clean}".lower()
    if _contains(
        identity,
        "guestcluster",
        "certsuite",
        "cert suite",
        "best practices certification",
        "chart-verifier",
        "chart verifier",
        "preflight",
        "operator certification operator",
        "konflux-certsuite",
        "cluster bot",
        "cluster management bot",
    ):
        report_name = (
            "Best Practices Certification Test Suite"
            if _contains(text, "guestcluster", "certsuite", "cert suite")
            else name_clean or project_clean
        )
        return "Certification / CI", report_name

    for phrase, partner_name in PARTNER_NAMES.items():
        if phrase in text:
            return "Partner Enablement", partner_name

    if _contains(
        text,
        "best practices certification",
        "chart-verifier",
        "chart verifier",
        "preflight",
        "operator certification operator",
    ):
        return "Certification / CI", name_clean or project_clean

    if _contains(
        text,
        "context window architecture",
        "contextwindowarchitecture",
        "security advisory",
        "conference",
        "upstream",
    ):
        return "Mindshare", "Upstream Open Source Leadership"

    if _contains(text, "tutorial", "workshop", "blog"):
        return "Mindshare", name_clean or project_clean

    # EET partner engagements are the dominant legacy-report default. Prefer a
    # concise epic label and drop common implementation suffixes only when the
    # source already names a recognizable initiative.
    report_name = name_clean or (label_values[0] if len(label_values) == 1 else project_clean)
    report_name = re.sub(r"^Assist\s+", "", report_name, flags=re.IGNORECASE).strip()
    return "Partner Enablement", report_name
