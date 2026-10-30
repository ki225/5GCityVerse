"""Compatible ASGI/CLI entry point. Composition and routes live in api.app."""
import os
from backend.onprem.api.app import create_app

app = create_app()


def main():
    import uvicorn
    uvicorn.run(app, host=os.environ.get('CITYVERSE_HOST', '127.0.0.1'),
                port=int(os.environ.get('CITYVERSE_PORT', '8000')), log_level='info')


if __name__ == '__main__':
    main()
