"""Who may do what in the quotations portal (module slugs in magicwand/access.py).

- quotations          open the portal and take the approved PDF (customer relations)
- quotation_costing   upload versions and see the Excel workings and the tracker
- quotation_approval  approve a price or ask for a revision; also sees the workings

Top Management (the KYC department of that name) can always approve and see the workings,
whatever sections an administrator has switched on for them.
"""
from __future__ import annotations

from kyc.departments import departments_of
from magicwand.access import user_has_service_access

VIEW = "quotations"
COSTING = "quotation_costing"
APPROVAL = "quotation_approval"


TOP_MANAGEMENT = "Top Management"


def is_top_management(user) -> bool:
    return any(d.name == TOP_MANAGEMENT and d.is_active for d in departments_of(user))


def can_cost(user) -> bool:
    return user_has_service_access(user, COSTING)


def can_approve(user) -> bool:
    return user_has_service_access(user, APPROVAL) or is_top_management(user)


def sees_workings(user) -> bool:
    """The Excel files, every version and the tracker. Everyone else sees only the approved PDF."""
    return can_cost(user) or can_approve(user)


def can_open_portal(user) -> bool:
    """The Quotations module, or Top Management (who open the PDFs and decide)."""
    return user_has_service_access(user, VIEW) or is_top_management(user)
