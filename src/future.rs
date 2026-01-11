use parking_lot::Mutex;
use pyo3::exceptions::{PyStopIteration, PyValueError};
use pyo3::prelude::*;
use std::sync::Arc;

pub enum FutureState {
    Pending,
    Finished(Py<PyAny>), // Result
    Failed(Py<PyAny>),   // Exception
    Cancelled,
}

// No Clone impl

#[pyclass(module = "uringcore")]
pub struct UringFuture {
    loop_: Py<PyAny>,
    pub state: Arc<Mutex<FutureState>>,
    pub callbacks: Arc<Mutex<Vec<(Py<PyAny>, Option<Py<PyAny>>)>>>,
    #[allow(dead_code)]
    blocking: bool,
}

#[pymethods]
impl UringFuture {
    #[new]
    #[pyo3(signature = (loop_=None))]
    fn new(py: Python<'_>, loop_: Option<Py<PyAny>>) -> PyResult<Self> {
        let loop_ = if let Some(l) = loop_ {
            l
        } else {
            let asyncio = py.import("asyncio")?;
            asyncio.call_method0("get_running_loop")?.into()
        };

        Ok(Self {
            loop_,
            state: Arc::new(Mutex::new(FutureState::Pending)),
            callbacks: Arc::new(Mutex::new(Vec::new())),
            blocking: false,
        })
    }

    fn done(&self) -> bool {
        let state = self.state.lock();
        !matches!(*state, FutureState::Pending)
    }

    fn cancelled(&self) -> bool {
        let state = self.state.lock();
        matches!(*state, FutureState::Cancelled)
    }

    fn result(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let state = self.state.lock();
        match &*state {
            FutureState::Pending => Err(PyValueError::new_err("Result is not ready.")),
            FutureState::Finished(res) => Ok(res.clone_ref(py)),
            FutureState::Failed(exc) => Err(PyErr::from_value(exc.bind(py).clone())),
            FutureState::Cancelled => {
                let asyncio = py.import("asyncio")?;
                let err = asyncio.getattr("CancelledError")?;
                Err(PyErr::from_value(err))
            }
        }
    }

    fn exception(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let state = self.state.lock();
        match &*state {
            FutureState::Pending => Err(PyValueError::new_err("Exception is not set.")),
            FutureState::Finished(_) => Ok(py.None()),
            FutureState::Failed(exc) => Ok(exc.clone_ref(py)),
            FutureState::Cancelled => {
                let asyncio = py.import("asyncio")?;
                let err = asyncio.getattr("CancelledError")?;
                Err(PyErr::from_value(err))
            }
        }
    }

    fn set_result(slf: Py<Self>, py: Python<'_>, result: Py<PyAny>) -> PyResult<()> {
        let (state, callbacks, loop_) = {
            let refs = slf.borrow(py);
            (
                refs.state.clone(),
                refs.callbacks.clone(),
                refs.loop_.clone_ref(py),
            )
        };

        let mut state_guard = state.lock();
        if !matches!(*state_guard, FutureState::Pending) {
            return Err(PyValueError::new_err("Future is already done."));
        }
        *state_guard = FutureState::Finished(result);
        drop(state_guard);

        Self::_schedule_callbacks(py, callbacks, loop_, slf.into_any())
    }

    fn set_exception(slf: Py<Self>, py: Python<'_>, exception: Py<PyAny>) -> PyResult<()> {
        let (state, callbacks, loop_) = {
            let refs = slf.borrow(py);
            (
                refs.state.clone(),
                refs.callbacks.clone(),
                refs.loop_.clone_ref(py),
            )
        };

        let mut state_guard = state.lock();
        if !matches!(*state_guard, FutureState::Pending) {
            return Err(PyValueError::new_err("Future is already done."));
        }
        *state_guard = FutureState::Failed(exception);
        drop(state_guard);

        Self::_schedule_callbacks(py, callbacks, loop_, slf.into_any())
    }

    fn cancel(slf: Py<Self>, py: Python<'_>) -> PyResult<bool> {
        let (state, callbacks, loop_) = {
            let refs = slf.borrow(py);
            (
                refs.state.clone(),
                refs.callbacks.clone(),
                refs.loop_.clone_ref(py),
            )
        };

        let mut state_guard = state.lock();
        if !matches!(*state_guard, FutureState::Pending) {
            return Ok(false);
        }
        *state_guard = FutureState::Cancelled;
        drop(state_guard);

        Self::_schedule_callbacks(py, callbacks, loop_, slf.into_any())?;
        Ok(true)
    }

    #[pyo3(signature = (func, context=None))]
    fn add_done_callback(
        slf: Py<Self>,
        py: Python<'_>,
        func: Py<PyAny>,
        context: Option<Py<PyAny>>,
    ) -> PyResult<()> {
        let (state, callbacks, loop_) = {
            let refs = slf.borrow(py);
            (
                refs.state.clone(),
                refs.callbacks.clone(),
                refs.loop_.clone_ref(py),
            )
        };

        let mut callbacks_guard = callbacks.lock();
        let state_guard = state.lock();

        if !matches!(*state_guard, FutureState::Pending) {
            drop(callbacks_guard);
            drop(state_guard);
            // Schedule immediately
            Self::_schedule_single(py, loop_, func, slf.into_any(), context)?;
            return Ok(());
        }

        callbacks_guard.push((func, context));
        Ok(())
    }

    fn remove_done_callback(&self, func: Py<PyAny>, _py: Python<'_>) -> usize {
        let mut callbacks = self.callbacks.lock();
        let len_before = callbacks.len();
        callbacks.retain(|(f, _)| !f.is(&func));
        len_before - callbacks.len()
    }

    fn __await__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(slf: Py<Self>, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        let slf_clone = slf.clone_ref(py);
        let refs = slf.borrow(py);
        let state = refs.state.lock();
        // match &*state works because locked guard derefs to inner
        match &*state {
            FutureState::Pending => {
                // Yield self to signal "wait for me"
                drop(state);
                drop(refs);
                Ok(Some(slf_clone.into_any()))
            }
            FutureState::Finished(value) => Err(PyStopIteration::new_err(value.clone_ref(py))),
            FutureState::Failed(exc) => Err(PyErr::from_value(exc.bind(py).clone())),
            FutureState::Cancelled => {
                let asyncio = py.import("asyncio")?;
                let err = asyncio.getattr("CancelledError")?;
                Err(PyErr::from_value(err))
            }
        }
    }

    /// `send()` is required for coroutine protocol - behaves like __next__
    #[pyo3(signature = (_value=None))]
    fn send(
        slf: Py<Self>,
        py: Python<'_>,
        _value: Option<Py<PyAny>>,
    ) -> PyResult<Option<Py<PyAny>>> {
        // send() is effectively the same as __next__ for futures
        Self::__next__(slf, py)
    }

    /// `throw()` is required for coroutine protocol - propagates exception
    #[pyo3(signature = (typ, val=None, tb=None))]
    fn throw(
        &self,
        py: Python<'_>,
        typ: Py<PyAny>,
        val: Option<Py<PyAny>>,
        tb: Option<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        // Re-raise the exception
        let exc = if let Some(v) = val { v } else { typ.call0(py)? };

        if let Some(traceback) = tb {
            exc.bind(py).setattr("__traceback__", traceback)?;
        }

        Err(PyErr::from_value(exc.bind(py).clone()))
    }

    fn get_loop(&self, py: Python<'_>) -> Py<PyAny> {
        self.loop_.clone_ref(py)
    }
}

// Native Optimization Methods
impl UringFuture {
    pub(crate) fn set_result_fast(
        &self,
        py: Python<'_>,
        scheduler: &crate::scheduler::Scheduler,
        result: Py<PyAny>,
        future_obj: Py<PyAny>,
    ) -> PyResult<()> {
        let mut state_guard = self.state.lock();
        if !matches!(*state_guard, FutureState::Pending) {
            return Err(PyValueError::new_err("Future is already done."));
        }
        *state_guard = FutureState::Finished(result);
        drop(state_guard);

        self.schedule_fast(py, scheduler, future_obj)
    }

    pub(crate) fn set_exception_fast(
        &self,
        py: Python<'_>,
        scheduler: &crate::scheduler::Scheduler,
        exception: Py<PyAny>,
        future_obj: Py<PyAny>,
    ) -> PyResult<()> {
        let mut state_guard = self.state.lock();
        if !matches!(*state_guard, FutureState::Pending) {
            return Err(PyValueError::new_err("Future is already done."));
        }
        *state_guard = FutureState::Failed(exception);
        drop(state_guard);

        self.schedule_fast(py, scheduler, future_obj)
    }

    fn schedule_fast(
        &self,
        py: Python<'_>,
        scheduler: &crate::scheduler::Scheduler,
        future_obj: Py<PyAny>,
    ) -> PyResult<()> {
        let mut cb_guard = self.callbacks.lock();
        let drained: Vec<_> = cb_guard.drain(..).collect();
        drop(cb_guard);

        for (func, ctx) in drained {
            // Construct UringHandle directly
            let args_tuple = pyo3::types::PyTuple::new(py, vec![future_obj.clone_ref(py)])?;
            let args: Py<pyo3::types::PyTuple> = args_tuple.into();

            let handle =
                crate::handle::UringHandle::new_native(func, args, self.loop_.clone_ref(py), ctx);
            // handle must be converted to Py<PyAny> to store in scheduler
            // Scheduler stores Py<PyAny> (handles)
            let py_handle = Py::new(py, handle)?;

            scheduler.push(py_handle.into_any());
        }
        Ok(())
    }

    fn _schedule_callbacks(
        py: Python<'_>,
        callbacks: Arc<Mutex<Vec<(Py<PyAny>, Option<Py<PyAny>>)>>>,
        loop_: Py<PyAny>,
        future_obj: Py<PyAny>,
    ) -> PyResult<()> {
        let mut cb_guard = callbacks.lock();
        let drained: Vec<_> = cb_guard.drain(..).collect();
        drop(cb_guard);

        for (func, ctx) in drained {
            Self::_schedule_single(py, loop_.clone_ref(py), func, future_obj.clone_ref(py), ctx)?;
        }
        Ok(())
    }

    fn _schedule_single(
        py: Python<'_>,
        loop_: Py<PyAny>,
        func: Py<PyAny>,
        future_obj: Py<PyAny>,
        context: Option<Py<PyAny>>,
    ) -> PyResult<()> {
        let args = (func, future_obj);
        if let Some(ctx) = context {
            let kwargs = pyo3::types::PyDict::new(py);
            kwargs.set_item("context", ctx)?;
            loop_.call_method(py, "call_soon", args, Some(&kwargs))?;
        } else {
            loop_.call_method1(py, "call_soon", args)?;
        }
        Ok(())
    }
}
