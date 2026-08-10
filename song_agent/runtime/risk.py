from __future__ import annotations

from ..kernel.intent import IntentSpec, OperationKind, RiskLevel

_MINIMUM_RISK = {
    OperationKind.READ: RiskLevel.LOW,
    OperationKind.CREATE: RiskLevel.LOW,
    OperationKind.UPDATE: RiskLevel.MEDIUM,
    OperationKind.DELETE: RiskLevel.HIGH,
    OperationKind.OVERWRITE: RiskLevel.HIGH,
    OperationKind.BULK: RiskLevel.HIGH,
}
_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


class RiskPolicy:
    def __init__(self, *, confirm_all_writes: bool = False) -> None:
        self.confirm_all_writes = confirm_all_writes

    def effective_risk(self, spec: IntentSpec) -> RiskLevel:
        floor = _MINIMUM_RISK[spec.operation_kind]
        return spec.risk_level if _ORDER[spec.risk_level] >= _ORDER[floor] else floor

    def requires_confirmation(self, spec: IntentSpec) -> bool:
        if self.confirm_all_writes and spec.is_write:
            return True
        return spec.confirmation_policy.required or self.effective_risk(spec) is RiskLevel.HIGH
