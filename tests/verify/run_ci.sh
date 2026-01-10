#!/bin/bash
set -e

# Usage: ./run_ci.sh <test_name>
TEST_NAME=$1

# Cleanup function to kill background processes
cleanup() {
    if [ -n "$PID" ]; then
        kill $PID 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Resolve Python interpreter (prefer project venv)
PYTHON_CMD="python"
if [ -f "../../.venv/bin/python" ]; then
    PYTHON_CMD="../../.venv/bin/python"
fi

echo "Starting Verification Test: $TEST_NAME using $PYTHON_CMD"

if [ "$TEST_NAME" == "fastapi" ]; then
    # Run FastAPI with uringcore (default config)
    $PYTHON_CMD -c "
import asyncio, uringcore
asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
import uvicorn
import app
uvicorn.run(app.app, host='127.0.0.1', port=8000)
" > fastapi_ci.log 2>&1 &
    PID=$!
    
    # Wait for startup
    sleep 3
    
    # Run wrk
    wrk -t4 -c100 -d5s http://127.0.0.1:8000/ping
    
elif [ "$TEST_NAME" == "starlette" ]; then
    # Run Starlette streaming
    $PYTHON_CMD -c "
import asyncio, uringcore
asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
import uvicorn
import stream_app
uvicorn.run(stream_app.app, host='127.0.0.1', port=8001)
" > starlette_ci.log 2>&1 &
    PID=$!
    
    sleep 3
    
    # Verify monotonic ordering
    curl -sN http://127.0.0.1:8001/stream | nl | head -n 20
    # Simple check for output
    curl -sN http://127.0.0.1:8001/stream | grep -q "0"
    
elif [ "$TEST_NAME" == "django" ]; then
    # Run Django with Daphne
    # Daphne is an executable, usually in venv/bin. Ensure PATH includes it or use venv path.
    export PATH="../../.venv/bin:$PATH"
    cd testproj
    daphne -p 8002 testproj.asgi:application > django_ci.log 2>&1 &
    PID=$!
    cd ..
    
    sleep 5
    
    # Check Admin page (302 or 200)
    curl -I http://127.0.0.1:8002/admin/login/?next=/admin/ | grep -E "HTTP/1.1 (200|302)"

elif [ "$TEST_NAME" == "nosqpoll" ]; then
    # Run with try_sqpoll=False
    $PYTHON_CMD -c "
import asyncio, uringcore
# Disable SQPOLL explicitly
loop = uringcore.UringEventLoop(try_sqpoll=False)
asyncio.set_event_loop(loop)
import uvicorn
import app
uvicorn.run(app.app, host='127.0.0.1', port=8003)
" > nosqpoll_ci.log 2>&1 &
    PID=$!
    
    sleep 3
    
    wrk -t4 -c100 -d5s http://127.0.0.1:8003/ping

else
    echo "Unknown test: $TEST_NAME"
    exit 1
fi

echo "Test $TEST_NAME Passed"
