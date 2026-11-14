"""Explicit controller-owned requirements; never accept executable tests from an API request."""
from dataclasses import dataclass
import hashlib


@dataclass(frozen=True)
class MaintainedContract:
    """Bind immutable test text to one exact request, with auditable provenance.

    Maintained means authored before execution, not automatically human-approved.
    Callers must validate tests against references and negative controls separately.
    """
    identifier: str
    request: str
    requirements: tuple[str, ...]
    tests: str
    provenance: str

    @property
    def sha256(self) -> str:
        """Identify the exact request, requirements and test source used by a run."""
        import json
        from dataclasses import asdict
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
