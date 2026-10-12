"""Authentification Entra ID et autorisation par rôles."""

from .jwt import AuthError, Principal, TokenVerifier, development_principal, principal_from_claims
from .roles import ROLE_PERMISSIONS, Permission, permissions_for

__all__ = [
    "ROLE_PERMISSIONS",
    "AuthError",
    "Permission",
    "Principal",
    "TokenVerifier",
    "development_principal",
    "permissions_for",
    "principal_from_claims",
]
