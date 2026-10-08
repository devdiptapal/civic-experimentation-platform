"""The hosted platform: a web application and API for one workspace.

Start it with ``civicexp serve --workspace PATH``. See
``docs/Product-Guide.md`` for what each page is for and
``docs/Deployment-Guide.md`` for running it for an agency.
"""

from __future__ import annotations

from .app import App, make_server

__all__ = ["App", "make_server", "serve"]


def serve(
    workspace_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    secure_cookies: bool = False,
    trust_proxy: bool = False,
) -> None:
    """Run the application until interrupted."""
    from ..workspace import Workspace

    app = App(Workspace(workspace_path), secure_cookies=secure_cookies, trust_proxy=trust_proxy)
    server = make_server(app, host, port)
    print(f"Civic Experimentation Platform {app.version}")
    print(f"Workspace: {app.workspace.name} ({workspace_path})")
    print(f"Serving on http://{host}:{server.server_address[1]}  (Ctrl+C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1") and not secure_cookies:
        print(
            "warning: listening beyond this machine without --secure-cookies. Put the "
            "platform behind a TLS-terminating proxy and pass --secure-cookies."
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
