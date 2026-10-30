"""Policy adapter errors shared by live and historical implementations."""

class PolicyBlocked(RuntimeError):
    pass

class InvalidPolicyDecision(ValueError):
    pass
