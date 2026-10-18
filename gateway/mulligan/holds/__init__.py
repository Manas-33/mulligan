"""Stream B's held channels, plugged into the core by `mulligan.services`.

* ``create_hold_manager``: the approval state machine. Texts the approver through the Photon
  service (iMessage), expires undecided holds, records payment authorizations and captures them
  when their undo window ends.
* ``create_adapters``: the payments adapter (Stripe test mode); email comes next.
"""

from __future__ import annotations

from mulligan.holds.manager import PhotonHoldManager, hold_ttl
from mulligan.holds.notify import PhotonNotifier
from mulligan.holds.payments import PaymentsAdapter
from mulligan.interfaces import Adapter, HoldDeps, HoldManager

# One instance shared by the core (which runs and reverts calls) and the hold manager (which
# captures them), created when the gateway assembles its parts.
_payments: PaymentsAdapter | None = None


def payments_adapter() -> PaymentsAdapter:
    global _payments  # noqa: PLW0603
    if _payments is None:
        _payments = PaymentsAdapter()
    return _payments


def create_hold_manager(deps: HoldDeps) -> HoldManager:
    return PhotonHoldManager(
        deps, PhotonNotifier.from_env(), ttl=hold_ttl(), payments=payments_adapter()
    )


def create_adapters() -> list[Adapter]:
    return [payments_adapter()]
