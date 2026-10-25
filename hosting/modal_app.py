"""Single-user model builder; Vercel serves the UI and authenticates visitors.

Deploy using: uvx --python 3.12 --with modal modal deploy hosting/modal_app.py
The generated Dockerfile.cloud pins the toolkit revision. Never include /data
or /config in the image. They live in independent persistent Modal volumes.
"""
from pathlib import Path
import modal

ROOT = Path(__file__).resolve().parents[1]
app = modal.App('lego-nova')
image = modal.Image.from_dockerfile(ROOT / 'hosting' / 'Dockerfile.cloud', context_dir=ROOT)
# Modal volumes require an empty mount point; startup creates these folders.
image = image.run_commands('rmdir /data/generated /data/chats /data/output',
    'uv pip install --system --break-system-packages --python /usr/bin/python3 psycopg[binary]==3.3.6',
    'uv pip install --python /opt/ldraw-nova/.venv/bin/python pypdf==6.12.0')
# Mount source separately: Python edits do not rebuild the CAD toolchain.
# The companion toolkit checkout is the same one used by local Docker builds.
image = image.add_local_dir(ROOT / 'web' / 'backend', '/app/web/backend',
                            ignore=['**/__pycache__/**', 'tests/**'])
image = image.add_local_dir(ROOT / 'chrome-helper','/opt/chrome-helper',ignore=['tests/**','**/__pycache__/**'])
image = image.add_local_dir(ROOT.parent / 'ldraw-nova' / 'ldraw_tools',
                            '/opt/ldraw-nova/ldraw_tools', ignore=['**/__pycache__/**'])
data = modal.Volume.from_name('lego-nova-data', create_if_missing=True)
config = modal.Volume.from_name('lego-nova-config', create_if_missing=True)
secret = modal.Secret.from_name('lego-nova-gateway')
pricing_secret = modal.Secret.from_name('lego-nova-purchasing')


@app.function(image=image, cpu=1, memory=2048, max_containers=1,
              scaledown_window=3600, timeout=3600,
              volumes={'/data': data, '/config': config}, secrets=[secret, pricing_secret])
@modal.concurrent(max_inputs=64)
@modal.asgi_app()
def server():
    import asyncio
    from contextlib import asynccontextmanager, suppress
    import hmac
    import logging
    import os
    import subprocess
    import sys

    # Reuse the image's installed app dependencies and untouched AI transport.
    sys.path[:0] = ['/app/web/backend', '/app', '/usr/local/lib/python3.12/dist-packages']
    os.chmod('/config', 0o700)
    from main import app as web, lifespan as original_lifespan
    from starlette.responses import JSONResponse

    worker_key = os.environ['LEGO_WORKER_KEY']

    @web.middleware('http')
    async def private_worker(request, call_next):
        if not hmac.compare_digest(request.headers.get('x-lego-worker-key', ''), worker_key):
            return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
        return await call_next(request)

    async def checkpoint():
        while True:
            await asyncio.sleep(20)
            try:
                await asyncio.gather(data.commit.aio(), config.commit.aio())
            except Exception:
                logging.exception('Could not save the latest model data')

    @asynccontextmanager
    async def lifespan(instance):
        display = subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1920x1080x24'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            ready = await asyncio.to_thread(subprocess.run, ['xdpyinfo', '-display', ':99'],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            await asyncio.sleep(0.1)
        else:
            display.terminate()
            raise RuntimeError('The model renderer could not start')
        saver = asyncio.create_task(checkpoint())
        try:
            async with original_lifespan(instance):
                yield
        finally:
            saver.cancel()
            with suppress(asyncio.CancelledError):
                await saver
            await asyncio.gather(data.commit.aio(), config.commit.aio())
            display.terminate()

    web.router.lifespan_context = lifespan
    return web


# Catalogue work does not need the CAD image or access to model/chat credentials.
catalogue_volume = modal.Volume.from_name('lego-nova-catalogue', create_if_missing=True)
catalogue_image = (modal.Image.debian_slim(python_version='3.12')
    .env({'UV_DEFAULT_INDEX':'https://pypi.org/simple','UV_INDEX_URL':'https://pypi.org/simple'})
    .pip_install('psycopg[binary]==3.3.6')
    .add_local_dir(ROOT/'web/backend/purchasing','/app/purchasing',ignore=['**/__pycache__/**']))

@app.function(image=catalogue_image, cpu=0.5, memory=1024, timeout=1800, max_containers=1,
              schedule=modal.Cron('0 6 * * *'), secrets=[pricing_secret],
              volumes={'/catalogue':catalogue_volume}, retries=0)
def refresh_catalogue():
    import sys
    sys.path.insert(0,'/app')
    from purchasing.sync import run
    try:
        return run(Path('/catalogue/rebrickable'))
    finally:
        catalogue_volume.commit()
