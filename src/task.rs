use pyo3::exceptions::PyStopIteration;
use pyo3::prelude::*;

#[pyclass(module = "uringcore")]
pub struct UringTask {
    coro: PyObject,
    loop_: PyObject,
    #[allow(dead_code)]
    name: Option<String>,
    #[allow(dead_code)]
    context: Option<PyObject>,
    future: PyObject,
    wakeup: Arc<Mutex<Option<PyObject>>>,
    #[pyo3(get, set)]
    _log_destroy_pending: bool,
}

use crate::future::{FutureState, UringFuture};
use parking_lot::Mutex;
use std::sync::Arc;

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
        Ok(Self {
            coro,
            loop_,
            name,
            context,
            future,
            wakeup: Arc::new(Mutex::new(None)),
            _log_destroy_pending: true,
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

    /// The core step method (Native Rust version).
    pub fn run_step(&self, py: Python<'_>, slf: Py<Self>) -> PyResult<()> {
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
        // _enter_task(loop, task)
        asyncio_tasks.call_method1("_enter_task", (loop_.clone_ref(py), slf.clone_ref(py)))?;

        // Note: run_step currently assumes no args (e.g. from ready queue).
        // If we need to pass args, we need to store them on the task or infer from future.
        // For standard task execution (send(None)), this is sufficient.
        let result = {
            let arg = py.None();
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
                // Optimization: Check if yielded is our native UringFuture
                if let Ok(uring_fut) = yielded.downcast_bound::<UringFuture>(py) {
                    let refs = uring_fut.borrow();
                    let state_guard = refs.state.lock();

                    if matches!(*state_guard, FutureState::Pending) {
                        drop(state_guard);
                        let wakeup = get_wakeup()?;

                        // Re-acquire lock to push callback
                        let refs = uring_fut.borrow();
                        let state_guard = refs.state.lock();
                        if matches!(*state_guard, FutureState::Pending) {
                            let mut cb_guard = refs.callbacks.lock();
                            cb_guard.push((wakeup, None));
                        } else {
                            // Finished in between
                            drop(state_guard);
                            let args = (wakeup, yielded);
                            // If finished, we just call wakeup.
                            // wakeup -> _step -> run_step. recursion?
                            // Standard asyncio uses call_soon.
                            // Here we use call_soon to be safe and consistent with logic below

                            let refs = slf.borrow(py);
                            let kwargs = if let Some(ctx) = refs.context.as_ref() {
                                let d = pyo3::types::PyDict::new(py);
                                d.set_item("context", ctx)?;
                                Some(d)
                            } else {
                                None
                            };

                            loop_.call_method(py, "call_soon", args, kwargs.as_ref())?;
                        }
                    } else {
                        drop(state_guard);
                        let wakeup = get_wakeup()?;
                        let args = (wakeup, yielded);

                        let refs = slf.borrow(py);
                        let kwargs = if let Some(ctx) = refs.context.as_ref() {
                            let d = pyo3::types::PyDict::new_bound(py);
                            d.set_item("context", ctx)?;
                            Some(d)
                        } else {
                            None
                        };

                        loop_.call_method(py, "call_soon", args, kwargs.as_ref())?;
                    }
                } else if yielded.is_none(py) {
                    // Task yielded None (e.g. sleep(0)). Re-schedule immediately.
                    // Instead of call_soon, we push directly to core.
                    let core = loop_.getattr(py, "_core")?;
                    core.call_method1(py, "push_task", (slf.clone_ref(py),))?;
                } else {
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
        // _enter_task(loop, task)
        asyncio_tasks.call_method1("_enter_task", (loop_.clone_ref(py), slf.clone_ref(py)))?;

        let result = if let Some(e) = exc {
            coro.call_method1(py, "throw", (e,))
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
                    future.call_method1(py, "set_exception", (e,))?;
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

        // Set a cancellation flag on the task?
        // We lack a mutable field we can easily access without unsafe or Mutex.
        // We can just rely on standard scheduling:
        
        let loop_ = refs.loop_.clone_ref(py);
        drop(refs);

        // Schedule _step with a special sentinel or just schedule it.
        // If we want to inject CancelledError, it's safest to construct it INSIDE _step
        // or let _step check a flag. But we don't have a flag.
        
        // Let's pass the exception CLASS, not instance, and let throw handle it?
        // Or better: Let's use Future::cancel which sets state to Cancelled.
        // BUT the user issue is that we need to allow suppression.
        
        // Alternative: Just schedule call_soon with the exception instance, 
        // effectively what we did, but checking `coro.throw` logic.
        
        // The previous error was TypeError.
        // Let's rely on Python side to construct the error?
        // We can pass a string "cancel" to _step?
        
        // Let's try simpler path:
        let asyncio = py.import("asyncio")?;
        let exc = asyncio.getattr("CancelledError")?.call0()?; // Intance
        
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
}
