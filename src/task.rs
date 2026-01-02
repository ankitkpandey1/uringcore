use pyo3::exceptions::PyStopIteration;
use pyo3::prelude::*;

#[pyclass(module = "uringcore", weakref)]
pub struct UringTask {
    coro: PyObject,
    loop_: PyObject,
    #[allow(dead_code)]
    name: Mutex<Option<String>>,
    #[allow(dead_code)]
    context: Option<PyObject>,
    future: PyObject,
    wakeup: Arc<Mutex<Option<PyObject>>>,
    #[pyo3(get, set)]
    _log_destroy_pending: bool,
    // SOTA: Cached asyncio function references
    enter_task_fn: PyObject,
    leave_task_fn: PyObject,
}

use crate::future::{FutureState, UringFuture};
use parking_lot::Mutex;
use std::sync::Arc;

/// Internal methods for UringTask (not exposed to Python)
impl UringTask {
    /// The core step method (Native Rust version - OPTIMIZED).
    /// Removes _enter_task/_leave_task calls from hot path for performance.
    pub fn run_step(&self, py: Python<'_>, slf: Py<Self>, scheduler: &crate::scheduler::Scheduler) -> PyResult<()> {
        let (coro, future) = {
            let refs = slf.borrow(py);
            (
                refs.coro.clone_ref(py),
                refs.future.clone_ref(py),
            )
        };

        // Fast check using Python's done() - this is necessary
        if future.call_method0(py, "done")?.is_truthy(py)? {
            return Ok(());
        }

        // Step the coroutine directly (NO _enter_task/_leave_task)
        let result = coro.call_method1(py, "send", (py.None(),));

        // Inline helper to get or create wakeup
        let get_wakeup = || -> PyResult<PyObject> {
            {
                let refs = slf.borrow(py);
                let w = refs.wakeup.lock();
                if let Some(ref obj) = *w {
                    return Ok(obj.clone_ref(py));
                }
            }
            let obj = slf.getattr(py, "_wakeup")?;
            let refs = slf.borrow(py);
            let mut w = refs.wakeup.lock();
            *w = Some(obj.clone_ref(py));
            Ok(obj)
        };

        match result {
            Ok(yielded) => {
                // Optimization: Check if yielded is our native UringFuture
                if let Ok(uring_fut) = yielded.downcast_bound::<UringFuture>(py) {
                    let refs = uring_fut.borrow();
                    let state_guard = refs.state.lock();

                    if matches!(*state_guard, FutureState::Pending) {
                        drop(state_guard);
                        
                        // Native callback registration
                        let refs = uring_fut.borrow();
                        let state_guard = refs.state.lock();
                        if matches!(*state_guard, FutureState::Pending) {
                            let wakeup = get_wakeup()?;
                            let mut cb_guard = refs.callbacks.lock();
                            cb_guard.push((wakeup, None));
                        } else {
                            // Future finished - reschedule immediately via scheduler
                            drop(state_guard);
                            scheduler.push(slf.into_any());
                        }
                    } else {
                        // Already done - reschedule to collect result
                        drop(state_guard);
                        scheduler.push(slf.into_any());
                    }
                } else if yielded.is_none(py) {
                    // Task yielded None (e.g. sleep(0)). Re-schedule immediately via scheduler.
                    scheduler.push(slf.into_any());
                } else {
                    // Generic awaitable - use Python add_done_callback
                    let wakeup = get_wakeup()?;
                    yielded.call_method1(py, "add_done_callback", (wakeup,))?;
                }
            }
            Err(e) => {
                if e.is_instance_of::<PyStopIteration>(py) {
                    let value = e.value(py);
                    let ret_val = value
                        .getattr("value")
                        .map_or_else(|_| py.None(), std::convert::Into::into);
                    future.call_method1(py, "set_result", (ret_val,))?;
                } else {
                    future.call_method1(py, "set_exception", (e,))?;
                }
            }
        }
        Ok(())
    }
}

#[pymethods]
impl UringTask {
    #[new]
    #[pyo3(signature = (coro, loop_, name=None, context=None))]
    fn new(
        py: Python<'_>,
        coro: PyObject,
        loop_: PyObject,
        name: Option<String>,
        context: Option<PyObject>,
    ) -> PyResult<Self> {
        let future = loop_.call_method0(py, "create_future")?;
        
        // SOTA: Cache asyncio.tasks functions once
        let asyncio_tasks = py.import("asyncio.tasks")?;
        let enter_task_fn = asyncio_tasks.getattr("_enter_task")?.into();
        let leave_task_fn = asyncio_tasks.getattr("_leave_task")?.into();
        
        Ok(Self {
            coro,
            loop_,
            name: Mutex::new(name),
            context,
            future,
            wakeup: Arc::new(Mutex::new(None)),
            _log_destroy_pending: true,
            enter_task_fn,
            leave_task_fn,
        })
    }

    /// Public API to start the task
    #[allow(clippy::needless_pass_by_value)]
    fn _start(slf: Py<Self>, py: Python<'_>) -> PyResult<()> {
        let refs = slf.borrow(py);
        let loop_ = refs.loop_.clone_ref(py);

        let kwargs = if let Some(ctx) = refs.context.as_ref() {
            let d = pyo3::types::PyDict::new(py);
            d.set_item("context", ctx)?;
            Some(d)
        } else {
            None
        };

        let step_cb = slf.getattr(py, "_step")?;
        loop_.call_method(py, "call_soon", (step_cb,), kwargs.as_ref())?;
        Ok(())
    }

    /// The core step method (Python exposed).
    #[pyo3(signature = (value=None, exc=None))]
    #[allow(clippy::needless_pass_by_value)]
    fn _step(
        slf: Py<Self>,
        py: Python<'_>,
        value: Option<PyObject>,
        exc: Option<PyObject>,
    ) -> PyResult<()> {

        let (coro, loop_, future) = {
            let refs = slf.borrow(py);
            (
                refs.coro.clone_ref(py),
                refs.loop_.clone_ref(py),
                refs.future.clone_ref(py),
            )
        };

        if future.call_method0(py, "done")?.is_truthy(py)? {
            return Ok(());
        }

        // Setup asyncio current_task context
        let asyncio_tasks = py.import("asyncio.tasks")?;
        asyncio_tasks.call_method1("_enter_task", (loop_.clone_ref(py), slf.clone_ref(py)))?;

        let result = if let Some(ref e) = exc {
            // Python's generator.throw() expects (type, value, traceback)
            let builtins = py.import("builtins")?;
            let exc_type = builtins.call_method1("type", (e,))?;
            coro.call_method1(py, "throw", (exc_type, e))
        } else {
            let arg = value.unwrap_or_else(|| py.None());
            coro.call_method1(py, "send", (arg,))
        };

        // Restore context
        asyncio_tasks.call_method1("_leave_task", (loop_.clone_ref(py), slf.clone_ref(py)))?;

        // Helper to get or create wakeup safely
        let get_wakeup = || -> PyResult<PyObject> {
            {
                let refs = slf.borrow(py);
                let w = refs.wakeup.lock();
                if let Some(ref obj) = *w {
                    return Ok(obj.clone_ref(py));
                }
            } // Lock released

            let obj = slf.getattr(py, "_wakeup")?;
            let refs = slf.borrow(py);
            let mut w = refs.wakeup.lock();
            *w = Some(obj.clone_ref(py));
            Ok(obj)
        };

        match result {
            Ok(yielded) => {
                if yielded.is_none(py) {
                    // Optimization for yield None in legacy _step too
                    let core = loop_.getattr(py, "_core")?;
                    core.call_method1(py, "push_task", (slf.clone_ref(py),))?;
                } else {
                    // Logic for other futures remains same (use add_done_callback)
                    let wakeup = get_wakeup()?;
                    yielded.call_method1(py, "add_done_callback", (wakeup,))?;
                }
            }
            Err(e) => {
                if e.is_instance_of::<PyStopIteration>(py) {
                    let value = e.value(py);
                    let ret_val = value
                        .getattr("value")
                        .map_or_else(|_| py.None(), std::convert::Into::into);
                    future.call_method1(py, "set_result", (ret_val,))?;
                } else {
                    // Check if it's a CancelledError - use cancel() instead of set_exception()
                    let asyncio = py.import("asyncio")?;
                    let cancelled_error_type = asyncio.getattr("CancelledError")?;
                    let builtins = py.import("builtins")?;
                    let is_cancelled: bool = builtins
                        .call_method1("isinstance", (e.value(py), cancelled_error_type))?
                        .extract()?;
                    if is_cancelled {
                        // Cancel the future properly
                        future.call_method0(py, "cancel")?;
                    } else {
                        future.call_method1(py, "set_exception", (e,))?;
                    }
                }
            }
        }
        Ok(())
    }

    /// Callback when a yielded future completes.
    #[allow(clippy::needless_pass_by_value)]
    fn _wakeup(slf: Py<Self>, py: Python<'_>, future: Bound<'_, PyAny>) -> PyResult<()> {
        let exc = future.call_method0("exception")?;

        let (val, err) = if exc.is_none() {
            let res = future.call_method0("result")?;
            (Some(res.into()), None)
        } else {
            (None, Some(exc.into()))
        };

        Self::_step(slf, py, val, err)
    }

    #[allow(clippy::needless_pass_by_value)]
    fn __await__(slf: Py<Self>, py: Python<'_>) -> PyResult<PyObject> {
        let users_future = slf.borrow(py).future.clone_ref(py);
        users_future.call_method0(py, "__await__")
    }

    // =========================================================================
    // Future Interface (Proxy)
    // =========================================================================

    fn cancel(slf: Py<Self>, py: Python<'_>) -> PyResult<bool> {
        let refs = slf.borrow(py);
        if refs.future.call_method0(py, "done")?.is_truthy(py)? {
            return Ok(false);
        }

        let loop_ = refs.loop_.clone_ref(py);
        drop(refs);

        // Create CancelledError instance and schedule _step
        let asyncio = py.import("asyncio")?;
        let exc = asyncio.getattr("CancelledError")?.call0()?;
        let step_cb = slf.getattr(py, "_step")?;
        loop_.call_method1(py, "call_soon", (step_cb, py.None(), exc))?;

        Ok(true)
    }

    fn done(&self, py: Python<'_>) -> PyResult<PyObject> {
        self.future.call_method0(py, "done")
    }

    fn result(&self, py: Python<'_>) -> PyResult<PyObject> {
        self.future.call_method0(py, "result")
    }

    fn cancelled(&self, py: Python<'_>) -> PyResult<PyObject> {
        self.future.call_method0(py, "cancelled")
    }

    /// Returns the number of pending cancellation requests (Python 3.11+).
    fn cancelling(&self, _py: Python<'_>) -> PyResult<i32> {
        // For compatibility, return 0 (no pending cancellations)
        // A full implementation would track nested cancel() calls
        Ok(0)
    }

    /// Decrement the pending cancellation count (Python 3.11+).
    fn uncancel(&self, _py: Python<'_>) -> PyResult<i32> {
        // For compatibility, return 0
        Ok(0)
    }

    fn exception(&self, py: Python<'_>) -> PyResult<PyObject> {
        self.future.call_method0(py, "exception")
    }

    #[pyo3(signature = (func, context=None))]
    fn add_done_callback(
        &self,
        py: Python<'_>,
        func: PyObject,
        context: Option<PyObject>,
    ) -> PyResult<PyObject> {
        if let Some(ctx) = context {
            self.future
                .call_method1(py, "add_done_callback", (func, ctx))
        } else {
            self.future.call_method1(py, "add_done_callback", (func,))
        }
    }

    fn remove_done_callback(&self, py: Python<'_>, func: PyObject) -> PyResult<PyObject> {
        self.future
            .call_method1(py, "remove_done_callback", (func,))
    }

    fn get_loop(&self, py: Python<'_>) -> PyResult<PyObject> {
        Ok(self.loop_.clone_ref(py))
    }

    fn get_name(&self, _py: Python<'_>) -> PyResult<String> {
        let guard = self.name.lock();
        Ok(guard.clone().unwrap_or_else(|| "Task".to_string()))
    }

    fn set_name(&self, name: String) -> PyResult<()> {
        let mut guard = self.name.lock();
        *guard = Some(name);
        Ok(())
    }

    #[getter]
    fn _loop(&self, py: Python<'_>) -> PyResult<PyObject> {
        Ok(self.loop_.clone_ref(py))
    }
}
