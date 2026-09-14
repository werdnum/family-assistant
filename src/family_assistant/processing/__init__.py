"""Processing module for message handling and context preparation."""

from family_assistant.delegation_security import DelegationSecurityLevel

from .protocol import (
    PENDING,
    DelegatableService,
    DelegationPermanentError,
    DelegationTaskNotFoundError,
    DelegationTransientError,
    ObservableDelegationService,
    PendingPoll,
    PollableDelegationService,
    RemoteDisposition,
    RemoteObservation,
    RemoteObservationMetadata,
    RemoteSubmission,
)
from .service import ProcessingService
from .types import ChatInteractionResult, ProcessingServiceConfig, RemoteServiceConfig

__all__ = [
    "PENDING",
    "ChatInteractionResult",
    "DelegatableService",
    "DelegationPermanentError",
    "DelegationSecurityLevel",
    "DelegationTaskNotFoundError",
    "DelegationTransientError",
    "ObservableDelegationService",
    "PendingPoll",
    "PollableDelegationService",
    "ProcessingService",
    "ProcessingServiceConfig",
    "RemoteDisposition",
    "RemoteObservation",
    "RemoteObservationMetadata",
    "RemoteServiceConfig",
    "RemoteSubmission",
]
