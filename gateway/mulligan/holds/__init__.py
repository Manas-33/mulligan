"""Stream B's held channels, plugged into the core by `mulligan.services`.

* ``create_hold_manager``: the approval state machine, texting the approver through the Photon
  service (iMessage) and expiring undecided holds.
* ``create_adapters``: the email and payments adapters (Phase 3; none yet).
"""

from __future__ import annotations

from mulligan.holds.manager import PhotonHoldManager, hold_ttl
from mulligan.holds.notify import PhotonNotifier
from mulligan.interfaces import Adapter, HoldDeps, HoldManager


def create_hold_manager(deps: HoldDeps) -> HoldManager:
    return PhotonHoldManager(deps, PhotonNotifier.from_env(), ttl=hold_ttl())


def create_adapters() -> list[Adapter]:
    return []
