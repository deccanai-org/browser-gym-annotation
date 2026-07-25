from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Platform API configuration (env-driven; see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres — the local dev default matches a stock Homebrew install.
    database_url: str = "postgresql+psycopg://localhost:5432/browser_gym_annotator"

    # CORS — the Vite dev server.
    cors_origins: list[str] = ["http://localhost:5180", "http://127.0.0.1:5180"]

    # Path to the gym repo (imported directly in later milestones for
    # verifiers/oracle); optional until then.
    gym_repo_path: str = ""

    # Live agent re-run (M6b). Key read from the environment — never committed.
    # Empty key → the re-run falls back to the deterministic gold path.
    anthropic_api_key: str = ""
    agent_model: str = "claude-haiku-4-5-20251001"

    # Live gym (M6c). The running ecommerce-browser-gym harness — for verifying
    # against the real world state. In Docker this is host.docker.internal.
    gym_url: str = "http://localhost:8000"
    gym_harness_token: str = ""

    # --- isolated workspaces (v2) -------------------------------------------
    # The gym holds ONE global SESSION per process, so two annotators sharing a
    # gym corrupt each other's world. When enabled, each attempt leases its OWN
    # gym process and `gym_url` becomes a fallback only. Off until gym_repo_path
    # is configured and verified, so the default stays the known-good behaviour.
    workspace_isolation: bool = False
    workspace_runtime: str = "local_process"   # local_process | docker | kubernetes
    # The image a `docker` workspace runs. Build it from the gym repo:
    #   docker build -t browser-gym:local .
    gym_image: str = "browser-gym:local"
    # How the BACKEND reaches a port published on the host. Inside a container
    # localhost is the container itself, so a published workspace port is only
    # reachable through the gateway.
    docker_host_gateway: str = "host.docker.internal"
    workspace_idle_ttl_minutes: int = 75       # INACTIVITY-based; extended by human control or a running job
    workspace_max_per_annotator: int = 2       # a human workspace + one agent branch worker
    gym_image_digest: str = ""                 # environment version stamped onto checkpoints/versions
    # The live browser service (CDP screencast + structured actions). A separate
    # process from the gym on purpose: the gym owns world state, this owns a
    # browser, and neither imports the other.
    live_browser_url: str = "http://localhost:8877"
    # How the BROWSER reaches the gym, when that differs from how this process
    # does. The backend is containerised and uses host.docker.internal; the live
    # browser runs on the host, where that name does not resolve. Empty means the
    # two share a namespace, which is right on a single-host dev box.
    gym_host_for_browser: str = ""
    # How many prefix actions a live open will replay to rebuild a fork's world.
    # Capped because it runs inside the open call: an annotator waiting on a pane
    # is waiting on this, and a pathological trajectory should degrade to "rebuilt
    # 60 of 140" rather than to a minute of nothing. 0 disables the rebuild
    # entirely, which is the operator kill switch.
    live_restore_max_steps: int = 60

    # Rerun cap. 0 = OFF, which is the only safe default until manual capture has
    # passed E2E — capping reruns before an annotator can finish a task by hand
    # would strand them with no way forward.
    agent_run_cap: int = 0

    env: str = "dev"

    # Dev bootstraps the schema with create_all; prod (GCP) sets this false and
    # applies Alembic migrations instead (`alembic upgrade head`).
    auto_create_all: bool = True

    # Minimal auth (login-gated platform). `auth_secret` signs the session token —
    # SET A REAL VALUE via AUTH_SECRET in prod. The token rides in an HttpOnly,
    # SameSite=Lax cookie. auth_enabled=false leaves the platform open (dev only).
    auth_secret: str = "dev-insecure-auth-secret-change-me"
    auth_cookie: str = "bg_auth"
    auth_ttl_hours: int = 168  # 7 days
    auth_enabled: bool = True
    # Open self-registration is OFF: accounts are the 5 seeded dummy annotators
    # (see seed.py). This also closes the account-claim takeover hole. Flip on via
    # ALLOW_REGISTRATION=1 when self-signup is wanted.
    allow_registration: bool = False


settings = Settings()
