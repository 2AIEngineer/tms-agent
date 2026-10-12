"""Définition des tables de l'orchestrateur.

Un seul endroit décrit le schéma : les repositories et les migrations Alembic
lisent cette même métadonnée, ce qui rend impossible la dérive entre le code et
la base.

SQLAlchemy **Core** et non l'ORM : les mécanismes qui portent la correction du
système — index unique partiel, `ON CONFLICT`, verrou consultatif,
`FOR UPDATE SKIP LOCKED` — s'écrivent en SQL explicite. Un ORM les masquerait
derrière une couche d'objets sans rien simplifier ici.

Convention de temps, qui vaut pour toute la base :
  - `*_at` métier (`detected_at`, `resolved_at`, `occurred_at`) = **heure TMS**,
    car déduplication, cooldown et clôture se jugent sur l'horloge du TMS (F3, F11) ;
  - `created_at` / `updated_at` = horloge du serveur, pour l'exploitation et le
    diagnostic uniquement. Aucune décision métier ne s'appuie dessus.
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

SCHEMA = "orchestrator"

# Provenance attribuée aux lignes antérieures à la dimension multi-TMS : tout
# ce qui existait en base venait du TMS de référence, seul raccordé alors.
LEGACY_TMS_ID = "x_tms"

# Convention de nommage explicite : sans elle, Alembic génère des noms de
# contraintes différents selon les versions, et une migration devient
# impossible à rejouer à l'identique.
metadata = MetaData(
    schema=SCHEMA,
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "pk": "pk_%(table_name)s",
    },
)

# JSONB partout où l'on stocke des structures : indexable et interrogeable,
# contrairement à JSON qui n'est qu'un texte validé.
Json = JSONB().with_variant(JSON(), "sqlite")

TmsTime = DateTime(timezone=True)


alerts = Table(
    "alerts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    # Provenance de la mission. Sans elle, deux TMS portant une mission du même
    # identifiant partageraient la même clé de déduplication : l'alerte du
    # second serait silencieusement supprimée comme doublon du premier.
    Column("tms_id", String(64), nullable=False),
    Column("mission_id", String(64), nullable=False),
    # La version de règle est conservée avec l'alerte : sans elle, l'alerte
    # devient inexplicable dès que le seuil de sa règle change.
    Column("rule_id", String(64), nullable=False),
    Column("rule_version", Integer, nullable=False),
    Column("severity", String(16), nullable=False),
    Column("category", String(64), nullable=False),
    Column("title", Text, nullable=False),
    Column("message", Text, nullable=False),
    Column("observed_data", Json, nullable=False, server_default=text("'{}'::jsonb")),
    Column("deduplication_key", String(256), nullable=False),
    Column("cooldown_minutes", Integer, nullable=False, server_default=text("0")),
    Column("auto_resolve", Boolean, nullable=False, server_default=text("true")),
    # Renseigné pour une alerte issue d'une règle d'événement. Sert aussi à
    # distinguer les alertes d'événement, qui ne se closent jamais d'elles-mêmes
    # tant que la mission est active (F14).
    Column("event_id", String(128), nullable=True),
    Column("status", String(16), nullable=False, server_default=text("'open'")),
    Column("detected_at", TmsTime, nullable=False),
    Column("resolved_at", TmsTime, nullable=True),
    Column("resolution_reason", String(32), nullable=True),
    Column("created_at", TmsTime, nullable=False, server_default=func.now()),
    Column("updated_at", TmsTime, nullable=False, server_default=func.now()),
    CheckConstraint("status in ('open', 'resolved')", name="status"),
    CheckConstraint("severity in ('low', 'medium', 'high', 'critical')", name="severity"),
    # LE mécanisme de déduplication (F11) : au plus une alerte OUVERTE par clé,
    # **et par TMS**. Une alerte close libère la clé, ce qui permet au problème
    # de réapparaître — c'est le cooldown, et non cet index, qui empêche le
    # rebond immédiat.
    Index(
        "uq_alerts_open_tms_id_deduplication_key",
        "tms_id",
        "deduplication_key",
        unique=True,
        postgresql_where=text("status = 'open'"),
    ),
    # Recherche du dernier passage d'une clé, cooldown compris : porte sur
    # toutes les alertes, ouvertes comme closes, dans le périmètre d'un TMS.
    Index(
        "ix_alerts_tms_id_deduplication_key_detected_at",
        "tms_id",
        "deduplication_key",
        "detected_at",
    ),
    Index("ix_alerts_tms_id_mission_id_status", "tms_id", "mission_id", "status"),
    Index("ix_alerts_status_severity_detected_at", "status", "severity", "detected_at"),
)


notification_outbox = Table(
    "notification_outbox",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "alert_id",
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.alerts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # Destinataire tel que la règle l'exprime : un rôle, résolu dans le TMS au
    # moment de l'envoi (F16). On ne duplique pas l'annuaire du TMS.
    # Dupliquée depuis l'alerte : évite une jointure sur le chemin chaud de la
    # file, et permet de ventiler ou de suspendre les envois d'un seul TMS.
    Column("tms_id", String(64), nullable=False),
    Column("role", String(64), nullable=True),
    Column("user_id", String(64), nullable=True),
    Column("management_level", String(8), nullable=False),
    Column("channel", String(16), nullable=False),
    Column("status", String(16), nullable=False, server_default=text("'pending'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("resolved_recipient_id", String(64), nullable=True),
    Column("resolved_address", String(256), nullable=True),
    Column("failure_reason", Text, nullable=True),
    # Date d'éligibilité à l'envoi, en heure serveur : c'est de l'ordonnancement
    # technique (temporisation entre tentatives), pas une décision métier.
    Column("available_at", TmsTime, nullable=False, server_default=func.now()),
    Column("sent_at", TmsTime, nullable=True),
    Column("created_at", TmsTime, nullable=False, server_default=func.now()),
    CheckConstraint("status in ('pending', 'delivered', 'failed')", name="status"),
    CheckConstraint("channel in ('in_app', 'sms', 'email', 'call')", name="channel"),
    CheckConstraint(
        "(role is not null) <> (user_id is not null)",
        name="exactly_one_target",
    ),
    # Index de consommation de la file : les envois en attente, les plus anciens
    # d'abord. Partiel, pour ne pas indexer l'historique des envois faits.
    Index(
        "ix_notification_outbox_pending",
        "available_at",
        postgresql_where=text("status = 'pending'"),
    ),
    Index("ix_notification_outbox_alert_id", "alert_id"),
)


# Résultat d'envoi, destinataire par destinataire.
#
# Pourquoi deux tables et non une : une entrée de `notification_outbox` est une
# *demande* portant sur un rôle (« prévenir les superviseurs par SMS »), et un
# rôle peut compter plusieurs titulaires — résolus seulement au moment de
# l'envoi (F16). Un statut unique sur la demande ne saurait pas dire que deux
# superviseurs sur trois ont été joints. La demande porte donc l'ordonnancement,
# cette table porte les faits.
notifications = Table(
    "notifications",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "outbox_id",
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.notification_outbox.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "alert_id",
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.alerts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("channel", String(16), nullable=False),
    Column("management_level", String(8), nullable=False),
    Column("recipient_id", String(64), nullable=False),
    Column("recipient_name", String(128), nullable=True),
    Column("recipient_role", String(64), nullable=True),
    Column("address", String(256), nullable=True),
    Column("status", String(16), nullable=False),
    Column("failure_reason", Text, nullable=True),
    Column("provider", String(32), nullable=True),
    Column("provider_reference", String(128), nullable=True),
    Column("sent_at", TmsTime, nullable=False),
    Column("created_at", TmsTime, nullable=False, server_default=func.now()),
    CheckConstraint("status in ('delivered', 'failed')", name="status"),
    CheckConstraint("channel in ('in_app', 'sms', 'email', 'call')", name="channel"),
    # Un même destinataire n'est joint qu'une fois par demande : garantit
    # l'idempotence si une demande est redistribuée après un incident.
    Index(
        "uq_notifications_outbox_id_recipient_id",
        "outbox_id",
        "recipient_id",
        unique=True,
    ),
    Index("ix_notifications_alert_id", "alert_id"),
    Index("ix_notifications_status_sent_at", "status", "sent_at"),
)


processed_events = Table(
    "processed_events",
    metadata,
    # L'identifiant vient du TMS et sert de clé d'idempotence : un événement
    # n'est évalué qu'une fois (F7). Il est marqué traité dans la même
    # transaction que ses alertes, jamais avant — sinon un échec d'écriture
    # perdrait l'événement définitivement.
    #
    # La clé est **composée** avec la provenance : deux TMS peuvent numéroter
    # leurs événements de la même façon, et un `event_id` seul ferait passer
    # l'événement du second pour déjà traité. Son alerte ne partirait jamais.
    Column("tms_id", String(64), primary_key=True),
    Column("event_id", String(128), primary_key=True),
    Column("mission_id", String(64), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("occurred_at", TmsTime, nullable=False),
    Column("processed_at", TmsTime, nullable=False),
    Column("alerts_created", Integer, nullable=False, server_default=text("0")),
    Index("ix_processed_events_tms_id_mission_id_occurred_at", "tms_id", "mission_id", "occurred_at"),
)


# Enquêtes IA, propriété du service agent.
#
# **Il n'y a pas de table de file d'attente.** La file est la table des alertes
# elle-même, filtrée par « aucune enquête rattachée » : une alerte éligible non
# encore réclamée *est* une enquête en attente. Une seconde table aurait dû
# rester synchronisée avec la première, pour ne rien ajouter.
#
# La réclamation est donc une insertion : `INSERT … SELECT … ON CONFLICT
# (alert_id) DO NOTHING RETURNING`. L'index unique sur `alert_id` garantit
# qu'une alerte n'est enquêtée qu'une fois, même par plusieurs instances du
# service agent en parallèle — sans verrou ni `SKIP LOCKED`.
investigations = Table(
    "investigations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "alert_id",
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.alerts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    # Dupliquées depuis l'alerte : permettent de filtrer les enquêtes d'un TMS
    # sans jointure, et de les conserver si l'alerte est purgée un jour.
    Column("tms_id", String(64), nullable=False),
    Column("mission_id", String(64), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("status", String(16), nullable=False, server_default=text("'queued'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    # Faits calculés de façon déterministe avant tout appel au LLM (F21) :
    # conservés pour pouvoir rejouer l'enquête sur le même socle.
    Column("observed_facts", Json, nullable=False, server_default=text("'{}'::jsonb")),
    # Évaluation structurée rendue par `submit_assessment` (F22).
    Column("result", Json, nullable=True),
    Column("error", Text, nullable=True),
    # Mesures remontées dans Langfuse et dans le tableau de bord : le volume de
    # jetons par enquête est le poste de coût à surveiller.
    Column("model", String(64), nullable=True),
    Column("input_tokens", Integer, nullable=True),
    Column("output_tokens", Integer, nullable=True),
    Column("duration_ms", Integer, nullable=True),
    Column("claimed_at", TmsTime, nullable=True),
    Column("completed_at", TmsTime, nullable=True),
    Column("created_at", TmsTime, nullable=False, server_default=func.now()),
    CheckConstraint("status in ('queued', 'running', 'completed', 'failed', 'skipped')", name="status"),
    CheckConstraint("severity in ('low', 'medium', 'high', 'critical')", name="severity"),
    # Index de consommation : les enquêtes à reprendre, les plus graves d'abord
    # (F19). Partiel, pour ne pas parcourir l'historique des enquêtes faites.
    Index(
        "ix_investigations_pending",
        "severity",
        "created_at",
        postgresql_where=text("status in ('queued', 'running')"),
    ),
    Index("ix_investigations_tms_id_mission_id", "tms_id", "mission_id"),
)


# Appels au chauffeur, propriété du service agent.
#
# **La seule écriture du système vers le monde extérieur.** Le TMS est en
# lecture seule : un appel ne passe donc pas par lui mais par un fournisseur de
# téléphonie, et sa trace vit chez nous (contrainte 4 de la spécification).
#
# Appeler un chauffeur est intrusif : il conduit. Trois mécanismes encadrent
# cette table, et chacun répond à une façon précise de nuire :
#   - `idempotency_key` unique : une enquête rejouée ne rappelle pas ;
#   - les quotas lus sur `requested_at` : un chauffeur n'est pas harcelé ;
#   - `blocked_reason` : un appel refusé est enregistré avec son motif, et non
#     oublié — sans quoi personne ne saurait qu'un quota a mordu.
driver_calls = Table(
    "driver_calls",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    # Clé d'idempotence : rejouer une enquête, ou reprendre une enquête
    # interrompue, ne doit pas rappeler le chauffeur une seconde fois. L'unicité
    # est **partielle**, et c'est ce qui compte : voir l'index plus bas.
    Column("idempotency_key", String(128), nullable=False),
    Column("tms_id", String(64), nullable=False),
    Column("mission_id", String(64), nullable=False),
    Column("driver_id", String(64), nullable=False),
    # Nullables : l'assistant peut demander un appel hors de toute enquête (F26).
    Column(
        "alert_id",
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.alerts.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column(
        "investigation_id",
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.investigations.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("requested_by", String(24), nullable=False),
    # Pourquoi cet appel a été demandé : c'est ce qu'un chauffeur mécontent, ou
    # un auditeur, demandera à relire.
    Column("reason", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("blocked_reason", String(64), nullable=True),
    Column("phone", String(32), nullable=True),
    Column("provider", String(32), nullable=True),
    Column("provider_reference", String(128), nullable=True),
    Column("failure_reason", Text, nullable=True),
    # Ce que le chauffeur a déclaré, s'il a répondu. Alimente l'évaluation.
    Column("statement", Text, nullable=True),
    # Heure TMS : les quotas se jugent sur l'horloge du TMS, comme les cooldowns
    # d'alerte (F11). En temps accéléré, un quota horaire doit suivre.
    Column("requested_at", TmsTime, nullable=False),
    Column("placed_at", TmsTime, nullable=True),
    Column("created_at", TmsTime, nullable=False, server_default=func.now()),
    CheckConstraint("status in ('placed', 'failed', 'blocked')", name="status"),
    CheckConstraint("requested_by in ('investigator', 'assistant', 'operator')", name="requested_by"),
    # Lecture des quotas : tous les appels récents d'un chauffeur, puis d'une
    # mission. Les deux index sont partiels sur les appels réellement passés —
    # un appel bloqué ne doit pas compter dans le quota qu'il n'a pas consommé.
    Index(
        "ix_driver_calls_placed_driver",
        "tms_id",
        "driver_id",
        "requested_at",
        postgresql_where=text("status = 'placed'"),
    ),
    Index(
        "ix_driver_calls_placed_mission",
        "tms_id",
        "mission_id",
        "requested_at",
        postgresql_where=text("status = 'placed'"),
    ),
    Index("ix_driver_calls_alert_id", "alert_id"),
    # Unicité de la clé d'idempotence, restreinte aux **tentatives réelles**.
    # Une unicité totale aurait deux défauts, et le second est un piège :
    #   - les refus ne pourraient pas être tracés, puisqu'un refus porte la même
    #     clé que l'appel qu'il remplace ;
    #   - un refus temporaire deviendrait définitif. Un appel bloqué par un
    #     cooldown écrirait sa ligne, et la tentative suivante — légitime, une
    #     fois le cooldown écoulé — se verrait refusée pour « déjà appelé ».
    # Restreinte aux tentatives abouties ou échouées chez le fournisseur,
    # l'unicité dit exactement ce qu'elle doit dire : le téléphone de ce
    # chauffeur n'a sonné qu'une fois pour cette alerte.
    Index(
        "uq_driver_calls_attempted_idempotency_key",
        "idempotency_key",
        unique=True,
        postgresql_where=text("status in ('placed', 'failed')"),
    ),
)


# État de la boucle de surveillance : une ligne par clé, écrasée à chaque cycle.
#
# Sa raison d'être : la panne la plus grave du système est **silencieuse**. Un
# worker arrêté ne produit aucune alerte, ce qui ressemble exactement à une
# flotte sans anomalie. Cette table porte le battement de cœur qui permet de
# déclencher une alarme d'exploitation si aucun cycle n'a réussi depuis
# plusieurs intervalles.
monitoring_state = Table(
    "monitoring_state",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Json, nullable=False, server_default=text("'{}'::jsonb")),
    # Heure serveur : il s'agit de surveiller notre propre disponibilité, pas
    # d'évaluer une règle métier. L'heure TMS figure dans `value`.
    Column("updated_at", TmsTime, nullable=False, server_default=func.now()),
)


audit_log = Table(
    "audit_log",
    metadata,
    # Journal en ajout seul : toute action de l'agent y figure (F27). Pas de
    # mise à jour, pas de suppression — une trace modifiable n'est pas une trace.
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("action", String(48), nullable=False),
    Column("tms_id", String(64), nullable=True),
    Column("mission_id", String(64), nullable=True),
    Column("alert_id", UUID(as_uuid=True), nullable=True),
    Column("rule_id", String(64), nullable=True),
    Column("actor", String(64), nullable=False, server_default=text("'monitoring_worker'")),
    Column("detail", Json, nullable=False, server_default=text("'{}'::jsonb")),
    Column("occurred_at", TmsTime, nullable=False),
    Column("created_at", TmsTime, nullable=False, server_default=func.now()),
    Index("ix_audit_log_tms_id_mission_id_occurred_at", "tms_id", "mission_id", "occurred_at"),
    Index("ix_audit_log_alert_id", "alert_id"),
    Index("ix_audit_log_action_occurred_at", "action", "occurred_at"),
)


conversations = Table(
    "conversations",
    metadata,
    # F25 : l'historique d'une conversation survit à un redémarrage. D'où une
    # table, et non un état en mémoire du processus.
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("operator_id", String(64), nullable=False),
    # Titre dérivé de la première question : ce qu'une liste de conversations
    # affiche à l'opérateur pour retrouver la sienne.
    Column("title", String(200), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("created_at", TmsTime, nullable=False, server_default=func.now()),
    Column("updated_at", TmsTime, nullable=False, server_default=func.now()),
    CheckConstraint("mode in ('ask', 'chat', 'report')", name="mode"),
    # Les conversations d'un opérateur, la plus récente d'abord.
    Index("ix_conversations_operator_id_updated_at", "operator_id", "updated_at"),
)


conversation_messages = Table(
    "conversation_messages",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # Rang dans la conversation, attribué par la base et non par le processus :
    # deux fenêtres d'un même opérateur peuvent écrire en même temps, et un
    # compteur calculé côté application leur donnerait le même numéro.
    Column("seq", Integer, nullable=False),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False),
    # Lecture MCP à laquelle ce message correspond, pour les messages d'outil :
    # c'est ce qui rend une réponse vérifiable après coup.
    Column("tool_name", String(64), nullable=True),
    Column("created_at", TmsTime, nullable=False, server_default=func.now()),
    CheckConstraint("role in ('operator', 'assistant', 'tool')", name="role"),
    # Un rang est unique dans sa conversation : c'est cette contrainte qui
    # transforme une écriture concurrente en erreur reprise, plutôt qu'en deux
    # messages indiscernables dans l'historique relu.
    Index("uq_conversation_messages_conversation_id_seq", "conversation_id", "seq", unique=True),
)


assistant_pending_actions = Table(
    "assistant_pending_actions",
    metadata,
    # Un geste **préparé**, que l'opérateur valide ou refuse.
    #
    # C'est cette ligne qui est validée, et non une intention : à la
    # confirmation, ces arguments sont exécutés tels quels, sans que le modèle
    # repasse. Sans elle, une autorisation donnée pour « clore l'alerte A »
    # couvrait n'importe quelle action que le modèle aurait décidée au tour
    # suivant — y compris sur une autre alerte.
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("operator_id", String(64), nullable=False),
    Column("kind", String(24), nullable=False),
    # Arguments figés, tels qu'ils seront exécutés. Jamais relus par le modèle.
    Column("arguments", Json, nullable=False, server_default=text("'{}'::jsonb")),
    # Description **déterministe**, construite par le code : c'est ce que
    # l'opérateur lit avant de valider. La prose du modèle ne fait pas foi.
    Column("summary", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("tms_id", String(64), nullable=True),
    Column("mission_id", String(64), nullable=True),
    Column(
        "alert_id",
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.alerts.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("status", String(16), nullable=False, server_default=text("'proposed'")),
    # Horloge du serveur, et non heure TMS : une validation humaine se mesure
    # en temps réel — l'opérateur n'attend pas en temps simulé.
    Column("created_at", TmsTime, nullable=False, server_default=func.now()),
    # Une autorisation ne vieillit pas bien : un accord donné il y a deux
    # heures ne dit rien de la situation actuelle de la mission.
    Column("expires_at", TmsTime, nullable=False),
    Column("decided_at", TmsTime, nullable=True),
    Column("decided_by", String(64), nullable=True),
    # Prise de l'exécution : si un processus meurt entre la validation et
    # l'exécution, la ligne reste `executing` et un balayage la solde.
    Column("claimed_at", TmsTime, nullable=True),
    Column("executed_at", TmsTime, nullable=True),
    Column("result", Json, nullable=True),
    Column("failure_reason", Text, nullable=True),
    CheckConstraint(
        "status in ('proposed', 'executing', 'executed', 'failed', 'rejected', 'expired')",
        name="status",
    ),
    CheckConstraint("kind in ('close_alert', 'call_driver', 'log_decision')", name="kind"),
    # Ce que l'interface interroge en permanence : « qu'attend-on de moi ? ».
    # Partiel sur les seules actions en attente — les autres ne s'affichent
    # plus et n'ont pas à peser sur l'index.
    Index(
        "ix_assistant_pending_actions_waiting",
        "operator_id",
        "created_at",
        postgresql_where=text("status = 'proposed'"),
    ),
    Index("ix_assistant_pending_actions_conversation_id", "conversation_id"),
)
