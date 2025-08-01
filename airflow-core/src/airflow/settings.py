#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import atexit
import functools
import json
import logging
import os
import sys
import warnings
from collections.abc import Callable
from importlib import metadata
from typing import TYPE_CHECKING, Any

import pluggy
from packaging.version import Version
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession as SAAsyncSession, create_async_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from airflow import __version__ as airflow_version, policies
from airflow.configuration import AIRFLOW_HOME, conf
from airflow.exceptions import AirflowInternalRuntimeError
from airflow.logging_config import configure_logging
from airflow.utils.orm_event_handlers import setup_event_handlers
from airflow.utils.sqlalchemy import is_sqlalchemy_v1
from airflow.utils.timezone import local_timezone, parse_timezone, utc

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from airflow.api_fastapi.common.types import UIAlert

log = logging.getLogger(__name__)

try:
    if (tz := conf.get_mandatory_value("core", "default_timezone")) != "system":
        TIMEZONE = parse_timezone(tz)
    else:
        TIMEZONE = local_timezone()
except Exception:
    TIMEZONE = utc

log.info("Configured default timezone %s", TIMEZONE)

if conf.has_option("database", "sql_alchemy_session_maker"):
    log.info(
        '[Warning] Found config "sql_alchemy_session_maker", make sure you know what you are doing.\n'
        "[Warning] Improper configuration of sql_alchemy_session_maker can lead to serious issues, "
        "including data corruption, unrecoverable application crashes.\n"
        "[Warning] Please review the SQLAlchemy documentation for detailed guidance on "
        "proper configuration and best practices."
    )

HEADER = "\n".join(
    [
        r"  ____________       _____________",
        r" ____    |__( )_________  __/__  /________      __",
        r"____  /| |_  /__  ___/_  /_ __  /_  __ \_ | /| / /",
        r"___  ___ |  / _  /   _  __/ _  / / /_/ /_ |/ |/ /",
        r" _/_/  |_/_/  /_/    /_/    /_/  \____/____/|__/",
    ]
)

LOGGING_LEVEL = logging.INFO

# the prefix to append to gunicorn worker processes after init
GUNICORN_WORKER_READY_PREFIX = "[ready] "

LOG_FORMAT = conf.get("logging", "log_format")
SIMPLE_LOG_FORMAT = conf.get("logging", "simple_log_format")

SQL_ALCHEMY_CONN: str | None = None
PLUGINS_FOLDER: str | None = None
LOGGING_CLASS_PATH: str | None = None
DONOT_MODIFY_HANDLERS: bool | None = None
DAGS_FOLDER: str = os.path.expanduser(conf.get_mandatory_value("core", "DAGS_FOLDER"))

AIO_LIBS_MAPPING = {"sqlite": "aiosqlite", "postgresql": "asyncpg", "mysql": "aiomysql"}
"""
Mapping of sync scheme to async scheme.

:meta private:
"""

engine: Engine
Session: scoped_session
# NonScopedSession creates global sessions and is not safe to use in multi-threaded environment without
# additional precautions. The only use case is when the session lifecycle needs
# custom handling. Most of the time we only want one unique thread local session object,
# this is achieved by the Session factory above.
NonScopedSession: sessionmaker
async_engine: AsyncEngine
AsyncSession: Callable[..., SAAsyncSession]

# The JSON library to use for DAG Serialization and De-Serialization
json = json

# Display alerts on the dashboard
# Useful for warning about setup issues or announcing changes to end users
# List of UIAlerts, which allows for specifying the content and category
# message to be shown. For example:
#   from airflow.api_fastapi.common.types import UIAlert
#
#   DASHBOARD_UIALERTS = [
#       UIAlert(text="Welcome to Airflow", category="info"),
#       UIAlert(text="Upgrade tomorrow [help](https://www.example.com)", category="warning"), #With markdown support
#   ]
#
DASHBOARD_UIALERTS: list[UIAlert] = []


@functools.cache
def _get_rich_console(file):
    # Delay imports until we need it
    import rich.console

    return rich.console.Console(file=file)


def custom_show_warning(message, category, filename, lineno, file=None, line=None):
    """Print rich and visible warnings."""
    # Delay imports until we need it
    import re

    from rich.markup import escape

    re_escape_regex = re.compile(r"(\\*)(\[[a-z#/@][^[]*?])").sub
    msg = f"[bold]{line}" if line else f"[bold][yellow]{filename}:{lineno}"
    msg += f" {category.__name__}[/bold]: {escape(str(message), _escape=re_escape_regex)}[/yellow]"
    write_console = _get_rich_console(file or sys.stderr)
    write_console.print(msg, soft_wrap=True)


def replace_showwarning(replacement):
    """
    Replace ``warnings.showwarning``, returning the original.

    This is useful since we want to "reset" the ``showwarning`` hook on exit to
    avoid lazy-loading issues. If a warning is emitted after Python cleaned up
    the import system, we would no longer be able to import ``rich``.
    """
    original = warnings.showwarning
    warnings.showwarning = replacement
    return original


original_show_warning = replace_showwarning(custom_show_warning)
atexit.register(functools.partial(replace_showwarning, original_show_warning))

POLICY_PLUGIN_MANAGER: Any = None


def task_policy(task):
    return POLICY_PLUGIN_MANAGER.hook.task_policy(task=task)


def dag_policy(dag):
    return POLICY_PLUGIN_MANAGER.hook.dag_policy(dag=dag)


def task_instance_mutation_hook(task_instance):
    return POLICY_PLUGIN_MANAGER.hook.task_instance_mutation_hook(task_instance=task_instance)


task_instance_mutation_hook.is_noop = True  # type: ignore


def pod_mutation_hook(pod):
    return POLICY_PLUGIN_MANAGER.hook.pod_mutation_hook(pod=pod)


def get_airflow_context_vars(context):
    return POLICY_PLUGIN_MANAGER.hook.get_airflow_context_vars(context=context)


def get_dagbag_import_timeout(dag_file_path: str):
    return POLICY_PLUGIN_MANAGER.hook.get_dagbag_import_timeout(dag_file_path=dag_file_path)


def configure_policy_plugin_manager():
    global POLICY_PLUGIN_MANAGER

    POLICY_PLUGIN_MANAGER = pluggy.PluginManager(policies.local_settings_hookspec.project_name)
    POLICY_PLUGIN_MANAGER.add_hookspecs(policies)
    POLICY_PLUGIN_MANAGER.register(policies.DefaultPolicy)


def load_policy_plugins(pm: pluggy.PluginManager):
    # We can't log duration etc  here, as logging hasn't yet been configured!
    pm.load_setuptools_entrypoints("airflow.policy")


def _get_async_conn_uri_from_sync(sync_uri):
    scheme, rest = sync_uri.split(":", maxsplit=1)
    scheme = scheme.split("+", maxsplit=1)[0]
    aiolib = AIO_LIBS_MAPPING.get(scheme)
    if aiolib:
        return f"{scheme}+{aiolib}:{rest}"
    return sync_uri


def configure_vars():
    """Configure Global Variables from airflow.cfg."""
    global SQL_ALCHEMY_CONN
    global SQL_ALCHEMY_CONN_ASYNC
    global DAGS_FOLDER
    global PLUGINS_FOLDER
    global DONOT_MODIFY_HANDLERS
    SQL_ALCHEMY_CONN = conf.get("database", "SQL_ALCHEMY_CONN")
    SQL_ALCHEMY_CONN_ASYNC = _get_async_conn_uri_from_sync(sync_uri=SQL_ALCHEMY_CONN)

    DAGS_FOLDER = os.path.expanduser(conf.get("core", "DAGS_FOLDER"))

    PLUGINS_FOLDER = conf.get("core", "plugins_folder", fallback=os.path.join(AIRFLOW_HOME, "plugins"))

    # If donot_modify_handlers=True, we do not modify logging handlers in task_run command
    # If the flag is set to False, we remove all handlers from the root logger
    # and add all handlers from 'airflow.task' logger to the root Logger. This is done
    # to get all the logs from the print & log statements in the DAG files before a task is run
    # The handlers are restored after the task completes execution.
    DONOT_MODIFY_HANDLERS = conf.getboolean("logging", "donot_modify_handlers", fallback=False)


def _run_openlineage_runtime_check():
    """
    Ensure compatibility of OpenLineage provider package and Airflow version.

    Airflow 2.10.0 introduced some core changes (#39336) that made versions <= 1.8.0 of OpenLineage
    provider incompatible with future Airflow versions (>= 2.10.0).
    """
    ol_package = "apache-airflow-providers-openlineage"
    try:
        ol_version = metadata.version(ol_package)
    except metadata.PackageNotFoundError:
        return

    if ol_version and Version(ol_version) < Version("1.8.0.dev0"):
        raise RuntimeError(
            f"You have installed `{ol_package}` == `{ol_version}` that is not compatible with "
            f"`apache-airflow` == `{airflow_version}`. "
            f"For `apache-airflow` >= `2.10.0` you must use `{ol_package}` >= `1.8.0`."
        )


def run_providers_custom_runtime_checks():
    _run_openlineage_runtime_check()


class SkipDBTestsSession:
    """
    This fake session is used to skip DB tests when `_AIRFLOW_SKIP_DB_TESTS` is set.

    :meta private:
    """

    def __init__(self):
        raise AirflowInternalRuntimeError(
            "Your test accessed the DB but `_AIRFLOW_SKIP_DB_TESTS` is set.\n"
            "Either make sure your test does not use database or mark the test with `@pytest.mark.db_test`\n"
            "See https://github.com/apache/airflow/blob/main/contributing-docs/testing/unit_tests.rst#"
            "best-practices-for-db-tests on how "
            "to deal with it and consult examples."
        )

    def remove(*args, **kwargs):
        pass

    def get_bind(
        self,
        mapper=None,
        clause=None,
        bind=None,
        _sa_skip_events=None,
        _sa_skip_for_implicit_returning=False,
    ):
        pass


AIRFLOW_PATH = os.path.dirname(os.path.dirname(__file__))
AIRFLOW_TESTS_PATH = os.path.join(AIRFLOW_PATH, "tests")
AIRFLOW_SETTINGS_PATH = os.path.join(AIRFLOW_PATH, "airflow", "settings.py")
AIRFLOW_UTILS_SESSION_PATH = os.path.join(AIRFLOW_PATH, "airflow", "utils", "session.py")
AIRFLOW_MODELS_BASEOPERATOR_PATH = os.path.join(AIRFLOW_PATH, "airflow", "models", "baseoperator.py")


def _is_sqlite_db_path_relative(sqla_conn_str: str) -> bool:
    """Determine whether the database connection URI specifies a relative path."""
    # Check for non-empty connection string:
    if not sqla_conn_str:
        return False
    # Check for the right URI scheme:
    if not sqla_conn_str.startswith("sqlite"):
        return False
    # In-memory is not useful for production, but useful for writing tests against Airflow for extensions
    if sqla_conn_str == "sqlite://":
        return False
    # Check for absolute path:
    if sqla_conn_str.startswith(abs_prefix := "sqlite:///") and os.path.isabs(
        sqla_conn_str[len(abs_prefix) :]
    ):
        return False
    return True


def _configure_async_session():
    global async_engine
    global AsyncSession

    async_engine = create_async_engine(SQL_ALCHEMY_CONN_ASYNC, future=True)
    AsyncSession = sessionmaker(
        bind=async_engine,
        autocommit=False,
        autoflush=False,
        class_=SAAsyncSession,
        expire_on_commit=False,
    )


def configure_orm(disable_connection_pool=False, pool_class=None):
    """Configure ORM using SQLAlchemy."""
    from airflow.sdk.execution_time.secrets_masker import mask_secret

    if _is_sqlite_db_path_relative(SQL_ALCHEMY_CONN):
        from airflow.exceptions import AirflowConfigException

        raise AirflowConfigException(
            f"Cannot use relative path: `{SQL_ALCHEMY_CONN}` to connect to sqlite. "
            "Please use absolute path such as `sqlite:////tmp/airflow.db`."
        )

    global Session
    global engine
    global NonScopedSession

    if os.environ.get("_AIRFLOW_SKIP_DB_TESTS") == "true":
        # Skip DB initialization in unit tests, if DB tests are skipped
        Session = SkipDBTestsSession
        engine = None
        return
    log.debug("Setting up DB connection pool (PID %s)", os.getpid())
    engine_args = prepare_engine_args(disable_connection_pool, pool_class)

    if conf.has_option("database", "sql_alchemy_connect_args"):
        connect_args = conf.getimport("database", "sql_alchemy_connect_args")
    else:
        connect_args = {}

    if SQL_ALCHEMY_CONN.startswith("sqlite"):
        # FastAPI runs sync endpoints in a separate thread. SQLite does not allow
        # to use objects created in another threads by default. Allowing that in test
        # to so the `test` thread and the tested endpoints can use common objects.
        connect_args["check_same_thread"] = False

    engine = create_engine(SQL_ALCHEMY_CONN, connect_args=connect_args, **engine_args, future=True)
    mask_secret(engine.url.password)
    setup_event_handlers(engine)

    if conf.has_option("database", "sql_alchemy_session_maker"):
        _session_maker = conf.getimport("database", "sql_alchemy_session_maker")
    else:
        _session_maker = functools.partial(
            sessionmaker,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    NonScopedSession = _session_maker(engine)
    Session = scoped_session(NonScopedSession)

    _configure_async_session()

    if register_at_fork := getattr(os, "register_at_fork", None):
        # https://docs.sqlalchemy.org/en/20/core/pooling.html#using-connection-pools-with-multiprocessing-or-os-fork
        def clean_in_fork():
            _globals = globals()
            if engine := _globals.get("engine"):
                engine.dispose(close=False)
            if async_engine := _globals.get("async_engine"):
                async_engine.sync_engine.dispose(close=False)

        # Won't work on Windows
        register_at_fork(after_in_child=clean_in_fork)


DEFAULT_ENGINE_ARGS = {
    "postgresql": {
        "executemany_mode": "values_plus_batch",
        "executemany_values_page_size" if is_sqlalchemy_v1() else "insertmanyvalues_page_size": 10000,
        "executemany_batch_page_size": 2000,
    },
}


def prepare_engine_args(disable_connection_pool=False, pool_class=None):
    """Prepare SQLAlchemy engine args."""
    default_args = {}
    for dialect, default in DEFAULT_ENGINE_ARGS.items():
        if SQL_ALCHEMY_CONN.startswith(dialect):
            default_args = default.copy()
            break

    engine_args: dict = conf.getjson("database", "sql_alchemy_engine_args", fallback=default_args)

    if pool_class:
        # Don't use separate settings for size etc, only those from sql_alchemy_engine_args
        engine_args["poolclass"] = pool_class
    elif disable_connection_pool or not conf.getboolean("database", "SQL_ALCHEMY_POOL_ENABLED"):
        engine_args["poolclass"] = NullPool
        log.debug("settings.prepare_engine_args(): Using NullPool")
    elif not SQL_ALCHEMY_CONN.startswith("sqlite"):
        # Pool size engine args not supported by sqlite.
        # If no config value is defined for the pool size, select a reasonable value.
        # 0 means no limit, which could lead to exceeding the Database connection limit.
        pool_size = conf.getint("database", "SQL_ALCHEMY_POOL_SIZE", fallback=5)

        # The maximum overflow size of the pool.
        # When the number of checked-out connections reaches the size set in pool_size,
        # additional connections will be returned up to this limit.
        # When those additional connections are returned to the pool, they are disconnected and discarded.
        # It follows then that the total number of simultaneous connections
        # the pool will allow is pool_size + max_overflow,
        # and the total number of "sleeping" connections the pool will allow is pool_size.
        # max_overflow can be set to -1 to indicate no overflow limit;
        # no limit will be placed on the total number
        # of concurrent connections. Defaults to 10.
        max_overflow = conf.getint("database", "SQL_ALCHEMY_MAX_OVERFLOW", fallback=10)

        # The DB server already has a value for wait_timeout (number of seconds after
        # which an idle sleeping connection should be killed). Since other DBs may
        # co-exist on the same server, SQLAlchemy should set its
        # pool_recycle to an equal or smaller value.
        pool_recycle = conf.getint("database", "SQL_ALCHEMY_POOL_RECYCLE", fallback=1800)

        # Check connection at the start of each connection pool checkout.
        # Typically, this is a simple statement like "SELECT 1", but may also make use
        # of some DBAPI-specific method to test the connection for liveness.
        # More information here:
        # https://docs.sqlalchemy.org/en/14/core/pooling.html#disconnect-handling-pessimistic
        pool_pre_ping = conf.getboolean("database", "SQL_ALCHEMY_POOL_PRE_PING", fallback=True)

        log.debug(
            "settings.prepare_engine_args(): Using pool settings. pool_size=%d, max_overflow=%d, "
            "pool_recycle=%d, pid=%d",
            pool_size,
            max_overflow,
            pool_recycle,
            os.getpid(),
        )
        engine_args["pool_size"] = pool_size
        engine_args["pool_recycle"] = pool_recycle
        engine_args["pool_pre_ping"] = pool_pre_ping
        engine_args["max_overflow"] = max_overflow

    # The default isolation level for MySQL (REPEATABLE READ) can introduce inconsistencies when
    # running multiple schedulers, as repeated queries on the same session may read from stale snapshots.
    # 'READ COMMITTED' is the default value for PostgreSQL.
    # More information here:
    # https://dev.mysql.com/doc/refman/8.0/en/innodb-transaction-isolation-levels.html"

    if SQL_ALCHEMY_CONN.startswith("mysql"):
        engine_args["isolation_level"] = "READ COMMITTED"

    if is_sqlalchemy_v1():
        # Allow the user to specify an encoding for their DB otherwise default
        # to utf-8 so jobs & users with non-latin1 characters can still use us.
        # This parameter was removed in SQLAlchemy 2.x.
        engine_args["encoding"] = conf.get("database", "SQL_ENGINE_ENCODING", fallback="utf-8")

    return engine_args


def dispose_orm(do_log: bool = True):
    """Properly close pooled database connections."""
    global Session, engine, NonScopedSession

    _globals = globals()
    if _globals.get("engine") is None and _globals.get("Session") is None:
        return

    if do_log:
        log.debug("Disposing DB connection pool (PID %s)", os.getpid())

    if "Session" in _globals and Session is not None:
        from sqlalchemy.orm.session import close_all_sessions

        Session.remove()
        Session = None
        NonScopedSession = None
        close_all_sessions()

    if "engine" in _globals and engine is not None:
        engine.dispose()
        engine = None


def reconfigure_orm(disable_connection_pool=False, pool_class=None):
    """Properly close database connections and re-configure ORM."""
    dispose_orm()
    configure_orm(disable_connection_pool=disable_connection_pool, pool_class=pool_class)


def configure_adapters():
    """Register Adapters and DB Converters."""
    from pendulum import DateTime as Pendulum

    if SQL_ALCHEMY_CONN.startswith("sqlite"):
        from sqlite3 import register_adapter

        register_adapter(Pendulum, lambda val: val.isoformat(" "))

    if SQL_ALCHEMY_CONN.startswith("mysql"):
        try:
            try:
                import MySQLdb.converters
            except ImportError:
                raise RuntimeError(
                    "You do not have `mysqlclient` package installed. "
                    "Please install it with `pip install mysqlclient` and make sure you have system "
                    "mysql libraries installed, as well as well as `pkg-config` system package "
                    "installed in case you see compilation error during installation."
                )

            MySQLdb.converters.conversions[Pendulum] = MySQLdb.converters.DateTime2literal
        except ImportError:
            pass
        try:
            import pymysql.converters

            pymysql.converters.conversions[Pendulum] = pymysql.converters.escape_datetime
        except ImportError:
            pass


def configure_action_logging() -> None:
    """Any additional configuration (register callback) for airflow.utils.action_loggers module."""


def prepare_syspath_for_config_and_plugins():
    """Update sys.path for the config and plugins directories."""
    # Add ./config/ for loading custom log parsers etc, or
    # airflow_local_settings etc.
    config_path = os.path.join(AIRFLOW_HOME, "config")
    if config_path not in sys.path:
        sys.path.append(config_path)

    if PLUGINS_FOLDER not in sys.path:
        sys.path.append(PLUGINS_FOLDER)


def import_local_settings():
    """Import airflow_local_settings.py files to allow overriding any configs in settings.py file."""
    try:
        import airflow_local_settings
    except ModuleNotFoundError as e:
        if e.name == "airflow_local_settings":
            log.debug("No airflow_local_settings to import.", exc_info=True)
        else:
            log.critical(
                "Failed to import airflow_local_settings due to a transitive module not found error.",
                exc_info=True,
            )
            raise
    except ImportError:
        log.critical("Failed to import airflow_local_settings.", exc_info=True)
        raise
    else:
        if hasattr(airflow_local_settings, "__all__"):
            names = set(airflow_local_settings.__all__)
        else:
            names = {n for n in airflow_local_settings.__dict__ if not n.startswith("__")}

        plugin_functions = policies.make_plugin_from_local_settings(
            POLICY_PLUGIN_MANAGER, airflow_local_settings, names
        )

        # If we have already handled a function by adding it to the plugin,
        # then don't clobber the global function
        for name in names - plugin_functions:
            globals()[name] = getattr(airflow_local_settings, name)

        if POLICY_PLUGIN_MANAGER.hook.task_instance_mutation_hook.get_hookimpls():
            task_instance_mutation_hook.is_noop = False

        log.info("Loaded airflow_local_settings from %s .", airflow_local_settings.__file__)


def initialize():
    """Initialize Airflow with all the settings from this file."""
    configure_vars()
    prepare_syspath_for_config_and_plugins()
    configure_policy_plugin_manager()
    # Load policy plugins _before_ importing airflow_local_settings, as Pluggy uses LIFO and we want anything
    # in airflow_local_settings to take precendec
    load_policy_plugins(POLICY_PLUGIN_MANAGER)
    import_local_settings()
    global LOGGING_CLASS_PATH
    LOGGING_CLASS_PATH = configure_logging()

    configure_adapters()
    # The webservers import this file from models.py with the default settings.

    if not os.environ.get("PYTHON_OPERATORS_VIRTUAL_ENV_MODE", None):
        is_worker = os.environ.get("_AIRFLOW__REEXECUTED_PROCESS") == "1"
        if not is_worker:
            configure_orm()
    configure_action_logging()

    # mask the sensitive_config_values
    conf.mask_secrets()

    # Run any custom runtime checks that needs to be executed for providers
    run_providers_custom_runtime_checks()

    # Ensure we close DB connections at scheduler and gunicorn worker terminations
    atexit.register(dispose_orm)


# Const stuff

KILOBYTE = 1024
MEGABYTE = KILOBYTE * KILOBYTE
WEB_COLORS = {"LIGHTBLUE": "#4d9de0", "LIGHTORANGE": "#FF9933"}

# Updating serialized DAG can not be faster than a minimum interval to reduce database
# write rate.
MIN_SERIALIZED_DAG_UPDATE_INTERVAL = conf.getint("core", "min_serialized_dag_update_interval", fallback=30)

# If set to True, serialized DAGs is compressed before writing to DB,
COMPRESS_SERIALIZED_DAGS = conf.getboolean("core", "compress_serialized_dags", fallback=False)

# Fetching serialized DAG can not be faster than a minimum interval to reduce database
# read rate. This config controls when your DAGs are updated in the Webserver
MIN_SERIALIZED_DAG_FETCH_INTERVAL = conf.getint("core", "min_serialized_dag_fetch_interval", fallback=10)

CAN_FORK = hasattr(os, "fork")

EXECUTE_TASKS_NEW_PYTHON_INTERPRETER = not CAN_FORK or conf.getboolean(
    "core",
    "execute_tasks_new_python_interpreter",
    fallback=False,
)

USE_JOB_SCHEDULE = conf.getboolean("scheduler", "use_job_schedule", fallback=True)

# By default Airflow plugins are lazily-loaded (only loaded when required). Set it to False,
# if you want to load plugins whenever 'airflow' is invoked via cli or loaded from module.
LAZY_LOAD_PLUGINS: bool = conf.getboolean("core", "lazy_load_plugins", fallback=True)

# By default Airflow providers are lazily-discovered (discovery and imports happen only when required).
# Set it to False, if you want to discover providers whenever 'airflow' is invoked via cli or
# loaded from module.
LAZY_LOAD_PROVIDERS: bool = conf.getboolean("core", "lazy_discover_providers", fallback=True)

# Executors can set this to true to configure logging correctly for
# containerized executors.
IS_EXECUTOR_CONTAINER = bool(os.environ.get("AIRFLOW_IS_EXECUTOR_CONTAINER", ""))
IS_K8S_EXECUTOR_POD = bool(os.environ.get("AIRFLOW_IS_K8S_EXECUTOR_POD", ""))
"""Will be True if running in kubernetes executor pod."""

HIDE_SENSITIVE_VAR_CONN_FIELDS = conf.getboolean("core", "hide_sensitive_var_conn_fields")

# By default this is off, but is automatically configured on when running task
# instances
MASK_SECRETS_IN_LOGS = False

# Prefix used to identify tables holding data moved during migration.
AIRFLOW_MOVED_TABLE_PREFIX = "_airflow_moved"

DAEMON_UMASK: str = conf.get("core", "daemon_umask", fallback="0o077")
