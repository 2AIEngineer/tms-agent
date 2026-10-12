"""Ligne de commande du faux TMS : `uv run tms --help`."""

import logging
import signal
from datetime import datetime
from enum import Enum
from typing import Annotated

import typer

from tms.simulator.scenarios import DEFAULT_SCENARIOS, SCENARIOS

app = typer.Typer(help="Faux TMS : API REST et simulateur de missions.", no_args_is_help=True)


class Routing(str, Enum):
    static = "static"
    osrm = "osrm"


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: Annotated[bool, typer.Option(help="Rechargement automatique (développement).")] = False,
):
    """Démarre l'API (documentation sur /v1/docs)."""
    import uvicorn

    uvicorn.run("tms.api.app:app", host=host, port=port, reload=reload)


@app.command("init-db")
def init_db_cmd():
    """Crée les tables et charge les utilisateurs / véhicules de test."""
    from tms.db import init_db
    from tms.seed import seed_reference_data

    init_db()
    seed_reference_data()
    typer.echo("Base initialisée.")


@app.command()
def scenarios():
    """Liste les scénarios de mission disponibles."""
    for key, s in SCENARIOS.items():
        default = " (défaut)" if key in DEFAULT_SCENARIOS else ""
        typer.echo(f"{key:<16}{s.vehicle_id:<10}{s.driver_id:<9}{s.description}{default}")


@app.command()
def reset(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Ne pas demander de confirmation.")] = False,
):
    """Supprime missions, événements, positions et itinéraires (conserve utilisateurs et véhicules)."""
    from tms.db import init_db, session_scope
    from tms.simulator.engine import reset_data

    if not yes:
        typer.confirm("Supprimer toutes les missions et données associées ?", abort=True)
    init_db()
    with session_scope() as session:
        reset_data(session)
    typer.echo("Données de missions supprimées.")


def _fmt(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M:%S UTC")


@app.command("start-missions")
def start_missions(
    scenario: Annotated[
        list[str] | None,
        typer.Option(
            "--scenario",
            "-s",
            help=f"Scénario(s) à lancer (répétable). Défaut : {', '.join(DEFAULT_SCENARIOS)}.",
        ),
    ] = None,
    speed: Annotated[
        float,
        typer.Option(min=0.1, help="Accélération du temps (60 = 1 min simulée par seconde)."),
    ] = 1.0,
    tick_seconds: Annotated[
        int, typer.Option(min=1, help="Intervalle (simulé) entre deux positions GPS.")
    ] = 30,
    routing: Annotated[
        Routing,
        typer.Option(help="Tracé des itinéraires : statique (hors-ligne) ou OSRM (routes réelles)."),
    ] = Routing.static,
    reset_data: Annotated[
        bool,
        typer.Option("--reset", help="Supprime les missions existantes avant de démarrer."),
    ] = False,
    skip_minutes: Annotated[
        float,
        typer.Option(min=0, help="Simule instantanément les N premières minutes."),
    ] = 0.0,
    fast_forward: Annotated[bool, typer.Option(help="Joue toute la simulation instantanément.")] = False,
    seed: Annotated[int | None, typer.Option(help="Graine aléatoire (reproductibilité).")] = None,
    progress_every: Annotated[
        int,
        typer.Option(help="Affiche l'avancement toutes les N minutes simulées (0 = jamais)."),
    ] = 5,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
):
    """Crée les missions de démonstration et les fait vivre jusqu'à leur terme.

    Peut être relancée à volonté : chaque exécution crée de nouvelles missions.
    L'API (`tms serve`) peut tourner en parallèle pour observer les missions en temps réel.
    """
    from tms.simulator.engine import run_simulation

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    keys = scenario or list(DEFAULT_SCENARIOS)

    def on_start(run_id, missions):
        typer.secho(
            f"Simulation {run_id} — vitesse x{speed:g}, position toutes les {tick_seconds}s simulées",
            bold=True,
        )
        for mission_id, reference, s in missions:
            typer.echo(
                f"  {reference}  {mission_id}  [{s.key}]  {s.origin.label} → {s.destination.label}  ({s.vehicle_id}, {s.driver_id})"
            )
        typer.echo("Ctrl+C pour interrompre (les missions non terminées seront annulées).")

    every = max(1, round(progress_every * 60 / tick_seconds)) if progress_every else 0
    counter = {"n": 0}

    def on_tick(at, runners):
        counter["n"] += 1
        if every and counter["n"] % every == 0:
            parts = [
                f"{r.s.key}:{r.phase}"
                + (f" {r.progress_km:.0f}/{r.path.length_km:.0f}km" if r.phase == "transit" else "")
                for r in runners
            ]
            typer.echo(f"[{_fmt(at)}] " + " | ".join(parts))

    def _terminate(*_):
        raise KeyboardInterrupt

    # `kill` est traité comme Ctrl+C : missions annulées et horloge remise à vitesse réelle.
    signal.signal(signal.SIGTERM, _terminate)
    try:
        result = run_simulation(
            keys,
            speed=speed,
            tick_seconds=tick_seconds,
            routing=routing.value,
            reset=reset_data,
            skip_minutes=skip_minutes,
            fast_forward=fast_forward,
            seed=seed,
            on_start=on_start,
            on_tick=on_tick,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except KeyboardInterrupt:
        typer.secho("\nSimulation interrompue : missions non terminées annulées.", fg="yellow")
        raise typer.Exit(130)
    duration = (result.ended_at - result.started_at).total_seconds() / 60
    typer.secho(
        f"Simulation terminée : {len(result.mission_ids)} mission(s), {duration:.0f} min simulées.",
        fg="green",
    )


if __name__ == "__main__":
    app()
