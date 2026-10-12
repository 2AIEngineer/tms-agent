"""Trappe de code par TMS, pour ce qu'une config ne peut pas exprimer.

Le dialecte couvre la **donnée** : chemins, noms de champs, valeurs
d'énumération, pagination, authentification. Trois choses lui échappent par
nature, et ce sont elles qui vivent ici :

  - **un calcul** que le TMS ne fournit pas. Si un TMS ne calcule pas l'écart à
    l'itinéraire, il faut le dériver de l'itinéraire et de la position : aucune
    correspondance de champs n'y parvient ;
  - **une réduction de structure**. Notre pivot suppose une origine et une
    destination. Un TMS à tournée multi-arrêts impose de décider laquelle de
    ses cinq étapes est « la » destination : c'est une décision métier, pas un
    renommage ;
  - **un dialecte de transport exotique** : requête signée en HMAC, enveloppe
    de réponse inhabituelle, jeton à rafraîchir.

Le profil par défaut ne fait rien. Pour un TMS conforme au contrat, le dialecte
suffit et aucun code n'est écrit. Rendre la config capable de tout ce qui
précède en ferait un langage de programmation sans typage ni tests, dont les
erreurs n'apparaîtraient qu'en production — c'est précisément ce que §28 de
l'architecture demande d'éviter.
"""

import logging
from typing import Any

from .dialect import Operation, TmsDialect

logger = logging.getLogger(__name__)


class TmsProfile:
    """Comportement par défaut : aucun ajustement.

    Un profil est instancié une fois par connexion et reçoit le dialecte, ce
    qui lui permet de lire les correspondances déclarées plutôt que de
    recoder des noms de champs en dur.
    """

    name = "default"

    def __init__(self, dialect: TmsDialect) -> None:
        self.dialect = dialect

    # --- Transport ------------------------------------------------------------

    def extra_headers(self) -> dict[str, str]:
        """En-têtes supplémentaires, au-delà de ceux que le dialecte décrit.

        Point d'entrée d'une signature de requête ou d'un jeton rafraîchi.
        """
        return {}

    def transform_response(self, operation: Operation, payload: Any) -> Any:
        """Dernière chance de normaliser une réponse hors norme.

        À n'employer que si l'enveloppe ne peut pas être décrite par
        `result_path` ou `items_path` du dialecte.
        """
        return payload

    # --- Structure et calculs -------------------------------------------------

    def normalize_mission(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ramène une mission au cadre attendu par le dialecte.

        C'est ici qu'un TMS multi-arrêts choisit son origine et sa destination
        parmi ses étapes, avant que les correspondances de champs s'appliquent.
        """
        return payload

    def compute_deviation(
        self,
        *,
        mission: dict[str, Any],
        route: dict[str, Any] | None,
        vehicle: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Calcule l'écart à l'itinéraire quand le TMS ne le fournit pas.

        Rend `None` par défaut : l'écart reste alors absent, et le rapport de
        capacité signale que les règles de dérive ne déclencheront jamais.
        Mieux vaut un manque documenté qu'un chiffre inventé.
        """
        return None


_PROFILES: dict[str, type[TmsProfile]] = {TmsProfile.name: TmsProfile}


def register_profile(profile: type[TmsProfile]) -> type[TmsProfile]:
    """Inscrit un profil, pour qu'un dialecte puisse le nommer."""
    if profile.name in _PROFILES and _PROFILES[profile.name] is not profile:
        raise RuntimeError(f"Profil déjà enregistré : {profile.name}")
    _PROFILES[profile.name] = profile
    return profile


def build_profile(dialect: TmsDialect) -> TmsProfile:
    """Instancie le profil nommé par le dialecte, ou le profil par défaut."""
    name = dialect.profile or TmsProfile.name
    implementation = _PROFILES.get(name)
    if implementation is None:
        known = ", ".join(sorted(_PROFILES))
        raise ValueError(f"Profil inconnu : '{name}' (disponibles : {known})")
    return implementation(dialect)


def profile_names() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))
