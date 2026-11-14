"""Who may do what in the quotations portal (module slugs in magicwand/access.py).

- quotations          open the portal and take the approved PDF (customer relations)
- quotation_costing   upload versions and see the Excel workings and the tracker
- quotation_approval  approve a price or ask for a revision; also sees the workings
"""
from __future__ import annotations

from magicwand.access import user_has_service_access

VIEW = "quotations"
COSTING = "quotation_costing"
APPROVAL = "quotation_approval"


def can_cost(user) -> bool:
    return user_has_service_access(user, COSTING)


def can_approve(user) -> bool:
    return user_has_service_access(user, APPROVAL)


def sees_workings(user) -> bool:
    """The Excel files, every version and the tracker. Everyone else sees only the approved PDF."""
    return can_cost(user) or can_approve(user)
