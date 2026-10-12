"""Tests de la vérification des jetons Entra ID.

Chaque test correspond à une attaque, et non à un cas d'erreur générique : un
jeton signé par un autre annuaire, un jeton émis pour une autre application du
même tenant, un jeton expiré, un jeton sans rôle. Ce sont ces quatre portes
qu'un jeton porteur doit trouver fermées.

Les jetons sont signés ici avec une vraie clé RSA : vérifier une signature
contre un double rendrait le test vert sans rien garantir.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK

from services.ops_api.auth import AuthError, Permission, TokenVerifier, principal_from_claims

ISSUER = "https://login.microsoftonline.com/tenant-abc/v2.0"
AUDIENCE = "api://tms-agent-ops"
KEY_ID = "test-key-1"


@pytest.fixture(scope="module")
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private


class _StubJwkClient:
    """Sert la clé publique attendue, sans appeler Microsoft."""

    def __init__(self, private_key) -> None:
        numbers = private_key.public_key().public_numbers()
        self._jwk = PyJWK.from_dict(
            {
                "kty": "RSA",
                "kid": KEY_ID,
                "use": "sig",
                "alg": "RS256",
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        )

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        header = jwt.get_unverified_header(token)
        if header.get("kid") != KEY_ID:
            raise LookupError("clé inconnue")
        return self._jwk


def _b64(value: int) -> str:
    import base64

    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_token(private_key, **overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": time.time() + 600,
        "iat": time.time(),
        "oid": "11111111-2222-3333-4444-555555555555",
        "name": "Awa Traoré",
        "roles": ["ops_agent"],
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": overrides.pop("kid", KEY_ID)},
    )


def verifier(private_key) -> TokenVerifier:
    return TokenVerifier(
        jwks_url="https://example.invalid/keys",
        issuer=ISSUER,
        audience=AUDIENCE,
        jwk_client=_StubJwkClient(private_key),  # type: ignore[arg-type]
    )


class TestJetonValide:
    def test_operateur_identifie(self, keypair):
        principal = verifier(keypair).verify(make_token(keypair))

        # `oid` et non l'adresse e-mail : une adresse change, et l'audit doit
        # rester rattaché à la même personne.
        assert principal.operator_id == "11111111-2222-3333-4444-555555555555"
        assert principal.name == "Awa Traoré"
        assert principal.roles == ("ops_agent",)
        assert principal.may(Permission.READ)
        assert principal.may(Permission.ACT)


class TestJetonsRefuses:
    def test_jeton_expire(self, keypair):
        token = make_token(keypair, exp=time.time() - 3600)

        with pytest.raises(AuthError, match="expiré"):
            verifier(keypair).verify(token)

    def test_jeton_dune_autre_application(self, keypair):
        """Sans ce contrôle, toute application du tenant appellerait cette API."""
        token = make_token(keypair, aud="api://autre-application")

        with pytest.raises(AuthError, match="autre application"):
            verifier(keypair).verify(token)

    def test_jeton_dun_autre_annuaire(self, keypair):
        token = make_token(keypair, iss="https://login.microsoftonline.com/autre/v2.0")

        with pytest.raises(AuthError, match="autre annuaire"):
            verifier(keypair).verify(token)

    def test_jeton_signe_par_une_autre_cle(self, keypair):
        """Une signature valide mais étrangère ne doit pas passer."""
        intruse = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = make_token(intruse)

        with pytest.raises(AuthError):
            verifier(keypair).verify(token)

    def test_algorithme_impose(self, keypair):
        """Un jeton HMAC n'est pas accepté, même si la charge est correcte."""
        token = jwt.encode(
            {"iss": ISSUER, "aud": AUDIENCE, "exp": time.time() + 600, "oid": "x"},
            # Clé assez longue pour que PyJWT ne proteste pas sur sa taille :
            # ce qui est testé est le refus de l'algorithme, pas la clé.
            "secret-suffisamment-long-pour-hmac-sha256",
            algorithm="HS256",
            headers={"kid": KEY_ID},
        )

        with pytest.raises(AuthError):
            verifier(keypair).verify(token)


class TestRoles:
    def test_role_inconnu_sans_permission(self):
        """Un annuaire mal configuré doit refuser l'accès, pas l'accorder."""
        principal = principal_from_claims({"oid": "o1", "roles": ["role_inconnu"]})

        assert principal.permissions == frozenset()
        assert not principal.may(Permission.READ)

    def test_lecture_seule_ne_peut_pas_agir(self):
        principal = principal_from_claims({"oid": "o1", "roles": ["ops_viewer"]})

        assert principal.may(Permission.READ)
        assert not principal.may(Permission.ACT)
        with pytest.raises(AuthError) as refus:
            principal.require(Permission.ACT)
        assert refus.value.status == 403

    def test_groupes_acceptes_a_defaut_de_roles(self):
        """Les deux formes existent selon la configuration du client."""
        principal = principal_from_claims({"oid": "o1", "groups": ["ops_supervisor"]})

        assert principal.may(Permission.ACT)

    def test_jeton_sans_identifiant_refuse(self):
        with pytest.raises(AuthError, match="identifiant"):
            principal_from_claims({"roles": ["ops_agent"]})

    def test_union_des_permissions(self):
        principal = principal_from_claims({"oid": "o1", "roles": ["ops_viewer", "general_management"]})

        assert principal.permissions == frozenset({Permission.READ})
