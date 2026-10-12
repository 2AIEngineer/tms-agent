"""Vérification des jetons Microsoft Entra ID.

Un jeton porteur n'est une preuve d'identité que si **toutes** ses propriétés
sont vérifiées. Chacune de celles qui suivent correspond à une attaque
possible, et aucune n'est optionnelle :

  - **signature** contre les clés publiques du tenant (JWKS), algorithme imposé
    à RS256. Sans contrainte d'algorithme, un jeton `alg: none` ou signé en
    HMAC avec une clé devinée passerait ;
  - **audience** : un jeton émis pour une autre application du même tenant est
    refusé. Sans cela, n'importe quelle application du client pourrait appeler
    cette API avec le jeton de son propre utilisateur ;
  - **émetteur** : le tenant attendu, et lui seul ;
  - **expiration**, avec une tolérance d'horloge de soixante secondes.

Le JWKS est mis en cache : le récupérer à chaque requête ajouterait un
aller-retour réseau sur le chemin critique et ferait dépendre l'API de la
disponibilité de Microsoft à chaque appel. La rotation des clés reste prise en
compte — un `kid` inconnu force une relecture immédiate.
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from .roles import Permission, permissions_for

logger = logging.getLogger(__name__)

ALGORITHMS = ["RS256"]
CLOCK_SKEW_S = 60


class AuthError(Exception):
    """Jeton absent, invalide ou insuffisant. Rendu en 401 ou 403."""

    def __init__(self, message: str, *, status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True, slots=True)
class Principal:
    """L'opérateur authentifié, tel que le reste de l'API le voit.

    `operator_id` est l'identifiant **stable** de l'objet Entra (`oid`), pas
    l'adresse e-mail : une adresse change, et l'historique des conversations et
    le journal d'audit doivent rester rattachés à la même personne.
    """

    operator_id: str
    name: str
    roles: tuple[str, ...]
    permissions: frozenset[Permission]
    # TMS auxquels cet opérateur a accès, lorsque le jeton le précise. Vide
    # signifie « tous ceux qui sont raccordés ».
    tms_ids: tuple[str, ...] = ()

    def may(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.may(permission):
            raise AuthError(
                f"Rôle insuffisant pour {permission.value} (rôles : {', '.join(self.roles) or 'aucun'}).",
                status=403,
            )


class TokenVerifier:
    """Vérifie un jeton Entra et en extrait l'opérateur."""

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
        cache_seconds: int = 3600,
        jwk_client: PyJWKClient | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._client = jwk_client or PyJWKClient(jwks_url, cache_keys=True, lifespan=cache_seconds, timeout=5)

    def verify(self, token: str) -> Principal:
        try:
            key = self._client.get_signing_key_from_jwt(token).key
        except httpx.HTTPError as error:  # pragma: no cover - dépend du réseau
            raise AuthError(f"Clés de signature injoignables : {error}", status=503) from error
        except Exception as error:
            raise AuthError(f"Clé de signature introuvable pour ce jeton : {error}") from error

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=CLOCK_SKEW_S,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as error:
            raise AuthError("Jeton expiré.") from error
        except jwt.InvalidAudienceError as error:
            raise AuthError("Jeton émis pour une autre application.") from error
        except jwt.InvalidIssuerError as error:
            raise AuthError("Jeton émis par un autre annuaire.") from error
        except jwt.InvalidTokenError as error:
            raise AuthError(f"Jeton invalide : {error}") from error

        return principal_from_claims(claims)


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    """Construit l'opérateur depuis les revendications du jeton.

    Les rôles sont lus dans `roles` (rôles d'application Entra) et, à défaut,
    dans `groups` : les deux formes existent selon la façon dont le client
    configure son annuaire, et exiger la première ferait échouer une
    installation par ailleurs correcte.
    """
    raw_roles = claims.get("roles") or claims.get("groups") or []
    roles = tuple(str(role) for role in raw_roles if isinstance(role, str))
    operator_id = str(claims.get("oid") or claims.get("sub") or "")
    if not operator_id:
        raise AuthError("Jeton sans identifiant d'utilisateur.")
    return Principal(
        operator_id=operator_id,
        name=str(claims.get("name") or claims.get("preferred_username") or operator_id),
        roles=roles,
        permissions=permissions_for(roles),
        tms_ids=tuple(str(item) for item in (claims.get("tms_ids") or []) if isinstance(item, str)),
    )


def development_principal(operator_id: str, roles: tuple[str, ...]) -> Principal:
    """Opérateur fictif, quand l'authentification est explicitement coupée.

    Le poste de développement n'a pas de tenant Entra. Ce mode est bruyant dans
    les journaux et n'a aucune valeur par défaut : il faut le demander.
    """
    return Principal(
        operator_id=operator_id,
        name=f"{operator_id} (développement)",
        roles=roles,
        permissions=permissions_for(roles),
    )
