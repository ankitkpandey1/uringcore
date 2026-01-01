import os
import asyncio
import uringcore
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'testproj.settings')

asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())

application = get_asgi_application()
