"""Dimension `tms_id` : plusieurs TMS raccordés simultanément

`mission_id` et `event_id` viennent du TMS : ils ne sont uniques que **dans**
ce TMS. Deux TMS portant une mission `MSN-1` entraient en collision sur deux
mécanismes de correction, et les deux défauts étaient silencieux :

  - l'index unique partiel des alertes ouvertes portait sur la seule clé de
    déduplication : l'alerte du second TMS était supprimée comme doublon du
    premier, et un camion en dérive ne déclenchait rien ;
  - la clé primaire de `processed_events` était l'identifiant d'événement seul :
    un événement du second TMS passait pour déjà traité, et son alerte ne
    partait jamais.

Cette migration ajoute la provenance et recompose les deux clés. Les lignes
existantes sont rattachées au TMS de référence, seul raccordé jusqu'ici : la
valeur par défaut sert à la reprise puis est retirée, pour qu'un appelant qui
oublierait la provenance échoue au lieu d'être attribué au hasard.

Revision ID: f4f389b372ac
Revises: db3729c8c5b5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4f389b372ac"
down_revision: str | Sequence[str] | None = "db3729c8c5b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "orchestrator"
# Tout ce qui existe en base provient du TMS de référence.
LEGACY_TMS_ID = "x_tms"


def _add_provenance(table: str) -> None:
    """Ajoute `tms_id`, en rattachant les lignes existantes au TMS de référence.

    La valeur par défaut n'existe que le temps de la reprise : la laisser
    ferait silencieusement attribuer au TMS de référence toute ligne insérée
    sans provenance.
    """
    op.add_column(
        table,
        sa.Column(
            "tms_id",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text(f"'{LEGACY_TMS_ID}'"),
        ),
        schema=SCHEMA,
    )
    op.alter_column(table, "tms_id", server_default=None, schema=SCHEMA)


def upgrade() -> None:
    _add_provenance("alerts")
    _add_provenance("notification_outbox")
    _add_provenance("processed_events")
    op.add_column(
        "audit_log", sa.Column("tms_id", sa.String(length=64), nullable=True), schema=SCHEMA
    )

    # Alembic n'autogénère pas les changements de clé primaire. Sans ce
    # remplacement, l'identifiant d'événement resterait unique à lui seul et la
    # collision d'idempotence entre TMS subsisterait — sans aucun symptôme,
    # sinon des alertes qui ne partent jamais.
    op.drop_constraint("pk_processed_events", "processed_events", schema=SCHEMA, type_="primary")
    op.create_primary_key(
        "pk_processed_events", "processed_events", ["tms_id", "event_id"], schema=SCHEMA
    )

    # --- Déduplication des alertes, désormais cantonnée à un TMS -------------
    op.drop_index("uq_alerts_open_deduplication_key", table_name="alerts", schema=SCHEMA)
    op.create_index(
        "uq_alerts_open_tms_id_deduplication_key",
        "alerts",
        ["tms_id", "deduplication_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.drop_index("ix_alerts_deduplication_key_detected_at", table_name="alerts", schema=SCHEMA)
    op.create_index(
        "ix_alerts_tms_id_deduplication_key_detected_at",
        "alerts",
        ["tms_id", "deduplication_key", "detected_at"],
        schema=SCHEMA,
    )
    op.drop_index("ix_alerts_mission_id_status", table_name="alerts", schema=SCHEMA)
    op.create_index(
        "ix_alerts_tms_id_mission_id_status",
        "alerts",
        ["tms_id", "mission_id", "status"],
        schema=SCHEMA,
    )

    op.drop_index(
        "ix_processed_events_mission_id_occurred_at",
        table_name="processed_events",
        schema=SCHEMA,
    )
    op.create_index(
        "ix_processed_events_tms_id_mission_id_occurred_at",
        "processed_events",
        ["tms_id", "mission_id", "occurred_at"],
        schema=SCHEMA,
    )

    op.drop_index("ix_audit_log_mission_id_occurred_at", table_name="audit_log", schema=SCHEMA)
    op.create_index(
        "ix_audit_log_tms_id_mission_id_occurred_at",
        "audit_log",
        ["tms_id", "mission_id", "occurred_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_log_tms_id_mission_id_occurred_at", table_name="audit_log", schema=SCHEMA
    )
    op.create_index(
        "ix_audit_log_mission_id_occurred_at",
        "audit_log",
        ["mission_id", "occurred_at"],
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_processed_events_tms_id_mission_id_occurred_at",
        table_name="processed_events",
        schema=SCHEMA,
    )
    op.create_index(
        "ix_processed_events_mission_id_occurred_at",
        "processed_events",
        ["mission_id", "occurred_at"],
        schema=SCHEMA,
    )
    op.drop_index("ix_alerts_tms_id_mission_id_status", table_name="alerts", schema=SCHEMA)
    op.create_index(
        "ix_alerts_mission_id_status", "alerts", ["mission_id", "status"], schema=SCHEMA
    )
    op.drop_index(
        "ix_alerts_tms_id_deduplication_key_detected_at", table_name="alerts", schema=SCHEMA
    )
    op.create_index(
        "ix_alerts_deduplication_key_detected_at",
        "alerts",
        ["deduplication_key", "detected_at"],
        schema=SCHEMA,
    )
    op.drop_index("uq_alerts_open_tms_id_deduplication_key", table_name="alerts", schema=SCHEMA)
    op.create_index(
        "uq_alerts_open_deduplication_key",
        "alerts",
        ["deduplication_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'open'"),
    )

    # Le retour en arrière ne peut pas être fidèle : deux TMS ayant pu produire
    # le même identifiant d'événement, rétablir une clé primaire sur `event_id`
    # seul échouerait sur les doublons. Les lignes des autres TMS sont donc
    # supprimées — seul retour cohérent possible, et la raison pour laquelle ce
    # retour arrière ne doit pas être joué sur un système multi-TMS en service.
    op.execute(f"DELETE FROM {SCHEMA}.processed_events WHERE tms_id <> '{LEGACY_TMS_ID}'")
    op.drop_constraint("pk_processed_events", "processed_events", schema=SCHEMA, type_="primary")
    op.create_primary_key("pk_processed_events", "processed_events", ["event_id"], schema=SCHEMA)

    op.drop_column("audit_log", "tms_id", schema=SCHEMA)
    op.drop_column("processed_events", "tms_id", schema=SCHEMA)
    op.drop_column("notification_outbox", "tms_id", schema=SCHEMA)
    op.drop_column("alerts", "tms_id", schema=SCHEMA)
