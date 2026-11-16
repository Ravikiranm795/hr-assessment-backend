"""Alembic environment.

Migrations run synchronously through psycopg; an ``postgresql+asyncpg`` URL from the app
settings is converted. ``sqlalchemy.url`` set on the config (tests) wins over settings; otherwise
``DATABASE_URL_MIGRATIONS`` (falls back to ``DATABASE_URL``).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from app.db import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        from app.core.config import settings

        url = settings.database_url_migrations or settings.database_url
    return make_url(url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    # Monthly audit partitions are created by SQL functions, not by models.
    return not (type_ == "table" and name.startswith("audit_logs_"))


def run_migrations_offline() -> None:
    context.configure(
        url=_url(), target_metadata=target_metadata, literal_binds=True, include_object=_include_object
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
