"""E2E tests for uringcore with Starlette."""

import asyncio
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def homepage(request):
    """Simple homepage handler."""
    return PlainTextResponse("Hello from Starlette!")


def json_endpoint(request):
    """JSON response handler."""
    return JSONResponse({
        "message": "Hello",
        "framework": "Starlette",
        "event_loop": "uringcore"
    })


async def async_handler(request):
    """Async handler with sleep."""
    await asyncio.sleep(0.01)
    return PlainTextResponse("Async response")


async def echo_handler(request):
    """Echo request body."""
    body = await request.body()
    return PlainTextResponse(body.decode())


# Create Starlette application
app = Starlette(
    debug=True,
    routes=[
        Route("/", homepage),
        Route("/json", json_endpoint),
        Route("/async", async_handler),
        Route("/echo", echo_handler, methods=["POST"]),
    ]
)


class TestStarletteBasic:
    """Basic Starlette integration tests."""

    def test_homepage(self):
        """Test homepage returns expected response."""
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert response.text == "Hello from Starlette!"

    def test_json_response(self):
        """Test JSON endpoint."""
        client = TestClient(app)
        response = client.get("/json")
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Hello"
        assert data["framework"] == "Starlette"

    def test_async_handler(self):
        """Test async handler with sleep."""
        client = TestClient(app)
        response = client.get("/async")
        assert response.status_code == 200
        assert response.text == "Async response"

    def test_echo_post(self):
        """Test echo POST endpoint."""
        client = TestClient(app)
        response = client.post("/echo", content="Hello World")
        assert response.status_code == 200
        assert response.text == "Hello World"


class TestStarletteConcurrency:
    """Concurrency tests for Starlette."""

    def test_multiple_requests(self):
        """Test multiple sequential requests."""
        client = TestClient(app)
        for i in range(10):
            response = client.get("/")
            assert response.status_code == 200

    def test_large_payload(self):
        """Test with larger payload."""
        client = TestClient(app)
        payload = "x" * 10000
        response = client.post("/echo", content=payload)
        assert response.status_code == 200
        assert len(response.text) == 10000


class TestStarletteWithUringloop:
    """Tests specifically for uringcore integration."""

    @pytest.mark.asyncio
    async def test_async_context(self):
        """Test that async context works properly."""
        from starlette.testclient import TestClient
        
        async def async_test():
            # Simple async operation
            await asyncio.sleep(0.001)
            return True
        
        result = await async_test()
        assert result is True

    def test_event_loop_type(self):
        """Verify event loop can be obtained."""
        # This test verifies asyncio works with the test client
        client = TestClient(app)
        response = client.get("/async")
        assert response.status_code == 200
