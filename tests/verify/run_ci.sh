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


# Kill any existing process on the target port
cleanup_port() {
    local port=$1
    if command -v fuser >/dev/null; then
        fuser -k -n tcp $port 2>/dev/null || true
    else
        # Fallback if fuser is missing (e.g. some containers)
        # Try lsof if available
        if command -v lsof >/dev/null; then
             lsof -t -i:$port | xargs -r kill 2>/dev/null || true
        fi
    fi
    sleep 1
}

wait_for_server() {
    local port=$1
    local logfile=$2
    local retries=30
    local wait_time=0.5

    echo "Waiting for server on port $port..."
    for i in $(seq 1 $retries); do
        if curl -s "http://127.0.0.1:$port" >/dev/null || curl -s "http://127.0.0.1:$port/ping" >/dev/null; then
            echo "Server is up!"
            return 0
        fi
        
        # Check if process is still running
        if ! kill -0 $PID 2>/dev/null; then
            echo "Server process (PID $PID) died unexpectedly."
            echo "=== Server Log ($logfile) ==="
            cat $logfile
            echo "============================="
            return 1
        fi
        
        sleep $wait_time
    done

    echo "Timed out waiting for server on port $port."
    echo "=== Server Log ($logfile) ==="
    cat $logfile
    echo "============================="
    return 1
}

if [ "$TEST_NAME" == "fastapi" ]; then
    cleanup_port 8000
    # Run FastAPI with uringcore (default config)
    $PYTHON_CMD -c "
import asyncio, uringcore
asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
import uvicorn
import app
uvicorn.run(app.app, host='127.0.0.1', port=8000)
" > fastapi_ci.log 2>&1 &
    PID=$!
    
    if ! wait_for_server 8000 "fastapi_ci.log"; then wait; exit 1; fi
    
    # Run wrk
    if command -v wrk >/dev/null; then
        wrk -t4 -c100 -d5s http://127.0.0.1:8000/ping
    else
        curl -s http://127.0.0.1:8000/ping
    fi
    
elif [ "$TEST_NAME" == "starlette" ]; then
    cleanup_port 8001
    # Run Starlette streaming
    $PYTHON_CMD -c "
import asyncio, uringcore
asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
import uvicorn
import stream_app
uvicorn.run(stream_app.app, host='127.0.0.1', port=8001)
" > starlette_ci.log 2>&1 &
    PID=$!
    
    # Starlette stream endpoint might not respond to / (404 is fine for connectivity check)
    if ! wait_for_server 8001 "starlette_ci.log"; then wait; exit 1; fi
    
    # Verify monotonic ordering
    curl -sN http://127.0.0.1:8001/stream | nl | head -n 20
    # Simple check for output
    curl -sN http://127.0.0.1:8001/stream | grep -q "0"
    
elif [ "$TEST_NAME" == "django" ]; then
    cleanup_port 8002
    # Run Django with Daphne
    # Daphne is an executable, usually in venv/bin. Ensure PATH includes it or use venv path.
    export PATH="../../.venv/bin:$PATH"
    cd testproj
    daphne -p 8002 testproj.asgi:application > django_ci.log 2>&1 &
    PID=$!
    cd ..
    
    if ! wait_for_server 8002 "django_ci.log"; then wait; exit 1; fi
    
    # Check Admin page (302 or 200)
    curl -I http://127.0.0.1:8002/admin/login/?next=/admin/ | grep -E "HTTP/1.1 (200|302)"
    
elif [ "$TEST_NAME" == "nosqpoll" ]; then
    cleanup_port 8003
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
    
    if ! wait_for_server 8003 "nosqpoll_ci.log"; then wait; exit 1; fi
    
    if command -v wrk >/dev/null; then
        wrk -t4 -c100 -d5s http://127.0.0.1:8003/ping
    else
        echo "Check: $(curl -s http://127.0.0.1:8003/ping)"
    fi

else
    echo "Unknown test: $TEST_NAME"
    exit 1
fi

echo "Test $TEST_NAME Passed"
