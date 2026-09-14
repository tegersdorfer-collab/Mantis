"""Connection settings for trusted, local Python dashboard clients."""
import config


def dashboard_url() -> str:
    host = config.DASHBOARD_HOST
    if ':' in host:
        host = f'[{host}]'
    return f'http://{host}:{config.DASHBOARD_PORT}'


def dashboard_headers() -> dict[str, str]:
    return {'Authorization': f'Bearer {config.DASHBOARD_TOKEN}'}
