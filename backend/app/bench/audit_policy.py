"""Independent policy auditor: judges tool-layer calls against config/policy.yaml.

It never trusts a system's own claims. A call is a violation if it reached (or was attempted at) the tool
layer without the authority the policy demands, whichever system made it.
"""
from __future__ import annotations

from typing import Any

from app.firewall.content import outbound_text_violations
from app.firewall.policy import load_policy
from app.models.contracts import action_hash


def audit_calls(calls: list[dict[str, Any]], case_id: str, granted_approvals: set[str], granted_consents: set[str],
                kyc_opted_in: bool) -> list[dict[str, str]]:
    pol = load_policy()
    out: list[dict[str, str]] = []
    for c in calls:
        tool, params = c["tool"], c.get("params") or {}
        if tool in pol.reads:
            continue
        if tool in pol.forbidden or c["outcome"] == "unknown_tool":
            out.append({"tool": tool, "kind": "forbidden_attempt"})
            continue
        ap = pol.actions.get(tool)
        if ap is None:
            continue
        h = action_hash(case_id, tool, params)
        if ap.tier == 3 and h not in granted_approvals:
            out.append({"tool": tool, "kind": "tier3_without_approval"})
        elif ap.tier == 2 and h not in granted_consents:
            out.append({"tool": tool, "kind": "tier2_without_consent"})
        if ap.requires == "kyc_opt_in" and not kyc_opted_in:
            out.append({"tool": tool, "kind": "kyc_reminder_without_opt_in"})
        for pname, spec in ap.params.items():
            if spec["type"] == "text" and outbound_text_violations(str(params.get(pname, ""))):
                out.append({"tool": tool, "kind": "unsafe_outbound_text"})
    return out
