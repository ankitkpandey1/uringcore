"""E2E tests for uringcore with FastAPI."""

import asyncio
import pytest
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel


# Create FastAPI application
app = FastAPI(title="uringcore E2E Test", version="1.0.0")


class EchoRequest(BaseModel):
    """Echo request model."""
    message: str
    count: int = 1


class EchoResponse(BaseModel):
    """Echo response model."""
    message: str
    repeated: list[str]


@app.get("/")
def read_root():
    """Root endpoint."""
    return {"message": "Hello from FastAPI!", "event_loop": "uringcore"}


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/async")
async def async_endpoint():
    """Async endpoint with sleep."""
    await asyncio.sleep(0.01)
    return {"async": True, "message": "Completed async operation"}


@app.post("/echo", response_model=EchoResponse)
async def echo(request: EchoRequest):
    """Echo endpoint with validation."""
    return EchoResponse(
        message=request.message,
        repeated=[request.message] * request.count
    )


@app.post("/echo/raw")
async def echo_raw(request: Request):
    """Raw echo endpoint."""
    body = await request.body()
    return PlainTextResponse(body.decode())


@app.get("/items/{item_id}")
async def read_item(item_id: int, q: str = None):
    """Read item with path and query parameters."""
    result = {"item_id": item_id}
    if q:
        result["query"] = q
    return result


@app.get("/error")
async def error_endpoint():
    """Endpoint that raises an error."""
    raise HTTPException(status_code=500, detail="Intentional error")


class TestFastAPIBasic:
    """Basic FastAPI integration tests."""

    def test_root(self):
        """Test root endpoint."""
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Hello from FastAPI!"

    def test_health(self):
        """Test health check endpoint."""
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_async_endpoint(self):
        """Test async endpoint."""
        client = TestClient(app)
        response = client.get("/async")
        assert response.status_code == 200
        assert response.json()["async"] is True

    def test_path_parameter(self):
        """Test path parameters."""
        client = TestClient(app)
        response = client.get("/items/42")
        assert response.status_code == 200
        assert response.json()["item_id"] == 42

    def test_query_parameter(self):
        """Test query parameters."""
        client = TestClient(app)
        response = client.get("/items/42?q=test")
        assert response.status_code == 200
        data = response.json()
        assert data["item_id"] == 42
        assert data["query"] == "test"


class TestFastAPIEcho:
    """Echo endpoint tests."""

    def test_echo_json(self):
        """Test JSON echo endpoint."""
        client = TestClient(app)
        response = client.post("/echo", json={"message": "Hello", "count": 3})
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Hello"
        assert data["repeated"] == ["Hello", "Hello", "Hello"]

    def test_echo_raw(self):
        """Test raw echo endpoint."""
        client = TestClient(app)
        response = client.post("/echo/raw", content="Raw content")
        assert response.status_code == 200
        assert response.text == "Raw content"

    def test_echo_validation_error(self):
        """Test validation error handling."""
        client = TestClient(app)
        response = client.post("/echo", json={"invalid": "data"})
        assert response.status_code == 422  # Validation error


class TestFastAPIErrors:
    """Error handling tests."""

    def test_not_found(self):
        """Test 404 response."""
        client = TestClient(app)
        response = client.get("/nonexistent")
        assert response.status_code == 404

    def test_internal_error(self):
        """Test 500 response."""
        client = TestClient(app)
        response = client.get("/error")
        assert response.status_code == 500
        assert response.json()["detail"] == "Intentional error"


class TestFastAPIConcurrency:
    """Concurrency tests for FastAPI."""

    def test_multiple_requests(self):
        """Test multiple sequential requests."""
        client = TestClient(app)
        for i in range(20):
            response = client.get("/health")
            assert response.status_code == 200

    def test_large_payload(self):
        """Test with larger payload."""
        client = TestClient(app)
        payload = "x" * 50000
        response = client.post("/echo/raw", content=payload)
        assert response.status_code == 200
        assert len(response.text) == 50000

    @pytest.mark.asyncio
    async def test_concurrent_async_tasks(self):
        """Test concurrent async operations."""
        async def make_request():
            await asyncio.sleep(0.001)
            return True
        
        tasks = [make_request() for _ in range(10)]
        results = await asyncio.gather(*tasks)
        assert all(results)


class TestFastAPIWithUringloop:
    """Tests specifically for uringcore integration."""

    def test_openapi_schema(self):
        """Test OpenAPI schema generation."""
        client = TestClient(app)
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"] == "uringcore E2E Test"

    def test_docs_available(self):
        """Test docs endpoint."""
        client = TestClient(app)
        response = client.get("/docs")
        assert response.status_code == 200
